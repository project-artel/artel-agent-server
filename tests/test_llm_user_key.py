"""The user's own LLM key: carried on the QA session, bound to its task, chosen per route.

No test here reaches the network. Request bodies are read from
`_get_request_payload`, the dict each client hands to its SDK.
"""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.llm import chat_model
from app.llm.api_key import KeyRefreshOnUnauthorized
from app.llm.chat_model import (
    OPENAI_API_BASE_URL,
    ClaudeApiKeyMissingError,
    GptApiKeyMissingError,
    build_chat_model,
    key_service_of,
    structured,
)
from app.llm.claude_api import ClaudeApiChat
from app.llm.credentials import LlmKey, LlmKeyService, bind_llm_key, bound_llm_key
from app.llm.models import ReasoningConfig, ReasoningEffort
from app.llm.usage import UsageBuffer, UsageCallback, set_usage_scope
from app.main import create_app
from app.qa.schemas import QaRunScenario, QaScenario
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore
from tests.conftest import install_catalog_with_enabled
from tests.test_qa_prompt_version import open_request

OPENROUTER_MODEL = "openai/gpt-6-luna"
CLAUDE_MODEL = "claude/claude-sonnet-5-5"
GPT_MODEL = "gpt/gpt-6.1-sol"
BEDROCK_MODEL = "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
GPT_MODELS = ["gpt/gpt-6.1-sol", "gpt/gpt-6-astra", "gpt/gpt-6-luna", "gpt/gpt-5.6-terra"]

USER_ANTHROPIC = LlmKey(service=LlmKeyService.anthropic, api_key="sk-ant-user")
USER_OPENAI = LlmKey(service=LlmKeyService.openai, api_key="sk-user-openai")
USER_OPENROUTER = LlmKey(service=LlmKeyService.openrouter, api_key="sk-or-user")


class _PlatformKeys:
    """Stands in for `LlmApiKeyProvider`: the platform keys an administrator stored."""

    def __init__(self) -> None:
        self.anthropic: str | None = "sk-ant-platform"
        self.openrouter = "sk-or-platform"

    def get_anthropic_key(self) -> str | None:
        return self.anthropic

    def get_bedrock_key(self) -> str | None:
        return None

    def get_openrouter_key_or_placeholder(self) -> str:
        return self.openrouter

    def invalidate(self) -> None:
        pass


@pytest.fixture(autouse=True)
def platform_keys(monkeypatch) -> _PlatformKeys:
    fixed = _PlatformKeys()
    monkeypatch.setattr("app.llm.chat_model.get_llm_api_key_provider", lambda: fixed)
    build_chat_model.cache_clear()
    bind_llm_key(None)
    yield fixed
    bind_llm_key(None)
    build_chat_model.cache_clear()


def _openrouter_key(model: ChatOpenAI) -> str:
    """The key the OpenRouter client sends, whether it was given a string or a callable."""
    key = model.openai_api_key
    return key() if callable(key) else key.get_secret_value()


def _usage_callback(model) -> UsageCallback:
    (callback,) = [c for c in model.callbacks if isinstance(c, UsageCallback)]
    return callback


# --- which key each route takes ------------------------------------------------------------


def test_routes_pair_with_key_services() -> None:
    assert key_service_of(CLAUDE_MODEL) is LlmKeyService.anthropic
    assert key_service_of(GPT_MODEL) is LlmKeyService.openai
    assert key_service_of(OPENROUTER_MODEL) is LlmKeyService.openrouter
    assert key_service_of("anthropic/claude-sonnet-5.5") is LlmKeyService.openrouter
    assert key_service_of(BEDROCK_MODEL) is None


def test_without_a_bound_key_every_route_keeps_the_platform_key() -> None:
    claude = build_chat_model(CLAUDE_MODEL)
    openrouter = build_chat_model(OPENROUTER_MODEL)

    assert claude.anthropic_api_key.get_secret_value() == "sk-ant-platform"
    assert _openrouter_key(openrouter) == "sk-or-platform"
    # Still a callable, so an administrator replacing the key reaches this client.
    assert callable(openrouter.openai_api_key)
    assert _usage_callback(claude)._key_source == "platform"
    assert _usage_callback(openrouter)._key_source == "platform"
    assert any(isinstance(c, KeyRefreshOnUnauthorized) for c in openrouter.callbacks)


def test_a_matching_anthropic_key_replaces_the_platform_key() -> None:
    bind_llm_key(USER_ANTHROPIC)

    claude = build_chat_model(CLAUDE_MODEL)

    assert isinstance(claude, ClaudeApiChat)
    assert claude.anthropic_api_key.get_secret_value() == "sk-ant-user"
    assert _usage_callback(claude)._key_source == "user"
    # Re-reading the platform key cannot fix a rejected user key.
    assert not any(isinstance(c, KeyRefreshOnUnauthorized) for c in claude.callbacks)


def test_a_matching_openrouter_key_replaces_the_platform_key() -> None:
    bind_llm_key(USER_OPENROUTER)

    openrouter = build_chat_model(OPENROUTER_MODEL)

    assert _openrouter_key(openrouter) == "sk-or-user"
    assert _usage_callback(openrouter)._key_source == "user"
    assert not any(isinstance(c, KeyRefreshOnUnauthorized) for c in openrouter.callbacks)


def test_a_key_for_another_service_leaves_the_platform_key_in_place() -> None:
    bind_llm_key(USER_ANTHROPIC)

    openrouter = build_chat_model(OPENROUTER_MODEL)

    assert _openrouter_key(openrouter) == "sk-or-platform"
    assert _usage_callback(openrouter)._key_source == "platform"

    bind_llm_key(USER_OPENROUTER)
    claude = build_chat_model(CLAUDE_MODEL)

    assert claude.anthropic_api_key.get_secret_value() == "sk-ant-platform"
    assert _usage_callback(claude)._key_source == "platform"


def test_a_user_anthropic_key_works_without_a_platform_one(platform_keys) -> None:
    platform_keys.anthropic = None

    with pytest.raises(ClaudeApiKeyMissingError):
        build_chat_model(CLAUDE_MODEL)

    bind_llm_key(USER_ANTHROPIC)
    assert build_chat_model(CLAUDE_MODEL).anthropic_api_key.get_secret_value() == "sk-ant-user"


def test_gpt_without_an_openai_key_fails_before_any_client_is_built(monkeypatch) -> None:
    built = []
    monkeypatch.setattr(chat_model, "ChatOpenAI", lambda **kwargs: built.append(kwargs))

    with pytest.raises(GptApiKeyMissingError, match="openai"):
        build_chat_model(GPT_MODEL)

    bind_llm_key(USER_OPENROUTER)
    with pytest.raises(GptApiKeyMissingError):
        build_chat_model(GPT_MODEL)
    assert built == []


def test_clients_are_not_shared_across_keys() -> None:
    platform = build_chat_model(OPENROUTER_MODEL)
    bind_llm_key(USER_OPENROUTER)
    first_user = build_chat_model(OPENROUTER_MODEL)
    bind_llm_key(LlmKey(service=LlmKeyService.openrouter, api_key="sk-or-other-user"))
    second_user = build_chat_model(OPENROUTER_MODEL)

    assert len({id(platform), id(first_user), id(second_user)}) == 3
    assert _openrouter_key(first_user) == "sk-or-user"
    assert _openrouter_key(second_user) == "sk-or-other-user"



def test_a_session_reuses_its_clients_and_a_new_session_gets_its_own() -> None:
    bind_llm_key(USER_OPENROUTER)
    first = build_chat_model(OPENROUTER_MODEL)
    assert build_chat_model(OPENROUTER_MODEL) is first

    # The same user's next session — same key — still starts from nothing.
    bind_llm_key(USER_OPENROUTER)
    assert build_chat_model(OPENROUTER_MODEL) is not first


def test_user_key_clients_never_enter_the_process_cache() -> None:
    from app.llm.chat_model import _build_chat_model

    build_chat_model.cache_clear()
    bind_llm_key(USER_ANTHROPIC)
    build_chat_model(CLAUDE_MODEL)
    bind_llm_key(USER_OPENAI)
    build_chat_model(GPT_MODEL)
    bind_llm_key(USER_OPENROUTER)
    build_chat_model(OPENROUTER_MODEL)

    assert _build_chat_model.cache_info().currsize == 0


def test_a_sessions_clients_go_when_the_session_does() -> None:
    import gc
    import weakref

    async def session() -> weakref.ref:
        bind_llm_key(USER_ANTHROPIC)
        return weakref.ref(build_chat_model(CLAUDE_MODEL))

    # A session's run lives in its own task; when that task ends, nothing holds its clients.
    async def run() -> weakref.ref:
        return await asyncio.create_task(session())

    client = asyncio.run(run())
    gc.collect()
    assert client() is None


def test_claude_clients_are_not_shared_across_keys() -> None:
    bind_llm_key(USER_ANTHROPIC)
    first = build_chat_model(CLAUDE_MODEL)
    bind_llm_key(LlmKey(service=LlmKeyService.anthropic, api_key="sk-ant-other-user"))
    second = build_chat_model(CLAUDE_MODEL)

    assert first is not second
    assert second.anthropic_api_key.get_secret_value() == "sk-ant-other-user"


# --- the `gpt/` route --------------------------------------------------------------------


@pytest.mark.parametrize("model_id", GPT_MODELS)
def test_gpt_ids_call_openai_directly_with_the_users_key(model_id) -> None:
    bind_llm_key(USER_OPENAI)

    model = build_chat_model(model_id)

    assert isinstance(model, ChatOpenAI)
    assert model.model_name == model_id.removeprefix("gpt/")
    assert model.openai_api_key.get_secret_value() == "sk-user-openai"
    assert model.openai_api_base == OPENAI_API_BASE_URL == "https://api.openai.com/v1"
    assert str(model.root_client.base_url).rstrip("/") == "https://api.openai.com/v1"
    assert _usage_callback(model)._key_source == "user"


def test_environment_variables_do_not_redirect_the_gpt_call(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "http://elsewhere.test")
    monkeypatch.setenv("OPENAI_API_BASE", "http://elsewhere.test")
    bind_llm_key(USER_OPENAI)

    model = build_chat_model(GPT_MODEL)

    assert str(model.root_client.base_url).rstrip("/") == "https://api.openai.com/v1"


@pytest.mark.parametrize(
    ("reasoning", "expected"),
    [
        (ReasoningConfig(effort=ReasoningEffort.high), "high"),
        (ReasoningConfig(effort=ReasoningEffort.max), "max"),
        (ReasoningConfig(effort=ReasoningEffort.low), "low"),
        (ReasoningConfig(max_tokens=0), "none"),
    ],
)
def test_gpt_payload_carries_openai_reasoning_effort(reasoning, expected) -> None:
    bind_llm_key(USER_OPENAI)

    payload = build_chat_model(GPT_MODEL, reasoning)._get_request_payload([HumanMessage("hi")])

    assert payload["model"] == "gpt-6.1-sol"
    # Responses API: `input`, not `messages`, and the effort inside `reasoning`.
    assert "input" in payload
    assert "messages" not in payload
    assert payload["reasoning"] == {"effort": expected}
    assert "reasoning_effort" not in payload
    # none of OpenRouter's request fields.
    assert "extra_body" not in payload
    assert "temperature" not in payload


def test_gpt_payload_without_reasoning_leaves_the_effort_to_the_model() -> None:
    bind_llm_key(USER_OPENAI)

    payload = build_chat_model(GPT_MODEL)._get_request_payload([HumanMessage("hi")])

    assert "reasoning" not in payload
    assert "reasoning_effort" not in payload
    assert "temperature" not in payload
    assert "extra_body" not in payload


def test_gpt_keeps_nothing_on_openai_and_asks_for_reasoning_it_can_send_back() -> None:
    bind_llm_key(USER_OPENAI)

    payload = build_chat_model(GPT_MODEL)._get_request_payload([HumanMessage("hi")])

    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]


def test_gpt_sends_the_previous_turns_encrypted_reasoning_back() -> None:
    """The point of the Responses API here: a turn's reasoning reaches the next turn."""
    bind_llm_key(USER_OPENAI)
    earlier = AIMessage(
        content=[
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAA-sealed"},
            {"type": "text", "text": "Clicking the start button."},
        ]
    )
    dropped = AIMessage(
        content=[
            {"type": "reasoning", "id": "rs_2", "summary": []},
            {"type": "text", "text": "No sealed reasoning on this one."},
        ]
    )

    payload = build_chat_model(GPT_MODEL)._get_request_payload(
        [HumanMessage("start"), earlier, HumanMessage("next"), dropped, HumanMessage("again")]
    )

    reasoning_items = [item for item in payload["input"] if item.get("type") == "reasoning"]
    assert [item.get("encrypted_content") for item in reasoning_items] == ["gAAAA-sealed"]


def test_gpt_payload_is_not_sent_to_openrouter() -> None:
    bind_llm_key(USER_OPENAI)

    model = build_chat_model(GPT_MODEL)

    assert "openrouter" not in str(model.root_client.base_url)
    assert not model.default_headers


class _Answer(BaseModel):
    value: int


def test_gpt_structured_output_uses_strict_json_schema() -> None:
    bind_llm_key(USER_OPENAI)

    chain = structured(GPT_MODEL, _Answer)
    bound = chain.first
    payload = build_chat_model(GPT_MODEL)._get_request_payload(
        [HumanMessage("hi")], **bound.kwargs
    )

    # A pydantic schema rides as the class; on the Responses API langchain-openai sends it
    # as `text_format`, which the SDK's `responses.parse` turns into a strict `json_schema`.
    assert payload["text_format"] is _Answer
    assert "response_format" not in payload
    assert payload["ls_structured_output_format"]["kwargs"] == {
        "method": "json_schema",
        "strict": True,
    }
    assert payload["model"] == "gpt-6.1-sol"


def test_gpt_usage_is_recorded_under_the_catalog_id_as_the_users() -> None:
    bind_llm_key(USER_OPENAI)
    callback = _usage_callback(build_chat_model(GPT_MODEL))
    sent: list[dict] = []

    async def send(payload: dict) -> None:
        sent.append(payload)

    callback._buffer = UsageBuffer(base_url="http://orchestration.test", send=send, flush_size=1)
    result = LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        "done",
                        usage_metadata={
                            "input_tokens": 100,
                            "output_tokens": 10,
                            "total_tokens": 110,
                        },
                    )
                )
            ]
        ],
        llm_output={"model_name": "gpt-6.1-sol"},
    )

    async def scenario() -> None:
        set_usage_scope("QA_RUN", 3)
        run_id = uuid4()
        await callback.on_chat_model_start({}, [], run_id=run_id)
        await callback.on_llm_end(result, run_id=run_id)

    asyncio.run(scenario())

    (record,) = sent[0]["records"]
    assert record["model"] == GPT_MODEL
    assert record["provider"] == "gpt"
    assert record["keySource"] == "user"


# --- usage `keySource` -------------------------------------------------------------------


@pytest.mark.parametrize(("key_source", "expected"), [(None, "platform"), ("user", "user")])
def test_usage_records_carry_the_key_source(key_source, expected) -> None:
    sent: list[dict] = []

    async def send(payload: dict) -> None:
        sent.append(payload)

    buffer = UsageBuffer(base_url="http://orchestration.test", send=send, flush_size=1)
    callback = (
        UsageCallback(buffer) if key_source is None else UsageCallback(buffer, key_source=key_source)
    )
    result = LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        "done",
                        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    )
                )
            ]
        ],
        llm_output={"model_name": OPENROUTER_MODEL, "token_usage": {"cost": 0.001}},
    )

    async def scenario() -> None:
        set_usage_scope("QA_RUN", 1)
        run_id = uuid4()
        await callback.on_chat_model_start({}, [], run_id=run_id)
        await callback.on_llm_end(result, run_id=run_id)

    asyncio.run(scenario())

    (record,) = sent[0]["records"]
    assert record["keySource"] == expected


# --- the session carries the key, and only the session ----------------------------------


def _open(service: QaExecutionService, **kwargs):
    return service.open(
        qa_run_id=9,
        game_instance_id=1,
        scenarios=[
            QaRunScenario(qa_try_id=101, test_scenario_id=1, scenario=QaScenario(title="S"))
        ],
        model=CLAUDE_MODEL,
        **kwargs,
    )


def test_the_key_lives_on_the_session_record_only_and_goes_with_it() -> None:
    async def run() -> None:
        store = InMemoryQaSessionStore()
        service = QaExecutionService(store=store)
        session_id, run_config = await _open(service, llm_key=USER_ANTHROPIC)

        assert "sk-ant-user" not in run_config.model_dump_json()
        record = await store.load(session_id)
        assert record.llm_key == USER_ANTHROPIC
        assert "sk-ant-user" not in repr(record)
        assert "sk-ant-user" not in repr(record.llm_key)

        await service.bind_llm_key(session_id)
        assert bound_llm_key() == USER_ANTHROPIC

        await service.close(session_id)
        assert await store.load(session_id) is None

    asyncio.run(run())


def test_a_session_without_a_key_binds_none() -> None:
    async def run() -> None:
        service = QaExecutionService(store=InMemoryQaSessionStore())
        session_id, _ = await _open(service)
        bind_llm_key(USER_OPENROUTER)

        await service.bind_llm_key(session_id)

        assert bound_llm_key() is None

    asyncio.run(run())


@pytest.fixture
def client():
    # The `claude/` rows are seeded off; a session request refuses a disabled model.
    install_catalog_with_enabled(CLAUDE_MODEL)
    app = create_app()
    store = InMemoryQaSessionStore()
    app.state.qa_session_service = QaExecutionService(store=store)
    app.state.qa_session_store = store
    return TestClient(app)


def test_open_without_llm_key_still_works(client) -> None:
    response = client.post("/internal/qa-sessions", json=open_request(model=CLAUDE_MODEL))

    assert response.status_code == 200
    assert "llm_key" not in response.text


def test_open_with_llm_key_keeps_it_out_of_the_response_and_run_config(client) -> None:
    body = open_request(
        model=CLAUDE_MODEL, llm_key={"service": "anthropic", "api_key": "sk-ant-user"}
    )

    response = client.post("/internal/qa-sessions", json=body)

    assert response.status_code == 200
    assert "sk-ant-user" not in response.text
    assert "llm_key" not in response.json()["run_config"]
    store = client.app.state.qa_session_store
    record = asyncio.run(store.load(response.json()["session_id"]))
    assert record.llm_key == USER_ANTHROPIC


def test_open_refuses_an_unknown_service(client) -> None:
    body = open_request(model=CLAUDE_MODEL, llm_key={"service": "gemini", "api_key": "k"})

    assert client.post("/internal/qa-sessions", json=body).status_code == 422


def test_open_refuses_gpt_without_an_openai_key(client) -> None:
    without = client.post("/internal/qa-sessions", json=open_request(model=GPT_MODEL))
    wrong_service = client.post(
        "/internal/qa-sessions",
        json=open_request(
            model=GPT_MODEL, llm_key={"service": "anthropic", "api_key": "sk-ant-user"}
        ),
    )
    opened = client.post(
        "/internal/qa-sessions",
        json=open_request(
            model=GPT_MODEL, llm_key={"service": "openai", "api_key": "sk-user-openai"}
        ),
    )

    assert without.status_code == 422
    assert "llm_key with service 'openai'" in without.text
    assert wrong_service.status_code == 422
    assert "sk-ant-user" not in wrong_service.text
    assert opened.status_code == 200
    assert "sk-user-openai" not in opened.text


def test_a_refused_open_does_not_echo_the_key(client) -> None:
    # A whole-body validator failure puts the whole body in the 422's `input`.
    body = open_request(
        model=CLAUDE_MODEL,
        reasoning={"max_tokens": 4096},
        llm_key={"service": "anthropic", "api_key": "sk-ant-user"},
    )

    response = client.post("/internal/qa-sessions", json=body)

    assert response.status_code == 422
    assert "sk-ant-user" not in response.text
    assert "**********" in response.text


class _ProbingService(QaExecutionService):
    """Records the key bound where the run and the receive loop do their work."""

    def __init__(self, store) -> None:
        super().__init__(store=store)
        self.seen: dict[str, LlmKey | None] = {}
        # Binds to a loop on first use, not here, so the server's loop can own it.
        self._delivered = asyncio.Event()

    async def run(self, session_id: str, send) -> None:
        self.seen["run"] = bound_llm_key()
        await self._delivered.wait()

    def deliver(self, session_id: str, raw: dict) -> bool:
        # `screen_verdict` and `screen_name` tasks are started from here.
        self.seen["receive"] = bound_llm_key()
        self._delivered.set()
        return True


def test_the_socket_binds_the_key_for_the_run_and_the_receive_loop() -> None:
    app = create_app()
    store = InMemoryQaSessionStore()
    service = _ProbingService(store)
    app.state.qa_session_service = service
    session_id, _ = asyncio.run(_open(service, llm_key=USER_ANTHROPIC))

    with TestClient(app).websocket_connect(f"/internal/qa-sessions/{session_id}") as socket:
        socket.send_json({"type": "PULSE"})
        with pytest.raises(Exception):
            socket.receive_json()

    assert service.seen == {"run": USER_ANTHROPIC, "receive": USER_ANTHROPIC}
    assert asyncio.run(store.load(session_id)) is None
