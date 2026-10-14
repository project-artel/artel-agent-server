"""The `claude/` route: Claude API calls through `langchain-anthropic`.

No test here reaches the network. Request bodies are read from
`ChatAnthropic._get_request_payload`, the dict langchain-anthropic hands to
`messages.create`, and model answers come from a fake parent class.
"""

import asyncio
from uuid import uuid4

import anthropic
import httpx
import pytest
from anthropic.types import CacheCreation, Usage
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.chat_models import _create_usage_metadata
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult, LLMResult
from pydantic import BaseModel

from app.llm.api_key import KeyRefreshOnUnauthorized, is_key_rejected_by_anthropic
from app.llm.chat_model import (
    CLAUDE_MAX_OUTPUT_TOKENS,
    ClaudeApiKeyMissingError,
    build_chat_model,
    select_structured_method,
    structured,
)
from app.llm.claude_api import ClaudeApiChat, forces_a_tool
from app.llm.models import ReasoningConfig, ReasoningEffort
from app.llm.usage import UsageBuffer, UsageCallback, set_usage_scope

OPUS = "claude/claude-opus-5-5"
SONNET = "claude/claude-sonnet-5-5"
HAIKU = "claude/claude-haiku-5-5"
FABLE = "claude/claude-fable-5-1"
CLAUDE_MODELS = [OPUS, SONNET, HAIKU, FABLE]

EFFORT_HIGH = ReasoningConfig(effort=ReasoningEffort.high)
DISABLED = ReasoningConfig(max_tokens=0)


class _FixedKeys:
    """Stands in for `LlmApiKeyProvider`: holds a Claude API key a test can change."""

    def __init__(self, anthropic_key: str | None) -> None:
        self.anthropic = anthropic_key
        self.invalidated = 0

    def get_anthropic_key(self) -> str | None:
        return self.anthropic

    def get_bedrock_key(self) -> str | None:
        return None

    def get_openrouter_key_or_placeholder(self) -> str:
        return "missing"

    def invalidate(self) -> None:
        self.invalidated += 1


@pytest.fixture
def keys(monkeypatch) -> _FixedKeys:
    fixed = _FixedKeys("sk-ant-test")
    monkeypatch.setattr("app.llm.chat_model.get_llm_api_key_provider", lambda: fixed)
    # `build_chat_model` is an `lru_cache`. Without this, two tests share a client.
    build_chat_model.cache_clear()
    yield fixed
    build_chat_model.cache_clear()


def _payload(model: ChatAnthropic, messages=None, **kwargs) -> dict:
    return model._get_request_payload(messages or [HumanMessage("hi")], **kwargs)


# --- route --------------------------------------------------------------------


@pytest.mark.parametrize("model_id", CLAUDE_MODELS)
def test_claude_ids_build_a_claude_api_client(keys, model_id) -> None:
    model = build_chat_model(model_id)

    assert isinstance(model, ClaudeApiChat)
    assert model.model == model_id.removeprefix("claude/")
    assert model.anthropic_api_key.get_secret_value() == "sk-ant-test"
    assert model.streaming is True
    assert model.max_tokens == CLAUDE_MAX_OUTPUT_TOKENS == 128_000
    # langchain-anthropic sends top-level `cache_control` only for this value.
    assert model._llm_type == "anthropic-chat"


def test_environment_variables_do_not_redirect_or_authenticate_the_call(
    keys, monkeypatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://elsewhere.test")
    monkeypatch.setenv("ANTHROPIC_API_URL", "http://elsewhere.test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-shell")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-from-shell")

    model = build_chat_model(OPUS)
    client = model._client

    assert str(client.base_url).rstrip("/") == "https://api.anthropic.com"
    assert client.api_key == "sk-ant-test"
    assert client.auth_token is None


def test_openrouter_anthropic_ids_stay_on_openrouter(keys) -> None:
    from langchain_openai import ChatOpenAI

    assert isinstance(build_chat_model("anthropic/claude-opus-5.5"), ChatOpenAI)


def test_no_stored_key_fails_before_any_client_is_built(keys, monkeypatch) -> None:
    keys.anthropic = None
    built = []
    monkeypatch.setattr(
        "app.llm.chat_model.ClaudeApiChat", lambda **kwargs: built.append(kwargs)
    )

    with pytest.raises(ClaudeApiKeyMissingError, match="admin page"):
        build_chat_model(OPUS)
    assert built == []


def test_a_changed_key_reaches_a_new_client_without_a_restart(keys) -> None:
    first = build_chat_model(OPUS)
    assert build_chat_model(OPUS) is first

    keys.anthropic = "sk-ant-new"
    second = build_chat_model(OPUS)

    assert second is not first
    assert second.anthropic_api_key.get_secret_value() == "sk-ant-new"


def test_a_401_drops_the_cached_key(keys) -> None:
    model = build_chat_model(OPUS)
    (refresh,) = [cb for cb in model.callbacks if isinstance(cb, KeyRefreshOnUnauthorized)]
    response = httpx.Response(
        401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )

    asyncio.run(
        refresh.on_llm_error(
            anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)
        )
    )

    assert keys.invalidated == 1
    assert refresh._rejected is is_key_rejected_by_anthropic


# --- request fields -----------------------------------------------------------


@pytest.mark.parametrize("model_id", CLAUDE_MODELS)
def test_an_effort_sends_adaptive_thinking_and_that_effort(keys, model_id) -> None:
    payload = _payload(build_chat_model(model_id, EFFORT_HIGH))

    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}


@pytest.mark.parametrize("model_id", CLAUDE_MODELS)
def test_no_reasoning_config_sends_neither_field(keys, model_id) -> None:
    payload = _payload(build_chat_model(model_id))

    assert "thinking" not in payload
    assert "output_config" not in payload


@pytest.mark.parametrize(
    ("model_id", "thinking", "output_config"),
    [
        # Thinking cannot be turned off on these two; `low` is the least they think.
        (OPUS, None, {"effort": "low"}),
        (FABLE, None, {"effort": "low"}),
        # Sonnet 5.5 refuses `disabled`; `between_tools` takes no other field.
        (SONNET, {"type": "between_tools"}, None),
        (HAIKU, {"type": "disabled"}, None),
    ],
)
def test_disabled_reasoning_is_mapped_per_model(
    keys, model_id, thinking, output_config
) -> None:
    payload = _payload(build_chat_model(model_id, DISABLED))

    assert payload.get("thinking") == thinking
    assert payload.get("output_config") == output_config


@pytest.mark.parametrize("model_id", CLAUDE_MODELS)
@pytest.mark.parametrize("reasoning", [None, EFFORT_HIGH, DISABLED])
def test_no_sampling_parameter_is_ever_sent(keys, model_id, reasoning) -> None:
    payload = _payload(build_chat_model(model_id, reasoning))

    assert "temperature" not in payload
    assert "top_p" not in payload
    assert "top_k" not in payload
    assert payload["max_tokens"] == 128_000
    assert "budget_tokens" not in str(payload)


def test_cache_prompt_sends_top_level_cache_control(keys) -> None:
    assert _payload(build_chat_model(OPUS, cache_prompt=True))["cache_control"] == {
        "type": "ephemeral"
    }
    assert "cache_control" not in _payload(build_chat_model(OPUS))


# --- tool choice and structured output ----------------------------------------


@pytest.mark.parametrize(
    ("tool_choice", "forced"),
    [
        (None, False),
        ("auto", False),
        ("none", False),
        ("any", True),
        ("lookup", True),
        ({"type": "any"}, True),
        ({"type": "tool", "name": "lookup"}, True),
        ({"type": "auto"}, False),
    ],
)
def test_forced_tool_choices_are_recognised(tool_choice, forced) -> None:
    assert forces_a_tool(tool_choice) is forced


class _Answer(BaseModel):
    verdict: str


def _tool_choice_sent(bound) -> object:
    return bound.kwargs.get("tool_choice")


@pytest.mark.parametrize("model_id", CLAUDE_MODELS)
@pytest.mark.parametrize("tool_choice", ["any", "_Answer", {"type": "any"}])
def test_bind_tools_never_forces_a_tool(keys, model_id, tool_choice) -> None:
    """`create_agent`'s `ToolStrategy` binds its answer tool with `tool_choice="any"`."""
    bound = build_chat_model(model_id).bind_tools([_Answer], tool_choice=tool_choice)

    assert _tool_choice_sent(bound) is None


def test_bind_tools_keeps_auto(keys) -> None:
    bound = build_chat_model(OPUS).bind_tools([_Answer], tool_choice="auto")

    assert _tool_choice_sent(bound) == {"type": "auto"}


@pytest.mark.parametrize("model_id", CLAUDE_MODELS)
def test_structured_output_uses_json_schema(keys, model_id) -> None:
    assert select_structured_method(model_id) == "json_schema"

    chain = structured(model_id, _Answer)
    bound = chain.first
    payload = _payload(build_chat_model(model_id), **bound.kwargs)

    assert payload["output_config"]["format"]["type"] == "json_schema"
    assert "tool_choice" not in payload
    assert "tools" not in payload


def test_structured_output_passes_no_strict_flag(keys, monkeypatch) -> None:
    from unittest.mock import MagicMock

    chat = MagicMock()
    monkeypatch.setattr("app.llm.chat_model.build_chat_model", lambda model: chat)

    structured(OPUS, _Answer)

    chat.with_structured_output.assert_called_once_with(_Answer, method="json_schema")


# --- usage --------------------------------------------------------------------


class _RecordingSender:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def __call__(self, payload: dict) -> None:
        self.payloads.append(payload)


@pytest.mark.parametrize(
    "cache_creation",
    [
        # What the Claude API sends today: the write split by TTL.
        CacheCreation(ephemeral_5m_input_tokens=3000, ephemeral_1h_input_tokens=0),
        # Without the split, only `cache_creation_input_tokens`.
        None,
    ],
)
def test_a_claude_call_is_priced_from_the_catalog(cache_creation) -> None:
    """The Claude API returns no cost, so the catalog prices the call.

    langchain-anthropic adds cache reads and cache writes into `input_tokens`. The
    receiving side counts cache writes outside `inputTokens`, so they are taken out.
    """
    usage = _create_usage_metadata(
        Usage(
            input_tokens=1000,
            output_tokens=200,
            cache_read_input_tokens=50_000,
            cache_creation_input_tokens=3000,
            cache_creation=cache_creation,
        )
    )
    assert usage["input_tokens"] == 54_000
    result = LLMResult(
        generations=[[ChatGeneration(message=AIMessage("done", usage_metadata=usage))]],
        llm_output={"model_name": "claude-opus-5-5"},
    )
    sender = _RecordingSender()
    buffer = UsageBuffer(base_url="http://orchestration.test", send=sender, flush_size=1)
    callback = UsageCallback(buffer, slug=OPUS, input_includes_cache_write=True)

    async def scenario() -> None:
        set_usage_scope("QA_RUN", 11)
        run_id = uuid4()
        await callback.on_chat_model_start({}, [], run_id=run_id)
        await callback.on_llm_end(result, run_id=run_id)

    asyncio.run(scenario())

    (record,) = sender.payloads[0]["records"]
    assert record["model"] == OPUS
    assert record["provider"] == "claude"
    assert record["inputTokens"] == 51_000
    assert record["cachedInputTokens"] == 50_000
    assert record["cacheWriteTokens"] == 3000
    assert record["outputTokens"] == 200
    # 1,000 x $4 + 50,000 x $0.20 + 3,000 x $5 + 200 x $20, per million tokens.
    assert record["costUsd"] == pytest.approx(0.033)
    assert record["costEstimated"] is True


def test_the_claude_client_reports_usage_under_the_catalog_id(keys) -> None:
    model = build_chat_model(OPUS)
    (usage,) = [cb for cb in model.callbacks if isinstance(cb, UsageCallback)]

    assert usage._slug == OPUS
    assert usage._input_includes_cache_write is True


# --- preserved thinking -------------------------------------------------------

SIGNATURE_1 = "sig-turn-1"
SIGNATURE_2 = "sig-turn-2"


def _assistant(signature: str, call_id: str, thinking: str = "") -> AIMessage:
    """An assistant turn the way langchain-anthropic stores one: thinking, then a tool call.

    `thinking` is empty by default because `display` defaults to `omitted` on these
    models; the block still carries a signature.
    """
    return AIMessage(
        content=[
            {"type": "thinking", "thinking": thinking, "signature": signature, "index": 0},
            {"type": "tool_use", "id": call_id, "name": "observe", "input": {}, "index": 1},
        ],
        tool_calls=[{"id": call_id, "name": "observe", "args": {}}],
        response_metadata={"model_provider": "anthropic"},
    )


class _Scripted(ClaudeApiChat):
    """`ClaudeApiChat` whose parent answers from a script instead of the network.

    The request body each call would have sent is recorded, so a test reads what
    left the process.
    """

    def __init__(self, answers: list[AIMessage], **kwargs) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_answers", list(answers))
        object.__setattr__(self, "_sent", [])

    def _record(self, messages, kwargs) -> AIMessage:
        self._sent.append(self._get_request_payload(messages, **kwargs))
        return self._answers.pop(0)


def _scripted(answers: list[AIMessage]) -> _Scripted:
    return _Scripted(
        answers,
        model="claude-opus-5-5",
        api_key="sk-ant-test",
        max_tokens=128_000,
        streaming=False,
    )


@pytest.fixture
def scripted_parent(monkeypatch):
    """Replaces `ChatAnthropic._agenerate` and `_astream` with the script."""

    async def agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._record(messages, kwargs))])

    async def astream(self, messages, stop=None, run_manager=None, **kwargs):
        answer = self._record(messages, kwargs)
        # Split the answer the way a stream arrives: the signature comes last and in
        # two pieces.
        thinking, tool_use = answer.content
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=[{"type": "thinking", "thinking": "", "index": 0}], id="msg_1"
            )
        )
        half = len(thinking["signature"]) // 2
        for piece in (thinking["signature"][:half], thinking["signature"][half:]):
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=[{"type": "thinking", "signature": piece, "index": 0}]
                )
            )
        yield ChatGenerationChunk(message=AIMessageChunk(content=[tool_use]))

    monkeypatch.setattr(ChatAnthropic, "_agenerate", agenerate)
    monkeypatch.setattr(ChatAnthropic, "_astream", astream)


def _thinking_blocks(payload: dict) -> list[dict]:
    return [
        block
        for message in payload["messages"]
        if message["role"] == "assistant"
        for block in message["content"]
        if block["type"] == "thinking"
    ]


def _loop_history() -> list:
    return [
        SystemMessage("You are a QA agent."),
        HumanMessage("Check the title screen."),
    ]


@pytest.mark.parametrize("stream", [False, True])
def test_an_unedited_tool_loop_sends_every_thinking_block_back(
    scripted_parent, stream
) -> None:
    """A two-turn tool loop keeps the first turn's thinking block, signature and all."""
    first = _assistant(SIGNATURE_1, "call_1")
    second = _assistant(SIGNATURE_2, "call_2")
    model = _scripted([first, second])

    async def scenario() -> None:
        history = _loop_history()
        if stream:
            async for _ in model._astream(list(history)):
                pass
        else:
            await model._agenerate(list(history))
        history += [first, ToolMessage("scene: title", tool_call_id="call_1")]
        await model._agenerate(list(history))

    asyncio.run(scenario())

    second_request = model._sent[1]
    assert _thinking_blocks(second_request) == [
        {"type": "thinking", "thinking": "", "signature": SIGNATURE_1}
    ]
    tool_use = second_request["messages"][1]["content"][1]
    assert tool_use["type"] == "tool_use" and tool_use["id"] == "call_1"


def test_a_rewritten_earlier_message_removes_the_thinking_after_it(scripted_parent) -> None:
    """`fold_context` rewrites an old tool result. Blocks bound to the old text go."""
    first = _assistant(SIGNATURE_1, "call_1")
    second = _assistant(SIGNATURE_2, "call_2")
    third = _assistant("sig-turn-3", "call_3")
    model = _scripted([first, second, third])
    full_view = ToolMessage("scene: title (full view)", tool_call_id="call_1")

    async def scenario() -> None:
        history = _loop_history()
        await model._agenerate(list(history))
        history += [first, full_view]
        await model._agenerate(list(history))
        history += [second, ToolMessage("scene: menu", tool_call_id="call_2")]
        # The fold replaces the first tool result with a placeholder.
        folded = [
            ToolMessage("[folded]", tool_call_id="call_1") if message is full_view else message
            for message in history
        ]
        await model._agenerate(folded)

    asyncio.run(scenario())

    third_request = model._sent[2]
    # `first` was produced before the edited message, so its prefix is unchanged.
    # `second` was produced after it, with the full view in front of it.
    assert [block["signature"] for block in _thinking_blocks(third_request)] == [SIGNATURE_1]
    second_turn = third_request["messages"][3]
    assert [block["type"] for block in second_turn["content"]] == ["tool_use"]


def test_a_removed_transient_message_removes_the_thinking_bound_to_it(
    scripted_parent,
) -> None:
    """`every_call` capture appends a screenshot to one request and does not keep it."""
    first = _assistant(SIGNATURE_1, "call_1")
    second = _assistant(SIGNATURE_2, "call_2")
    model = _scripted([first, second])

    async def scenario() -> None:
        history = _loop_history()
        await model._agenerate([*history, HumanMessage("[screenshot 1]")])
        history += [first, ToolMessage("scene: title", tool_call_id="call_1")]
        await model._agenerate([*history, HumanMessage("[screenshot 2]")])

    asyncio.run(scenario())

    assert _thinking_blocks(model._sent[1]) == []


def test_removal_is_repeated_on_every_later_request_and_new_blocks_stay(
    scripted_parent,
) -> None:
    """The same blocks go on every request after the edit, so the history stays fixed."""
    first = _assistant(SIGNATURE_1, "call_1")
    second = _assistant(SIGNATURE_2, "call_2")
    third = _assistant("sig-turn-3", "call_3")
    model = _scripted([first, second, third])

    async def scenario() -> None:
        history = _loop_history()
        await model._agenerate([*history, HumanMessage("[screenshot 1]")])
        history += [first, ToolMessage("scene: title", tool_call_id="call_1")]
        await model._agenerate(list(history))
        history += [second, ToolMessage("scene: menu", tool_call_id="call_2")]
        await model._agenerate(list(history))

    asyncio.run(scenario())

    # `first` lost its block on the second request and stays without it; `second`
    # was produced after that and keeps its block.
    assert _thinking_blocks(model._sent[1]) == []
    assert [block["signature"] for block in _thinking_blocks(model._sent[2])] == [SIGNATURE_2]


def test_a_block_this_process_never_produced_is_sent_unchanged(scripted_parent) -> None:
    model = _scripted([_assistant(SIGNATURE_2, "call_2")])
    history = [
        *_loop_history(),
        _assistant("sig-from-elsewhere", "call_1"),
        ToolMessage("scene: title", tool_call_id="call_1"),
    ]

    asyncio.run(model._agenerate(history))

    assert [block["signature"] for block in _thinking_blocks(model._sent[0])] == [
        "sig-from-elsewhere"
    ]


def test_a_changed_tool_set_removes_the_thinking_bound_to_the_old_one(
    scripted_parent,
) -> None:
    first = _assistant(SIGNATURE_1, "call_1")
    second = _assistant(SIGNATURE_2, "call_2")
    model = _scripted([first, second])
    tool_a = {"name": "observe", "description": "Look.", "input_schema": {"type": "object"}}
    tool_b = {"name": "press", "description": "Press.", "input_schema": {"type": "object"}}

    async def scenario() -> None:
        history = _loop_history()
        await model._agenerate(list(history), tools=[tool_a])
        history += [first, ToolMessage("scene: title", tool_call_id="call_1")]
        await model._agenerate(list(history), tools=[tool_a, tool_b])

    asyncio.run(scenario())

    assert _thinking_blocks(model._sent[1]) == []


def test_a_create_agent_tool_loop_keeps_thinking_and_forces_no_tool(scripted_parent) -> None:
    """The real loop: `create_agent` with a tool and a `ToolStrategy` answer.

    The second request carries the first turn's thinking block unchanged, and
    neither request forces a tool although `ToolStrategy` asks for `any`.
    """
    from langchain.agents import create_agent
    from langchain.agents.structured_output import ToolStrategy
    from langchain_core.tools import tool

    @tool
    def observe() -> str:
        """Look at the screen."""
        return "scene: title"

    final = AIMessage(
        content=[
            {"type": "thinking", "thinking": "", "signature": SIGNATURE_2, "index": 0},
            {
                "type": "tool_use",
                "id": "call_2",
                "name": "_Answer",
                "input": {"verdict": "pass"},
                "index": 1,
            },
        ],
        tool_calls=[{"id": "call_2", "name": "_Answer", "args": {"verdict": "pass"}}],
        response_metadata={"model_provider": "anthropic"},
    )
    model = _scripted([_assistant(SIGNATURE_1, "call_1"), final])
    agent = create_agent(
        model=model,
        tools=[observe],
        system_prompt="You are a QA agent.",
        response_format=ToolStrategy(schema=_Answer),
    )

    result = asyncio.run(agent.ainvoke({"messages": [HumanMessage("Check the title.")]}))

    assert result["structured_response"] == _Answer(verdict="pass")
    first_request, second_request = model._sent
    assert "tool_choice" not in first_request
    assert "tool_choice" not in second_request
    assert _thinking_blocks(second_request) == [
        {"type": "thinking", "thinking": "", "signature": SIGNATURE_1}
    ]
