"""이 머신에 로그인된 `claude` CLI credential 로 도는 LLM backend.

OpenRouter API key 에 credit 이 없을 때도 서버를 그대로 돌려보기 위한 로컬 테스트
경로다. `claude-agent-sdk` 가 `claude` CLI 를 subprocess 로 띄우고, CLI 는 이미
로그인된 구독 credential 을 쓰므로 API key 가 필요 없다. 사용량은 달러가 아니라
구독의 5시간 window 에서 빠진다.

## 반드시 깎아야 하는 다섯 개 option

`tools=[]`, `mcp_servers={}`, `strict_mcp_config=True`, `setting_sources=[]`,
`skills=[]` 를 주지 않으면 Claude Code harness 의 system prompt 와 built-in tool
정의가 통째로 앞에 붙는다. 실측(claude-agent-sdk 0.2.151, `claude` CLI 2.1.258):
`17 * 23` 한 번이 13,148 input token, 다섯 개를 주면 290 token. 45배다.
`setting_sources=[]` 와 `strict_mcp_config=True` 는 개발자 머신의 `.claude` 설정과
MCP server 목록이 요청에 딸려 들어오는 것도 같이 막는다.

## 두 가지 mode 로 갈라지는 이유

구조화된 `tool_use` block 을 history 로 되먹이면서 tool 을 선언하면 CLI 가
`API Error: 400 due to tool use concurrency issues` 로 죽는다. tool 이름을 선언
목록 밖의 것으로 바꿔도 똑같다 — 이름이 아니라 block 종류가 문제다. 그래서:

- **tool 이 bind 되지 않은 호출** — history 를 진짜 `tool_use`/`tool_result` block
  으로 싣는다. `output_format` 으로 structured output 을 받는다.
- **tool 이 bind 된 호출** — tool 을 in-process MCP server 로 선언하고, 과거의 tool
  호출과 결과는 `<tool_call>`/`<tool_result>` 텍스트로 풀어 싣는다. fidelity 를
  잃는다(prompt caching 이 걸리지 않고 모델이 history 를 읽는 정확도가 떨어진다).
  로컬 테스트용 backend 라서 받아들인다.

두 번째 mode 에서 tool 호출을 받아오는 방법은 `can_use_tool` 이다. tool 을 실행하지
않고 이름과 인자만 받아야 하므로, callback 이 호출을 기록하고
`PermissionResultDeny(interrupt=True)` 를 돌려준다. tool body 는 절대 돌지 않는다.
"""

import json
import logging
import os
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from operator import itemgetter
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import (
    Runnable,
    RunnableLambda,
    RunnableMap,
    RunnablePassthrough,
)
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

from app.config import get_settings
from app.llm.models import (
    LLMModel,
    LLMProvider,
    ReasoningConfig,
    get_model_spec,
    validate_reasoning,
)
from app.llm.usage import UsageCallback

logger = logging.getLogger(__name__)

# bind 된 tool 을 담는 in-process MCP server 의 이름. SDK 는 tool 이름을
# `mcp__<server>__<tool>` 로 보고하므로, LangChain 에 돌려주기 전에 이 접두사를
# 떼어내야 원래 tool 이름이 된다.
MCP_SERVER_NAME = "artel"
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

# `output_format` 을 주면 SDK 가 이 이름의 tool 을 내부적으로 하나 만들어 쓴다.
# 우리 tool 이 아니므로 `can_use_tool` 에서 막으면 안 된다.
STRUCTURED_OUTPUT_TOOL_NAME = "StructuredOutput"

# JSON Schema draft 2020-12 에만 있는 keyword 들. Agent SDK 의 `output_format` 은
# draft-07 만 받고, Pydantic 의 `model_json_schema()` 는 2020-12 를 낸다.
_DRAFT_2020_ONLY_KEYWORDS = frozenset(
    {
        "$defs",
        "$schema",
        "$id",
        "$anchor",
        "$dynamicRef",
        "$dynamicAnchor",
        "$comment",
        "unevaluatedProperties",
        "unevaluatedItems",
        "dependentSchemas",
        "dependentRequired",
    }
)


class ClaudeAgentSdkMissingError(RuntimeError):
    """`claude-agent-sdk` 없이 이 backend 를 켰을 때."""


def _import_claude_agent_sdk():
    """`claude_agent_sdk` 를 그 자리에서 import 한다.

    module import 시점이 아니라 호출 시점에 import 하는 이유: 이것은 로컬 테스트용
    dev dependency 라서, `app.llm` 을 import 하는 것만으로 설치를 요구하면 배포
    이미지가 쓰지도 않는 패키지를 들고 있어야 한다.
    """
    try:
        import claude_agent_sdk
    except ImportError as error:
        raise ClaudeAgentSdkMissingError(
            "LLM_BACKEND=claude_subscription needs the `claude-agent-sdk` package "
            "and a `claude` CLI that is already logged in. Install it with "
            "`python -m pip install -e \".[dev]\"`, then run `claude` once to log in."
        ) from error
    return claude_agent_sdk


# --------------------------------------------------------------------------- #
# 모델 이름
# --------------------------------------------------------------------------- #


def to_sdk_model_name(model: LLMModel) -> str:
    """`LLMModel` 의 OpenRouter slug 를 Agent SDK 가 받는 맨 모델 이름으로.

    `anthropic/claude-opus-4.8` -> `claude-opus-4-8`. Anthropic 이 아닌 slug 는
    구독으로 돌릴 수 없으므로 설정된 대체 모델로 바꾸고, 바꿨다는 사실을 warning 으로
    남긴다. 조용히 바꾸면 어떤 모델이 답했는지 아무도 모른다.
    """
    if get_model_spec(model).provider is not LLMProvider.anthropic:
        fallback = get_settings().claude_subscription_fallback_model
        logger.warning(
            "[claude-subscription] '%s' cannot run on a Claude subscription; "
            "this call is answered by '%s' instead",
            model.value,
            fallback,
        )
        return fallback
    _, _, bare_name = model.value.partition("/")
    return bare_name.replace(".", "-")


# --------------------------------------------------------------------------- #
# JSON Schema draft-07 변환
# --------------------------------------------------------------------------- #


def to_draft_07_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """draft 2020-12 schema 를 Agent SDK 가 받는 draft-07 로 바꾼다.

    Pydantic 의 `model_json_schema()` 는 중첩 모델을 `$defs` + `$ref` 로 내는데
    `output_format` 은 그것을 거절한다. `$ref` 를 전부 그 자리에 펼치고, 2020-12
    에만 있는 keyword 를 떼고, `prefixItems` 를 draft-07 의 tuple 형태인 배열
    `items` 로 바꾼다.
    """
    definitions = dict(schema.get("$defs") or schema.get("definitions") or {})
    return _rewrite_schema_node(schema, definitions, ())


def _rewrite_schema_node(
    node: Any, definitions: Mapping[str, Any], resolving: tuple[str, ...]
) -> Any:
    if isinstance(node, list):
        return [_rewrite_schema_node(item, definitions, resolving) for item in node]
    if not isinstance(node, Mapping):
        return node

    reference = node.get("$ref")
    if isinstance(reference, str):
        return _inline_reference(node, reference, definitions, resolving)

    rewritten: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DRAFT_2020_ONLY_KEYWORDS:
            continue
        # draft-07 은 tuple 을 `items: [schema, ...]` 로 쓴다. 2020-12 가 그 자리를
        # `prefixItems` 로 옮겼을 뿐 뜻은 같다.
        target_key = "items" if key == "prefixItems" else key
        rewritten[target_key] = _rewrite_schema_node(value, definitions, resolving)
    return rewritten


def _inline_reference(
    node: Mapping[str, Any],
    reference: str,
    definitions: Mapping[str, Any],
    resolving: tuple[str, ...],
) -> dict[str, Any]:
    name = reference.rsplit("/", 1)[-1]
    if name in resolving:
        # 스스로를 가리키는 모델은 펼치면 끝나지 않는다. 무한 재귀로 죽는 것보다
        # 어느 정의가 문제인지 말하고 멈추는 편이 낫다.
        raise ValueError(
            f"Cannot inline the recursive schema definition '{name}' for draft-07; "
            "the Claude subscription backend needs a schema with no self-reference."
        )
    if name not in definitions:
        raise ValueError(f"Schema reference '{reference}' has no definition.")

    inlined = _rewrite_schema_node(definitions[name], definitions, resolving + (name,))
    # `$ref` 옆에 붙은 형제 key (`description`, `default` 등) 는 draft-07 에서 무시되지만,
    # 펼친 뒤에는 뜻이 살아나므로 같이 옮긴다. 펼친 정의를 덮어쓰지는 않는다.
    siblings = {
        key: _rewrite_schema_node(value, definitions, resolving)
        for key, value in node.items()
        if key != "$ref" and key not in _DRAFT_2020_ONLY_KEYWORDS
    }
    return {**siblings, **inlined} if isinstance(inlined, dict) else inlined


def _schema_as_dict(schema: Any) -> dict[str, Any]:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    if isinstance(schema, Mapping):
        return dict(schema)
    raise TypeError(
        f"Cannot build a JSON schema from {schema!r}; pass a Pydantic model or a dict."
    )


# --------------------------------------------------------------------------- #
# 요청으로 나가는 모양
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BoundTool:
    """`bind_tools` 로 들어온 tool 하나. MCP server 로 선언될 재료다."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class SdkPrompt:
    """SDK 의 streaming input 으로 나갈 한 요청."""

    system_prompt: str | None
    turns: tuple[dict[str, Any], ...]

    @property
    def replayed_assistant_turn_count(self) -> int:
        """되먹인 assistant turn 의 개수.

        SDK 는 되먹인 assistant turn 을 `AssistantMessage` 로 그대로 다시 돌려준다.
        그 개수만큼 건너뛰어야 새 답과 메아리를 헷갈리지 않는다.
        """
        return sum(1 for turn in self.turns if turn["type"] == "assistant")


@dataclass(frozen=True)
class RecordedToolCall:
    """`can_use_tool` 이 붙잡은 tool 호출 하나."""

    id: str
    name: str
    arguments: dict[str, Any]


def build_sdk_prompt(
    messages: Sequence[BaseMessage], *, tools_bound: bool
) -> SdkPrompt:
    """LangChain message 목록을 SDK 의 system prompt 와 turn 목록으로 나눈다."""
    system_texts: list[str] = []
    turns: list[dict[str, Any]] = []

    for message in messages:
        if isinstance(message, SystemMessage):
            system_texts.append(_flatten_text(message.content))
            continue
        role, blocks = _turn_for_message(message, tools_bound=tools_bound)
        if blocks:
            _append_turn(turns, role, blocks)

    if not turns:
        raise ValueError("A Claude subscription call needs at least one non-system message.")
    if turns[-1]["type"] != "user":
        # Anthropic 은 assistant turn 으로 끝나는 요청을 "이어서 써라" 로 읽는다.
        # LangChain 쪽에서 이 모양이 오는 일은 드물지만, 오면 요청이 400 으로 죽는
        # 대신 다음 turn 을 달라고 명시적으로 말한다.
        _append_turn(turns, "user", [{"type": "text", "text": "Continue."}])

    return SdkPrompt(
        system_prompt="\n\n".join(text for text in system_texts if text) or None,
        turns=tuple(turns),
    )


def _append_turn(
    turns: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]
) -> None:
    """같은 role 이 연달아 오면 한 turn 으로 합친다.

    병렬 tool 호출의 결과는 `ToolMessage` 여러 개로 오는데, Anthropic 은 그것들이
    한 user turn 안에 있기를 요구한다. turn 을 따로 내면 요청이 거절된다.
    """
    if turns and turns[-1]["type"] == role:
        turns[-1]["message"]["content"].extend(blocks)
        return
    turns.append({"type": role, "message": {"role": role, "content": blocks}})


def _turn_for_message(
    message: BaseMessage, *, tools_bound: bool
) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(message, ToolMessage):
        return "user", _tool_result_blocks(message, tools_bound=tools_bound)
    if isinstance(message, AIMessage):
        return "assistant", _assistant_blocks(message, tools_bound=tools_bound)
    return "user", _content_blocks(message.content)


def _assistant_blocks(
    message: AIMessage, *, tools_bound: bool
) -> list[dict[str, Any]]:
    blocks = _content_blocks(message.content)
    for call in message.tool_calls or ():
        rendered = json.dumps(call.get("args") or {}, ensure_ascii=False)
        if tools_bound:
            # tool 이 선언된 요청에는 `tool_use` block 을 실을 수 없다. 텍스트로 푼다.
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"<tool_call id=\"{call.get('id', '')}\" "
                        f"name=\"{call.get('name', '')}\">{rendered}</tool_call>"
                    ),
                }
            )
            continue
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id", ""),
                "name": call.get("name", ""),
                "input": call.get("args") or {},
            }
        )
    return blocks


def _tool_result_blocks(
    message: ToolMessage, *, tools_bound: bool
) -> list[dict[str, Any]]:
    text = _flatten_text(message.content)
    if tools_bound:
        return [
            {
                "type": "text",
                "text": f'<tool_result id="{message.tool_call_id}">{text}</tool_result>',
            }
        ]
    return [
        {
            "type": "tool_result",
            "tool_use_id": message.tool_call_id,
            "content": text,
        }
    ]


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    """LangChain 의 message content 를 Anthropic content block 으로."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []

    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part:
                blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, Mapping):
            continue
        kind = part.get("type")
        if kind == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif kind == "image_url":
            blocks.append(_image_block(part))
        elif kind == "image":
            blocks.append(dict(part))
    return blocks


def _image_block(part: Mapping[str, Any]) -> dict[str, Any]:
    """LangChain 의 OpenAI 모양 `image_url` 을 Anthropic 의 `image` block 으로.

    이 저장소의 vision 경로(`app/agents/qa/vision.py`)는 capture 를
    `data:image/png;base64,...` 로 싣는다. Anthropic 은 media type 과 base64 를
    따로 받는다.
    """
    url = part.get("image_url")
    if isinstance(url, Mapping):
        url = url.get("url", "")
    url = url or ""

    if not url.startswith("data:"):
        return {"type": "image", "source": {"type": "url", "url": url}}

    header, _, encoded = url.partition(",")
    media_type = header[len("data:") :].split(";", 1)[0] or "image/png"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": encoded},
    }


def _flatten_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, Mapping) and part.get("type") == "text":
            parts.append(part.get("text", ""))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 응답 읽기
# --------------------------------------------------------------------------- #


def strip_mcp_tool_prefix(name: str) -> str:
    """`mcp__artel__capture` -> `capture`.

    SDK 는 MCP tool 을 `mcp__<server>__<tool>` 로 보고한다. LangChain 은 자기가 준
    이름을 그대로 돌려받아야 그 tool 을 찾는다.
    """
    return name[len(MCP_TOOL_PREFIX) :] if name.startswith(MCP_TOOL_PREFIX) else name


def usage_metadata_from_sdk(usage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """`ResultMessage.usage` 를 LangChain 의 `usage_metadata` 로.

    Anthropic 의 `input_tokens` 는 cache 로 읽은 것과 쓴 것을 빼고 센다. LangChain 의
    `input_tokens` 는 전부 합한 값이므로 더해서 싣고, 내역은
    `input_token_details` 로 남긴다 — `app/llm/usage.py` 가 거기서 읽는다.

    돈은 싣지 않는다. `ResultMessage.total_cost_usd` 는 client 쪽 추정치이고, 구독
    사용량은 애초에 달러로 청구되지 않으므로 실제 비용이 아니다.
    """
    if not usage:
        return None

    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    input_tokens = int(usage.get("input_tokens") or 0) + cache_read + cache_creation
    output_tokens = int(usage.get("output_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_token_details": {
            "cache_read": cache_read,
            "cache_creation": cache_creation,
        },
    }


@dataclass(frozen=True)
class SdkTurn:
    """SDK 한 번의 호출에서 건져낸 것."""

    text: str
    tool_calls: tuple[RecordedToolCall, ...]
    structured_output: Any
    usage: Mapping[str, Any] | None
    stop_reason: str | None


def _to_ai_message(turn: SdkTurn, reported_model_name: str) -> AIMessage:
    additional_kwargs: dict[str, Any] = {}
    if turn.structured_output is not None:
        additional_kwargs["structured_output"] = turn.structured_output
    return AIMessage(
        content=turn.text,
        tool_calls=[
            {
                "name": call.name,
                "args": call.arguments,
                "id": call.id,
                "type": "tool_call",
            }
            for call in turn.tool_calls
        ],
        additional_kwargs=additional_kwargs,
        usage_metadata=usage_metadata_from_sdk(turn.usage),
        response_metadata={
            "model_name": reported_model_name,
            "stop_reason": turn.stop_reason,
        },
    )


# --------------------------------------------------------------------------- #
# 모델
# --------------------------------------------------------------------------- #


class ChatClaudeSubscription(BaseChatModel):
    """`claude` CLI 구독 credential 로 답하는 chat model.

    비동기 경로만 있다. 이 저장소의 모든 모델 호출은 `ainvoke`/`astream` 이고,
    동기 경로를 흉내내려면 SDK 의 subprocess 를 돌릴 event loop 를 새로 띄워야 하는데
    그것은 FastAPI worker 안에서 하면 안 되는 일이다. 동기 호출이 생기면 여기서
    바로 실패하는 편이 낫다.
    """

    sdk_model_name: str
    """Agent SDK 에 그대로 넘길 맨 모델 이름 (`claude-sonnet-5`)."""

    reported_model_name: str
    """사용량 기록에 남길 이름. 실제로 답한 모델이라야 대체가 드러난다."""

    effort: str | None = None
    bound_tools: tuple[BoundTool, ...] = ()
    output_format: dict[str, Any] | None = None

    @property
    def _llm_type(self) -> str:
        return "claude-subscription"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.reported_model_name, "effort": self.effort}

    def _replace(self, **updates: Any) -> "ChatClaudeSubscription":
        return self.model_copy(update=updates)

    # -- binding ------------------------------------------------------------ #

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ChatClaudeSubscription":
        """`create_agent` 가 이 모델을 쓰려면 있어야 하는 입구.

        `tool_choice` 같은 나머지 인자는 받아만 두고 버린다. Agent SDK 에는 tool 선택을
        강제할 자리가 없고, 여기서 거절하면 `create_agent` 가 아예 만들어지지 않는다.
        """
        if kwargs:
            logger.debug(
                "[claude-subscription] bind_tools ignored %s; the Agent SDK has no "
                "equivalent",
                sorted(kwargs),
            )
        return self._replace(bound_tools=tuple(_to_bound_tool(tool) for tool in tools))

    def with_structured_output(
        self,
        schema: Any,
        *,
        method: str = "json_schema",
        strict: bool | None = None,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable:
        """`output_format` 으로 가는 structured output.

        `method` 와 `strict` 는 받아만 둔다. 이 둘은 OpenRouter 경로에서 모델이 strict
        json_schema 를 지원하는지 아닌지를 고르는 값인데(`select_structured_method`),
        Agent SDK 에는 갈래가 하나뿐이다 — `output_format` 은 늘 schema 를 받고 늘
        파싱된 dict 를 돌려준다. `json_mode` 로 불려도 같은 길로 간다.
        """
        if method not in {"json_schema", "json_mode", "function_calling"}:
            raise ValueError(
                f"Unsupported structured output method '{method}' for the Claude "
                "subscription backend."
            )
        if kwargs:
            logger.debug(
                "[claude-subscription] with_structured_output ignored %s",
                sorted(kwargs),
            )

        model = self._replace(
            output_format={
                "type": "json_schema",
                "schema": to_draft_07_schema(_schema_as_dict(schema)),
            }
        )
        parser = RunnableLambda(lambda message: _parse_structured(message, schema))
        if not include_raw:
            return model | parser

        parsed = RunnablePassthrough.assign(
            parsed=itemgetter("raw") | parser,
            parsing_error=RunnableLambda(lambda _: None),
        )
        unparsed = RunnablePassthrough.assign(parsed=RunnableLambda(lambda _: None))
        return RunnableMap(raw=model) | parsed.with_fallbacks(
            [unparsed], exception_key="parsing_error"
        )

    # -- generation --------------------------------------------------------- #

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError(
            "ChatClaudeSubscription is async-only; call ainvoke/astream."
        )

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        raise NotImplementedError(
            "ChatClaudeSubscription is async-only; call ainvoke/astream."
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        turn = await self._run_turn(messages)
        message = _to_ai_message(turn, self.reported_model_name)
        return ChatResult(
            generations=[ChatGeneration(message=message)],
            llm_output={"model_name": self.reported_model_name},
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """한 덩어리로 한 번 내보낸다.

        token 단위 streaming 은 `include_partial_messages` 로 열 수 있지만, 이 저장소가
        `astream` 을 쓰는 곳(`app/agents/qa/runner.py`)은 graph node 단위 update 를
        읽지 token 을 읽지 않는다. 쓰이지 않는 경로를 붙이는 대신 계약만 채운다.
        """
        turn = await self._run_turn(messages)
        message = _to_ai_message(turn, self.reported_model_name)
        chunk = AIMessageChunk(
            content=message.content,
            tool_calls=message.tool_calls,
            additional_kwargs=message.additional_kwargs,
            usage_metadata=message.usage_metadata,
            response_metadata=message.response_metadata,
        )
        yield ChatGenerationChunk(message=chunk)

    async def _run_turn(self, messages: Sequence[BaseMessage]) -> SdkTurn:
        sdk = _import_claude_agent_sdk()
        prompt = build_sdk_prompt(messages, tools_bound=bool(self.bound_tools))
        recorded: list[RecordedToolCall] = []
        options = self._build_options(sdk, prompt, recorded)

        async def stream_turns() -> AsyncIterator[dict[str, Any]]:
            for turn in prompt.turns:
                yield turn

        collected_text: list[str] = []
        streamed_tool_calls: list[RecordedToolCall] = []
        structured_output: Any = None
        usage: Mapping[str, Any] | None = None
        stop_reason: str | None = None
        assistant_messages_seen = 0

        try:
            async for message in sdk.query(prompt=stream_turns(), options=options):
                if isinstance(message, sdk.AssistantMessage):
                    assistant_messages_seen += 1
                    if assistant_messages_seen <= prompt.replayed_assistant_turn_count:
                        # 우리가 되먹인 history 가 그대로 돌아온 것이다.
                        continue
                    _collect_assistant_blocks(
                        message, collected_text, streamed_tool_calls
                    )
                elif isinstance(message, sdk.ResultMessage):
                    usage = message.usage
                    stop_reason = message.stop_reason
                    structured_output = message.structured_output
                    if not collected_text and message.result:
                        collected_text.append(message.result)
        except sdk.ResultError as error:
            # tool 호출을 `interrupt=True` 로 막으면 CLI 는 turn 을 못 끝냈다고 보고
            # 죽는다. 붙잡은 호출이 있으면 그것이 바로 이 turn 의 답이다.
            if not recorded and not streamed_tool_calls:
                raise
            logger.debug(
                "[claude-subscription] the turn ended on a tool call: %s", error
            )

        tool_calls = tuple(recorded or streamed_tool_calls)
        return SdkTurn(
            text="\n".join(part for part in collected_text if part),
            tool_calls=tool_calls,
            structured_output=structured_output,
            usage=usage,
            stop_reason=stop_reason,
        )

    def _build_options(
        self, sdk: Any, prompt: SdkPrompt, recorded: list[RecordedToolCall]
    ) -> Any:
        mcp_servers: dict[str, Any] = {}
        tool_names: list[str] = []
        can_use_tool = None

        if self.bound_tools:
            mcp_servers[MCP_SERVER_NAME] = sdk.create_sdk_mcp_server(
                name=MCP_SERVER_NAME,
                tools=[_declare_tool(sdk, tool) for tool in self.bound_tools],
            )
            tool_names = [f"{MCP_TOOL_PREFIX}{tool.name}" for tool in self.bound_tools]
            can_use_tool = _build_permission_callback(sdk, recorded)

        return sdk.ClaudeAgentOptions(
            model=self.sdk_model_name,
            system_prompt=prompt.system_prompt,
            # 이 다섯 개가 harness prompt 와 built-in tool 정의를 잘라낸다. module
            # docstring 의 45배가 그 차이다.
            tools=tool_names,
            mcp_servers=mcp_servers,
            strict_mcp_config=True,
            setting_sources=[],
            skills=[],
            # tool 을 막고 나면 CLI 는 turn 을 이어가려 한다. LangChain 은 turn 하나를
            # 기대하므로 거기서 끊는다. tool 이 없으면 부를 것도 없어서 필요 없다.
            max_turns=1 if self.bound_tools else None,
            can_use_tool=can_use_tool,
            effort=self.effort,
            output_format=self.output_format,
        )


def _collect_assistant_blocks(
    message: Any, texts: list[str], tool_calls: list[RecordedToolCall]
) -> None:
    for block in message.content or ():
        text = getattr(block, "text", None)
        if text is not None:
            texts.append(text)
            continue
        name = getattr(block, "name", None)
        if name is None or name == STRUCTURED_OUTPUT_TOOL_NAME:
            continue
        tool_calls.append(
            RecordedToolCall(
                id=getattr(block, "id", ""),
                name=strip_mcp_tool_prefix(name),
                arguments=dict(getattr(block, "input", None) or {}),
            )
        )


def _build_permission_callback(sdk: Any, recorded: list[RecordedToolCall]):
    """호출을 기록하고 막는 `can_use_tool`.

    tool 을 실제로 돌리는 것은 LangChain 쪽 graph 다. 여기서는 모델이 무엇을 부르려
    했는지만 받아야 하므로 `interrupt=True` 로 turn 을 그 자리에서 끊는다.

    우리 MCP tool 이 아닌 것은 통과시킨다. `output_format` 을 쓰면 SDK 가
    `StructuredOutput` tool 을 스스로 부르는데, 그것까지 막으면 structured output 이
    영영 오지 않는다.
    """

    async def decide(tool_name: str, input_data: dict[str, Any], context: Any):
        if not tool_name.startswith(MCP_TOOL_PREFIX):
            return sdk.PermissionResultAllow()
        recorded.append(
            RecordedToolCall(
                id=getattr(context, "tool_use_id", None) or "",
                name=strip_mcp_tool_prefix(tool_name),
                arguments=dict(input_data or {}),
            )
        )
        return sdk.PermissionResultDeny(
            message="This tool runs outside the Claude Code session.",
            interrupt=True,
        )

    return decide


def _declare_tool(sdk: Any, tool: BoundTool) -> Any:
    async def never_runs(_arguments: dict[str, Any]) -> dict[str, Any]:
        # `can_use_tool` 이 `interrupt=True` 로 막으므로 여기까지 오지 않는다. 오면
        # 그 계약이 깨진 것이고, 조용히 빈 결과를 돌려주면 아무도 모른다.
        raise RuntimeError(
            f"Tool '{tool.name}' was executed inside the Claude Agent SDK; it must "
            "only be reported back to LangChain."
        )

    return sdk.tool(tool.name, tool.description, tool.input_schema)(never_runs)


def _to_bound_tool(tool: Any) -> BoundTool:
    if isinstance(tool, BoundTool):
        return tool
    specification = convert_to_openai_tool(tool)["function"]
    return BoundTool(
        name=specification["name"],
        description=specification.get("description") or specification["name"],
        input_schema=to_draft_07_schema(
            specification.get("parameters") or {"type": "object", "properties": {}}
        ),
    )


def _parse_structured(message: Any, schema: Any) -> Any:
    parsed = (getattr(message, "additional_kwargs", None) or {}).get(
        "structured_output"
    )
    if parsed is None:
        raise ValueError(
            "The Claude subscription backend returned no structured output for this "
            "call."
        )
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_validate(parsed)
    return parsed


# --------------------------------------------------------------------------- #
# factory
# --------------------------------------------------------------------------- #


# 이 셋 중 하나라도 환경에 있으면 CLI 는 구독이 아니라 그 credential 로 붙는다. 그러면
# 이 backend 를 고른 이유 자체가 사라진다 — 돈이 없는 key 를 피해 왔는데 조용히 돈 내는
# 경로로 되돌아간 것이고, 요금이 찍히기 전까지 아무도 모른다.
#
# 지우지는 않는다. 호출자의 환경을 말없이 고치는 쪽이 더 나쁘고, 그 변수를 일부러 둔
# 사람도 있다. 사실만 알리고 판단은 넘긴다.
_SUBSCRIPTION_OVERRIDING_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def _warn_if_an_api_key_will_outrank_the_subscription() -> None:
    present = [name for name in _SUBSCRIPTION_OVERRIDING_VARS if os.environ.get(name)]
    if not present:
        return
    logger.warning(
        "[claude-subscription] %s is set, so the `claude` CLI authenticates with it "
        "instead of the Claude subscription and these calls are billed to that "
        "account. Unset it to spend the subscription instead.",
        ", ".join(present),
    )


@lru_cache
def build_claude_subscription_chat_model(
    model: LLMModel,
    reasoning: ReasoningConfig | None = None,
    cache_prompt: bool = False,
) -> BaseChatModel:
    """`build_chat_model` 과 같은 서명의, 구독으로 도는 chat model.

    `cache_prompt` 는 받아만 두고 버린다. Agent SDK 에는 cache breakpoint 를 놓을
    자리가 없다 — CLI 가 자기 판단으로 캐시하고 요청은 거기에 관여할 수 없다. 인자를
    빼면 `build_chat_model` 과 서명이 갈라져서 호출부가 backend 를 알아야 한다.
    """
    _warn_if_an_api_key_will_outrank_the_subscription()
    reasoning = validate_reasoning(model, reasoning)

    effort: str | None = None
    if reasoning is not None and reasoning.effort is not None:
        effort = reasoning.effort.value
    elif reasoning is not None:
        # `reasoning.max_tokens` 에 대응하는 자리가 SDK 에 없다. 여기서 거절하지 않는
        # 이유는 모델을 이미 바꿔 싣고 있기 때문이다 — max_tokens 를 요구하는 모델은
        # 전부 Anthropic 이 아니라서 어차피 대체 모델이 답한다. 예산 하나 때문에 런을
        # 못 돌게 하는 것보다, 바뀐 사실을 로그에 남기고 진행하는 편이 맞다.
        logger.warning(
            "[claude-subscription] dropped reasoning.max_tokens=%s; the Agent SDK "
            "only takes an effort level",
            reasoning.max_tokens,
        )

    sdk_model_name = to_sdk_model_name(model)
    return ChatClaudeSubscription(
        sdk_model_name=sdk_model_name,
        # `app/llm/usage.py` 는 이 값을 "/" 로 갈라 vendor 를 얻는다. 요청이 고른 slug
        # 가 아니라 실제로 답한 모델을 싣는다.
        reported_model_name=f"{LLMProvider.anthropic.value}/{sdk_model_name}",
        effort=effort,
        callbacks=[UsageCallback()],
    )
