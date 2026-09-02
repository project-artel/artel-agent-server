import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.api.qa_sessions import OpenQaSessionRequest
from app.agents.qa import runner as runner_module
from app.config import get_settings
from app.agents.qa.runner import QaRunner
from app.agents.qa.tools import QaRunState
from app.llm import chat_model
from app.llm.models import (
    LLMModel,
    ReasoningConfig,
    ReasoningEffort,
    ReasoningKind,
)
from app.main import app
from app.qa.channel import QaRunChannel
from app.qa.run_config import resolve_run_config
from app.qa.schemas import QaRunScenario
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore
from tests.test_qa_prompt_version import StubChatModel, make_scenario, open_request


def test_models_api_exposes_reasoning_selection_capabilities() -> None:
    catalog = {
        item["id"]: item for item in TestClient(app).get("/internal/models").json()
    }

    assert set(catalog) == {model.value for model in LLMModel}
    assert catalog[LLMModel.claude_sonnet_5]["reasoning"] == {
        "kind": "effort",
        "efforts": ["max", "xhigh", "high", "medium", "low"],
        "min_tokens": None,
        "max_tokens": None,
        "step": None,
    }
    assert catalog[LLMModel.gemini_2_5_pro]["reasoning"] == {
        "kind": "max_tokens",
        "efforts": None,
        "min_tokens": 128,
        "max_tokens": 32768,
        "step": 128,
    }
    assert catalog[LLMModel.gemini_2_5_pro]["input_modalities"] == [
        "text",
        "image",
        "file",
        "audio",
        "video",
    ]
    assert catalog[LLMModel.gemini_2_5_pro]["multimodal"] is True
    assert catalog[LLMModel.gpt_5_6_luna]["reasoning"] == {
        "kind": "effort",
        "efforts": ["max", "xhigh", "high", "medium", "low"],
        "min_tokens": None,
        "max_tokens": None,
        "step": None,
    }
    assert catalog[LLMModel.gemini_3_7_flash]["reasoning"] == {
        "kind": "effort",
        # Three, not five. The picker has to offer what the model takes: an
        # effort it never advertised is a 400 the user only sees mid-run.
        "efforts": ["high", "medium", "low"],
        "min_tokens": None,
        "max_tokens": None,
        "step": None,
    }
    assert catalog[LLMModel.gpt_chat_latest]["reasoning"] is None


def test_request_accepts_each_supported_reasoning_shape() -> None:
    effort = OpenQaSessionRequest.model_validate(
        open_request(
            model=LLMModel.claude_sonnet_5,
            reasoning={"effort": "high"},
        )
    )
    budget = OpenQaSessionRequest.model_validate(
        open_request(
            model=LLMModel.gemini_2_5_pro,
            reasoning={"max_tokens": 2048},
        )
    )

    assert effort.reasoning == ReasoningConfig(effort=ReasoningEffort.high)
    assert budget.reasoning == ReasoningConfig(max_tokens=2048)


@pytest.mark.parametrize(
    ("model", "reasoning"),
    [
        (LLMModel.gpt_chat_latest, {"effort": "low"}),
        (LLMModel.claude_sonnet_5, {"max_tokens": 2048}),
        (LLMModel.gemini_2_5_pro, {"effort": "high"}),
        # The right kind, an effort the model does not offer.
        (LLMModel.gemini_3_7_flash, {"effort": "max"}),
        (LLMModel.kimi_k3, {"effort": "medium"}),
    ],
)
def test_request_rejects_unsupported_model_reasoning_combinations(
    model: LLMModel, reasoning: dict
) -> None:
    with pytest.raises(ValidationError):
        OpenQaSessionRequest.model_validate(
            open_request(model=model, reasoning=reasoning)
        )


def test_reasoning_budget_requires_exactly_one_setting() -> None:
    with pytest.raises(ValidationError):
        ReasoningConfig()
    with pytest.raises(ValidationError):
        ReasoningConfig(effort=ReasoningEffort.low, max_tokens=1024)


def test_request_rejects_unknown_or_out_of_range_reasoning_fields() -> None:
    with pytest.raises(ValidationError):
        OpenQaSessionRequest.model_validate(
            open_request(
                model=LLMModel.claude_sonnet_5,
                reasoning={"effort": "high", "max_token": 2048},
            )
        )
    with pytest.raises(ValidationError, match="max_tokens >= 128"):
        OpenQaSessionRequest.model_validate(
            open_request(
                model=LLMModel.gemini_2_5_pro,
                reasoning={"max_tokens": 127},
            )
        )


def test_service_persists_reasoning_and_passes_it_to_runner() -> None:
    async def run() -> None:
        seen = []

        class Runner:
            async def run_with_deadline(self, channel, scenario):
                return None, None

        def factory(**kwargs):
            seen.append(kwargs)
            return Runner()

        store = InMemoryQaSessionStore()
        service = QaExecutionService(store=store, runner_factory=factory)
        reasoning = ReasoningConfig(effort=ReasoningEffort.medium)
        session_id, run_config = await service.open(
            qa_run_id=7,
            game_instance_id=1,
            scenarios=[
                QaRunScenario(qa_try_id=7, test_scenario_id=1, scenario=make_scenario())
            ],
            model=LLMModel.claude_sonnet_5,
            reasoning=reasoning,
        )

        # Returned to the caller as well as stored: Orchestration records the
        # try, and what it has to record is the resolved form.
        assert run_config.reasoning == reasoning
        assert run_config.reasoning_supported is True
        assert (await store.load(session_id)).run_config.reasoning == reasoning

        async def send(_frame: dict) -> None:
            return None

        await service.run(session_id, send)
        assert seen[0]["config"].reasoning == reasoning

    asyncio.run(run())


def test_service_rejects_invalid_reasoning_before_saving() -> None:
    async def run() -> None:
        service = QaExecutionService(InMemoryQaSessionStore())
        with pytest.raises(ValueError, match="does not support"):
            await service.open(
                qa_run_id=7,
                game_instance_id=1,
                scenarios=[
                    QaRunScenario(
                        qa_try_id=7, test_scenario_id=1, scenario=make_scenario()
                    )
                ],
                # Named rather than left to DEFAULT_MODEL: the default now
                # reasons, and this case needs a model that does not.
                model=LLMModel.gpt_chat_latest,
                reasoning=ReasoningConfig(effort=ReasoningEffort.low),
            )

    asyncio.run(run())


def test_langchain_sends_reasoning_in_openrouter_request(monkeypatch) -> None:
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "completion",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        chat_model,
        "ChatOpenAI",
        lambda **kwargs: ChatOpenAI(
            **kwargs, http_client=httpx.Client(transport=transport)
        ),
    )
    chat_model.build_chat_model.cache_clear()
    try:
        chat_model.build_chat_model(
            LLMModel.claude_sonnet_5,
            ReasoningConfig(effort=ReasoningEffort.high),
        ).invoke("test")
    finally:
        chat_model.build_chat_model.cache_clear()

    assert requests[0]["reasoning"] == {"effort": "high", "exclude": True}


def test_omitted_reasoning_is_not_sent_and_uses_a_distinct_cache_entry(
    monkeypatch,
) -> None:
    created: list[dict] = []

    class FakeChat:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(chat_model, "ChatOpenAI", FakeChat)
    chat_model.build_chat_model.cache_clear()
    try:
        plain = chat_model.build_chat_model(LLMModel.claude_sonnet_5)
        reasoned = chat_model.build_chat_model(
            LLMModel.claude_sonnet_5,
            ReasoningConfig(effort=ReasoningEffort.low),
        )
        assert plain is not reasoned
    finally:
        chat_model.build_chat_model.cache_clear()

    # usage.include always rides along (it is what makes OpenRouter report cost);
    # reasoning is the part that appears only when it was asked for.
    assert "reasoning" not in created[0]["extra_body"]
    assert created[1]["extra_body"]["reasoning"] == {"effort": "low", "exclude": True}


def test_caching_is_opt_in_and_only_for_anthropic(monkeypatch) -> None:
    """Both conditions have to hold before a breakpoint is sent.

    `cache_prompt` because the breakpoint lands at the end of the prompt: a caller
    that varies its last message shares nothing past it, so it rewrites the cache
    every request and pays the write premium for a read that never comes. Only a
    caller that appends — the agent loop — comes out ahead.

    Anthropic because OpenAI and Google cache without being asked, and must not be
    sent a parameter their providers do not read.
    """
    created: list[dict] = []

    class FakeChat:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(chat_model, "ChatOpenAI", FakeChat)
    chat_model.build_chat_model.cache_clear()
    try:
        chat_model.build_chat_model(LLMModel.claude_opus_5, cache_prompt=True)
        chat_model.build_chat_model(LLMModel.claude_opus_5)
        chat_model.build_chat_model(LLMModel.gpt_chat_latest, cache_prompt=True)
    finally:
        chat_model.build_chat_model.cache_clear()

    asked_anthropic, default_anthropic, asked_openai = created
    assert asked_anthropic["extra_body"]["cache_control"] == {"type": "ephemeral"}
    # extra_body always carries usage.include (cost reporting), so the check is
    # the absence of the breakpoint, not an empty body.
    assert "cache_control" not in default_anthropic["extra_body"]
    assert "cache_control" not in asked_openai["extra_body"]


def test_run_start_log_names_reasoning(monkeypatch, caplog) -> None:
    class SilentAgent:
        def astream(self, *_args, **_kwargs):
            async def updates():
                return
                yield {}

            return updates()

    monkeypatch.setattr(
        runner_module, "build_chat_model", lambda model, reasoning=None, **_: StubChatModel()
    )
    monkeypatch.setattr(
        runner_module, "create_agent", lambda **_kwargs: SilentAgent()
    )

    async def run() -> None:
        async def send(_frame: dict) -> None:
            return None

        runner = QaRunner(
            resolve_run_config(
                model=LLMModel.claude_sonnet_5,
                reasoning=ReasoningConfig(effort=ReasoningEffort.high),
            )
        )
        await runner.run(
            QaRunChannel(qa_try_id=7, send=send),
            make_scenario(),
            QaRunState(total_steps=1),
        )

    with caplog.at_level(logging.INFO, logger="app.agents.qa.runner"):
        asyncio.run(run())

    starting = [
        record.getMessage()
        for record in caplog.records
        if "[QA] run starting" in record.getMessage()
    ]
    assert len(starting) == 1
    assert "'model': 'anthropic/claude-sonnet-5'" in starting[0]
    assert "'reasoning': {'effort': 'high'}" in starting[0]


def test_a_model_call_carries_a_request_timeout_and_bounded_retries(
    monkeypatch,
) -> None:
    """A call that never comes back must end as a failure, not as a wait (ARTEL-510).

    The OpenAI client defaults to 600 s with 2 retries. With those defaults a
    stalled upstream held one authoring turn for up to 30 minutes while the person
    watching the chat had nothing to read and no way out but reloading the page.
    """
    created: list[dict] = []

    class FakeChat:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(chat_model, "ChatOpenAI", FakeChat)
    chat_model.build_chat_model.cache_clear()
    try:
        chat_model.build_chat_model(LLMModel.claude_sonnet_5)
    finally:
        chat_model.build_chat_model.cache_clear()

    settings = get_settings()
    assert created[0]["timeout"] == settings.openrouter_timeout_seconds
    assert created[0]["max_retries"] == settings.openrouter_max_retries
    # Whatever the values are, they have to be tighter than the client defaults —
    # that is the whole point of setting them.
    assert settings.openrouter_timeout_seconds < 600
    assert settings.openrouter_max_retries < 2
