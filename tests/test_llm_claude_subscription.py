"""The Claude subscription backend, with the Agent SDK boundary stubbed out.

Nothing here reaches the network or the `claude` CLI. What is worth testing is
everything on this side of `claude_agent_sdk.query`: which turns the request is
built from, which of the two history modes a call lands in, and what the answer
looks like coming back.
"""

import asyncio
import logging
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from app.config import Settings
from app.llm import claude_subscription
from app.llm.claude_subscription import (
    BoundTool,
    ChatClaudeSubscription,
    ClaudeAgentSdkMissingError,
    build_claude_subscription_chat_model,
    build_sdk_prompt,
    strip_mcp_tool_prefix,
    to_draft_07_schema,
    to_sdk_model_name,
    usage_metadata_from_sdk,
)
from app.llm.models import LLMModel, ReasoningConfig, ReasoningEffort

RED_PIXEL_DATA_URL = "data:image/png;base64,iVBORw0KGgo="


# --------------------------------------------------------------------------- #
# A stand-in for `claude_agent_sdk`
# --------------------------------------------------------------------------- #


@dataclass
class StubTextBlock:
    text: str


@dataclass
class StubToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class StubAssistantMessage:
    content: list[Any]


@dataclass
class StubResultMessage:
    usage: dict[str, Any] | None = None
    result: str | None = None
    structured_output: Any = None
    stop_reason: str | None = None


@dataclass
class StubToolPermissionContext:
    tool_use_id: str


class StubResultError(Exception):
    pass


@dataclass
class StubPermissionResultAllow:
    behavior: str = "allow"


@dataclass
class StubPermissionResultDeny:
    message: str = ""
    interrupt: bool = False


@dataclass
class StubOptions:
    """Mirrors the fields `_build_options` sets, so a test can read them back."""

    model: str | None = None
    system_prompt: str | None = None
    tools: list[str] = field(default_factory=list)
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    strict_mcp_config: bool = False
    setting_sources: list[str] | None = None
    skills: list[str] | None = None
    max_turns: int | None = None
    can_use_tool: Any = None
    effort: str | None = None
    output_format: dict[str, Any] | None = None


class StubSdk(types.ModuleType):
    """The slice of `claude_agent_sdk` this backend actually touches."""

    def __init__(self, messages: list[Any], raise_after_stream: bool = False) -> None:
        super().__init__("claude_agent_sdk")
        self.AssistantMessage = StubAssistantMessage
        self.ResultMessage = StubResultMessage
        self.ResultError = StubResultError
        self.PermissionResultAllow = StubPermissionResultAllow
        self.PermissionResultDeny = StubPermissionResultDeny
        self.ClaudeAgentOptions = StubOptions
        self.create_sdk_mcp_server = self._create_sdk_mcp_server
        self.tool = self._tool
        self.query = self._query
        self._messages = messages
        self._raise_after_stream = raise_after_stream
        self.sent_turns: list[dict[str, Any]] = []
        self.options: StubOptions | None = None
        self.declared_tools: list[tuple[str, str, dict[str, Any]]] = []
        # A tool call the fake CLI "decides" to make, routed through can_use_tool.
        self.tool_call_to_attempt: tuple[str, dict[str, Any], str] | None = None

    def _create_sdk_mcp_server(self, name: str, tools: list[Any]) -> dict[str, Any]:
        return {"name": name, "tools": tools}

    def _tool(self, name: str, description: str, input_schema: dict[str, Any]):
        self.declared_tools.append((name, description, input_schema))

        def decorate(handler):
            return {"name": name, "handler": handler}

        return decorate

    async def _query(self, *, prompt, options):
        self.options = options
        async for turn in prompt:
            self.sent_turns.append(turn)
        if self.tool_call_to_attempt is not None:
            name, arguments, tool_use_id = self.tool_call_to_attempt
            await options.can_use_tool(
                name, arguments, StubToolPermissionContext(tool_use_id=tool_use_id)
            )
        for message in self._messages:
            yield message
        if self._raise_after_stream:
            raise StubResultError("Reached maximum number of turns")


def install_stub_sdk(monkeypatch, sdk: StubSdk) -> StubSdk:
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    return sdk


def build_model(**overrides: Any) -> ChatClaudeSubscription:
    fields: dict[str, Any] = {
        "sdk_model_name": "claude-sonnet-5",
        "reported_model_name": "anthropic/claude-sonnet-5",
    }
    fields.update(overrides)
    return ChatClaudeSubscription(**fields)


# --------------------------------------------------------------------------- #
# Model names
# --------------------------------------------------------------------------- #


def test_an_anthropic_slug_becomes_the_bare_sdk_name() -> None:
    """The catalog carries OpenRouter slugs; the SDK takes the bare name, and the
    version's dot is a dash there."""
    assert to_sdk_model_name(LLMModel.claude_sonnet_5) == "claude-sonnet-5"
    assert to_sdk_model_name(LLMModel.claude_opus_4_8) == "claude-opus-4-8"


def test_a_non_anthropic_model_is_substituted_and_the_swap_is_logged(caplog) -> None:
    """A subscription cannot run GPT or Gemini. Substituting silently would leave
    nobody able to say which model produced a run's answers."""
    with caplog.at_level(logging.WARNING, logger=claude_subscription.__name__):
        substituted = to_sdk_model_name(LLMModel.gpt_5_6_luna)

    assert substituted == Settings(_env_file=None).claude_subscription_fallback_model
    assert "openai/gpt-5.6-luna" in caplog.text
    assert substituted in caplog.text


# --------------------------------------------------------------------------- #
# Schema conversion
# --------------------------------------------------------------------------- #


class Sighting(BaseModel):
    label: str
    confidence: float


class SightingReport(BaseModel):
    note: str | None = None
    sightings: list[Sighting]


def test_a_nested_pydantic_schema_is_inlined_for_draft_07() -> None:
    """`output_format` refuses draft 2020-12, and `model_json_schema()` emits it:
    the nested model arrives as `$defs` plus a `$ref` that has to be flattened."""
    converted = to_draft_07_schema(SightingReport.model_json_schema())

    assert "$defs" not in converted
    assert converted["properties"]["sightings"]["items"]["properties"].keys() == {
        "label",
        "confidence",
    }
    assert "$ref" not in repr(converted)


def test_tuple_positions_move_from_prefix_items_to_items() -> None:
    """draft-07 spells a tuple as a list-valued `items`; 2020-12 renamed it."""
    converted = to_draft_07_schema(
        {"type": "array", "prefixItems": [{"type": "string"}, {"type": "integer"}]}
    )

    assert converted == {
        "type": "array",
        "items": [{"type": "string"}, {"type": "integer"}],
    }


def test_a_self_referential_schema_is_refused_by_name() -> None:
    """Inlining a cycle never terminates. Naming the definition beats a hang."""
    with pytest.raises(ValueError, match="Node"):
        to_draft_07_schema(
            {
                "$defs": {
                    "Node": {
                        "type": "object",
                        "properties": {"child": {"$ref": "#/$defs/Node"}},
                    }
                },
                "$ref": "#/$defs/Node",
            }
        )


# --------------------------------------------------------------------------- #
# Message translation
# --------------------------------------------------------------------------- #


def test_the_system_message_leaves_the_turn_list() -> None:
    """The SDK takes the system prompt as its own option, not as a turn."""
    prompt = build_sdk_prompt(
        [SystemMessage(content="You are terse."), HumanMessage(content="Hello.")],
        tools_bound=False,
    )

    assert prompt.system_prompt == "You are terse."
    assert prompt.turns == (
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "Hello."}
        ]}},
    )


def test_history_without_bound_tools_keeps_real_tool_blocks() -> None:
    """With `tools=[]` a structured history passes, so it is sent at full fidelity."""
    prompt = build_sdk_prompt(
        [
            HumanMessage(content="Weather?"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_weather", "args": {"city": "Seoul"}, "id": "call-1"}
                ],
            ),
            ToolMessage(content="18C", tool_call_id="call-1"),
        ],
        tools_bound=False,
    )

    assistant_turn, tool_turn = prompt.turns[1], prompt.turns[2]
    assert assistant_turn["message"]["content"] == [
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "get_weather",
            "input": {"city": "Seoul"},
        }
    ]
    assert tool_turn["message"]["content"] == [
        {"type": "tool_result", "tool_use_id": "call-1", "content": "18C"}
    ]


def test_history_with_bound_tools_renders_tool_turns_as_text() -> None:
    """A structured `tool_use` block and a non-empty tool declaration cannot share
    one request — the CLI answers `400 due to tool use concurrency issues`. So the
    prior turns go in as text instead."""
    prompt = build_sdk_prompt(
        [
            HumanMessage(content="Weather?"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_weather", "args": {"city": "Seoul"}, "id": "call-1"}
                ],
            ),
            ToolMessage(content="18C", tool_call_id="call-1"),
        ],
        tools_bound=True,
    )

    blocks = [
        block
        for turn in prompt.turns
        for block in turn["message"]["content"]
    ]
    assert all(block["type"] == "text" for block in blocks)
    assert '<tool_call id="call-1" name="get_weather">{"city": "Seoul"}</tool_call>' in (
        blocks[1]["text"]
    )
    assert blocks[2]["text"] == '<tool_result id="call-1">18C</tool_result>'


def test_parallel_tool_results_share_one_user_turn() -> None:
    """Anthropic wants every result for one assistant turn in a single user turn;
    two turns in a row are refused."""
    prompt = build_sdk_prompt(
        [
            HumanMessage(content="Both, please."),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "a", "args": {}, "id": "call-1"},
                    {"name": "b", "args": {}, "id": "call-2"},
                ],
            ),
            ToolMessage(content="first", tool_call_id="call-1"),
            ToolMessage(content="second", tool_call_id="call-2"),
        ],
        tools_bound=False,
    )

    assert [turn["type"] for turn in prompt.turns] == ["user", "assistant", "user"]
    assert len(prompt.turns[2]["message"]["content"]) == 2


def test_a_capture_turn_becomes_an_anthropic_image_block() -> None:
    """`app/agents/qa/vision.py` emits LangChain's OpenAI-style `image_url` data
    URL; Anthropic takes the media type and the base64 payload apart."""
    prompt = build_sdk_prompt(
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": "What colour?"},
                    {"type": "image_url", "image_url": {"url": RED_PIXEL_DATA_URL}},
                ]
            )
        ],
        tools_bound=False,
    )

    assert prompt.turns[0]["message"]["content"][1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "iVBORw0KGgo=",
        },
    }


def test_a_request_ending_on_an_assistant_turn_gets_a_user_turn() -> None:
    """Anthropic reads a trailing assistant turn as "keep writing this message",
    which is not what a LangChain caller asked for."""
    prompt = build_sdk_prompt(
        [HumanMessage(content="Hi."), AIMessage(content="Hello.")], tools_bound=False
    )

    assert prompt.turns[-1]["type"] == "user"


def test_a_call_with_no_message_to_answer_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one non-system message"):
        build_sdk_prompt([SystemMessage(content="Only rules.")], tools_bound=False)


# --------------------------------------------------------------------------- #
# Reading the answer back
# --------------------------------------------------------------------------- #


def test_the_mcp_prefix_is_stripped_from_a_tool_name() -> None:
    """The SDK reports `mcp__artel__capture`; LangChain only knows `capture`."""
    assert strip_mcp_tool_prefix("mcp__artel__capture") == "capture"
    assert strip_mcp_tool_prefix("StructuredOutput") == "StructuredOutput"


def test_usage_counts_cached_input_tokens_into_the_input_total() -> None:
    """Anthropic reports cache reads and writes beside `input_tokens`; LangChain's
    `input_tokens` is the whole prompt, and `app/llm/usage.py` reads the breakdown
    out of `input_token_details`."""
    usage = usage_metadata_from_sdk(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 40,
        }
    )

    assert usage == {
        "input_tokens": 440,
        "output_tokens": 20,
        "total_tokens": 460,
        "input_token_details": {"cache_read": 300, "cache_creation": 40},
    }


def test_a_plain_answer_carries_its_text_and_usage(monkeypatch) -> None:
    sdk = install_stub_sdk(
        monkeypatch,
        StubSdk(
            [
                StubAssistantMessage(content=[StubTextBlock(text="391")]),
                StubResultMessage(
                    usage={"input_tokens": 290, "output_tokens": 7},
                    result="391",
                    stop_reason="end_turn",
                ),
            ]
        ),
    )
    model = build_model()

    answer = asyncio.run(model.ainvoke([HumanMessage(content="17 * 23?")]))

    assert answer.content == "391"
    assert answer.usage_metadata["input_tokens"] == 290
    assert answer.response_metadata["model_name"] == "anthropic/claude-sonnet-5"
    assert sdk.options.tools == []
    assert sdk.options.max_turns is None


def test_the_five_cost_options_are_always_set(monkeypatch) -> None:
    """Without them the whole Claude Code harness prompt and every built-in tool
    definition ride along: 13,148 input tokens for a question that costs 290 with
    them. They also stop the developer's own `.claude` config and MCP servers from
    leaking into the request."""
    sdk = install_stub_sdk(monkeypatch, StubSdk([StubResultMessage(result="ok")]))

    asyncio.run(build_model().ainvoke([HumanMessage(content="Hi.")]))

    assert sdk.options.tools == []
    assert sdk.options.mcp_servers == {}
    assert sdk.options.strict_mcp_config is True
    assert sdk.options.setting_sources == []
    assert sdk.options.skills == []


def test_replayed_assistant_turns_are_not_mistaken_for_the_answer(monkeypatch) -> None:
    """The SDK echoes an injected assistant turn back as an `AssistantMessage`.
    Counting them is what keeps the echo out of the answer."""
    install_stub_sdk(
        monkeypatch,
        StubSdk(
            [
                StubAssistantMessage(content=[StubTextBlock(text="Blue.")]),
                StubAssistantMessage(content=[StubTextBlock(text="Red.")]),
                StubResultMessage(result="Red."),
            ]
        ),
    )

    answer = asyncio.run(
        build_model().ainvoke(
            [
                HumanMessage(content="Colour?"),
                AIMessage(content="Blue."),
                HumanMessage(content="And now?"),
            ]
        )
    )

    assert answer.content == "Red."


def test_a_bound_tool_comes_back_as_a_langchain_tool_call(monkeypatch) -> None:
    """The tool is declared as an in-process MCP server and denied with
    `interrupt=True`, so the SDK reports the call without running the body — and
    then raises `ResultError` because the turn never finished."""
    sdk = install_stub_sdk(
        monkeypatch,
        StubSdk([StubResultMessage(stop_reason="tool_use")], raise_after_stream=True),
    )
    sdk.tool_call_to_attempt = (
        "mcp__artel__get_weather",
        {"city": "Seoul"},
        "toolu_01",
    )
    model = build_model(
        bound_tools=(
            BoundTool(
                name="get_weather",
                description="Weather for a city.",
                input_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            ),
        )
    )

    answer = asyncio.run(model.ainvoke([HumanMessage(content="Weather in Seoul?")]))

    assert answer.tool_calls == [
        {
            "name": "get_weather",
            "args": {"city": "Seoul"},
            "id": "toolu_01",
            "type": "tool_call",
        }
    ]
    assert sdk.options.tools == ["mcp__artel__get_weather"]
    assert sdk.options.max_turns == 1
    assert sdk.declared_tools[0][0] == "get_weather"


def test_the_structured_output_tool_is_not_denied(monkeypatch) -> None:
    """`output_format` makes the SDK call a `StructuredOutput` tool of its own.
    Denying that one would mean structured output never arrives."""
    sdk = install_stub_sdk(monkeypatch, StubSdk([StubResultMessage(result="ok")]))
    model = build_model(
        bound_tools=(
            BoundTool(name="probe", description="probe", input_schema={"type": "object"}),
        )
    )
    asyncio.run(model.ainvoke([HumanMessage(content="Hi.")]))

    decision = asyncio.run(
        sdk.options.can_use_tool(
            "StructuredOutput", {"answer": 1}, StubToolPermissionContext("toolu_02")
        )
    )

    assert isinstance(decision, StubPermissionResultAllow)


def test_a_failure_with_no_tool_call_still_raises(monkeypatch) -> None:
    """`ResultError` is only expected when a denied tool call ended the turn. Any
    other one is a real failure and must not be swallowed."""
    install_stub_sdk(monkeypatch, StubSdk([], raise_after_stream=True))

    with pytest.raises(StubResultError):
        asyncio.run(build_model().ainvoke([HumanMessage(content="Hi.")]))


def test_structured_output_arrives_as_the_requested_model(monkeypatch) -> None:
    install_stub_sdk(
        monkeypatch,
        StubSdk(
            [
                StubResultMessage(
                    structured_output={
                        "note": "one sighting",
                        "sightings": [{"label": "red", "confidence": 0.9}],
                    }
                )
            ]
        ),
    )
    chain = build_model().with_structured_output(
        SightingReport, method="json_schema", strict=True
    )

    report = asyncio.run(chain.ainvoke([HumanMessage(content="What do you see?")]))

    assert isinstance(report, SightingReport)
    assert report.sightings[0].label == "red"


def test_json_mode_takes_the_same_route_as_json_schema(monkeypatch) -> None:
    """The Agent SDK has one structured-output path. `json_mode` exists on the
    OpenRouter side for models without strict json_schema, and means nothing here."""
    sdk = install_stub_sdk(
        monkeypatch, StubSdk([StubResultMessage(structured_output={"sightings": []})])
    )
    chain = build_model().with_structured_output(SightingReport, method="json_mode")

    asyncio.run(chain.ainvoke([HumanMessage(content="Anything?")]))

    assert sdk.options.output_format["type"] == "json_schema"
    assert "$defs" not in sdk.options.output_format["schema"]


def test_an_unknown_structured_output_method_is_refused() -> None:
    with pytest.raises(ValueError, match="Unsupported structured output method"):
        build_model().with_structured_output(SightingReport, method="grammar")


# --------------------------------------------------------------------------- #
# The factory
# --------------------------------------------------------------------------- #


def test_the_factory_maps_reasoning_effort_onto_the_sdk_option() -> None:
    build_claude_subscription_chat_model.cache_clear()
    try:
        model = build_claude_subscription_chat_model(
            LLMModel.claude_sonnet_5, ReasoningConfig(effort=ReasoningEffort.high)
        )
    finally:
        build_claude_subscription_chat_model.cache_clear()

    assert model.effort == "high"
    assert model.sdk_model_name == "claude-sonnet-5"


def test_the_factory_drops_a_token_budget_it_cannot_express(caplog) -> None:
    """`reasoning.max_tokens` has no place in `ClaudeAgentOptions`. Every model that
    asks for it is non-Anthropic and is already being substituted, so the call
    proceeds and says what it dropped."""
    build_claude_subscription_chat_model.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger=claude_subscription.__name__):
            model = build_claude_subscription_chat_model(
                LLMModel.gemini_2_5_pro, ReasoningConfig(max_tokens=2048)
            )
    finally:
        build_claude_subscription_chat_model.cache_clear()

    assert model.effort is None
    assert "reasoning.max_tokens=2048" in caplog.text


def test_cache_prompt_is_accepted_and_ignored() -> None:
    """The signature has to match `build_chat_model` or every call site would have
    to know which backend it is talking to. The SDK has no cache breakpoint."""
    build_claude_subscription_chat_model.cache_clear()
    try:
        cached = build_claude_subscription_chat_model(
            LLMModel.claude_sonnet_5, None, True
        )
    finally:
        build_claude_subscription_chat_model.cache_clear()

    assert isinstance(cached, ChatClaudeSubscription)


def test_a_missing_sdk_says_what_to_install(monkeypatch) -> None:
    """`app.llm` must import without this dev-only package, so the failure can only
    happen at call time — and it has to name the fix."""
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    with pytest.raises(ClaudeAgentSdkMissingError, match="claude-agent-sdk"):
        asyncio.run(build_model().ainvoke([HumanMessage(content="Hi.")]))


def test_the_sync_path_refuses_instead_of_pretending(monkeypatch) -> None:
    """Every model call in this server is awaited. A sync path would have to run the
    SDK's subprocess on a loop of its own inside a FastAPI worker."""
    install_stub_sdk(monkeypatch, StubSdk([StubResultMessage(result="ok")]))

    with pytest.raises(NotImplementedError, match="async-only"):
        build_model().invoke([HumanMessage(content="Hi.")])


def test_an_exported_api_key_is_reported_because_it_outranks_the_subscription(
    monkeypatch, caplog
) -> None:
    """This backend exists to spend the subscription rather than money. The `claude`
    CLI prefers an exported credential over the logged-in session, so a key left in
    the environment quietly puts the calls back on a billed account — the one thing
    the caller was trying to avoid, and invisible until the invoice."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    build_claude_subscription_chat_model.cache_clear()

    with caplog.at_level(logging.WARNING):
        build_claude_subscription_chat_model(LLMModel.claude_sonnet_5)

    build_claude_subscription_chat_model.cache_clear()
    assert "ANTHROPIC_API_KEY" in caplog.text


def test_nothing_is_said_when_only_the_subscription_can_answer(
    monkeypatch, caplog
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    build_claude_subscription_chat_model.cache_clear()

    with caplog.at_level(logging.WARNING):
        build_claude_subscription_chat_model(LLMModel.claude_sonnet_5)

    build_claude_subscription_chat_model.cache_clear()
    assert "billed" not in caplog.text
