"""`ChatAnthropic` for the `claude/` route, with two Claude API rules applied.

1. Forced `tool_choice`. Opus 5.5, Sonnet 5.5 and Fable 5.1 answer `tool_choice`
   `any` or `tool` with a 400. `create_agent`'s `ToolStrategy` sends `any` to get
   its final structured answer, and langchain-anthropic 1.7 removes it only when
   `thinking` is set on the client. A call with no effort chosen sends no
   `thinking`, so the forced choice would reach the API. `bind_tools` lowers it to
   `auto` here. Haiku 5.5 accepts a forced choice, but all four models are treated
   the same so that a run behaves the same whichever of them it uses.

2. Preserved thinking. A thinking block's `signature` is bound to everything sent
   before it: the system prompt, the tools, and every earlier message. A later
   request that sends the block back after any of that changed is a 400 on accounts
   created on or after 2026-08-31 (older accounts are not checked unless they opt
   in). Removing every thinking block from the turn that was edited onward is
   accepted. The QA loop changes earlier messages in three places:

   - `fold_context` (`app/agents/qa/runner.py`) replaces old scene views, skill
     blocks, knowledge neighbour blocks and `on_demand` screenshots in earlier
     `ToolMessage` and capture messages with placeholders, once per batch.
   - `QaCompactionMiddleware` (`app/agents/qa/compaction.py`) replaces old turns
     with a summary and a progress ledger and keeps the newest turns (with their
     scene views folded). The kept assistant turns were produced with the full
     history in front of them.
   - `QaCaptureVisionMiddleware.awrap_model_call` (`app/agents/qa/vision.py`), in
     the `every_call` and `by_role` modes, appends screenshots to one request and
     does not store them. The assistant turn produced by that request was bound to
     a prefix that ends with those screenshots, and the next request no longer has
     them.

   None of the three touches an assistant message itself, so the thinking blocks
   stay in the graph state unchanged. `ClaudeApiChat` keeps, for each thinking
   block it receives, a hash of the request that produced it. Before each request
   it compares that hash with the messages now in front of the block, and removes
   the block from this request only when they differ. An unedited loop sends every
   block back unchanged. An edited one loses the reasoning of the turns after the
   edit, which is what the API would drop anyway, instead of failing with a 400.

   The hash covers the message types, their content without thinking blocks, tool
   calls and tool call ids, and the bound tools. Thinking blocks are left out on
   purpose: the API binds a block to the visible prefix and chains it to the block
   before it, so a block removed by this rule is removed on every later request too
   and the chain stays the same.

   A block whose signature this process has not seen (a restart in the middle of a
   run, or a block from another model) is sent unchanged. The API drops blocks from
   other models by itself.

The scenario agent loop (`app/agents/scenario/agent.py`) only appends to its
history, so it is not affected.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})

# How many thinking block signatures one client remembers. One QA run makes a few
# hundred model calls; the client is cached for the process and shared by every run
# on the same model, key and settings, so this is room for many runs at once. Each
# entry is a signature and a 64-character hash.
MAX_REMEMBERED_SIGNATURES = 20_000

# 이번 호출에만 붙고 대화에 저장되지 않는 메시지의 표시. `additional_kwargs` 에 둔다 — langchain 의
# Anthropic · OpenAI 변환은 사람 메시지의 `additional_kwargs` 를 요청에 싣지 않는다.
PER_CALL_KEY = "artel_per_call"


def forces_a_tool(tool_choice: Any) -> bool:
    """Whether `tool_choice` forces `any` or a named tool. `auto` and None do not."""
    if tool_choice is None:
        return False
    if isinstance(tool_choice, dict):
        return tool_choice.get("type") in ("any", "tool")
    if isinstance(tool_choice, str):
        return tool_choice not in ("auto", "none")
    return bool(tool_choice)


def _is_thinking(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") in THINKING_BLOCK_TYPES


def _signatures(message: BaseMessage) -> list[str]:
    """The signatures of `message`'s thinking blocks. `redacted_thinking` uses `data`."""
    if not isinstance(message.content, list):
        return []
    found = []
    for block in message.content:
        if not _is_thinking(block):
            continue
        signature = block.get("signature") or block.get("data")
        if isinstance(signature, str) and signature:
            found.append(signature)
    return found


def _without_thinking(message: BaseMessage) -> BaseMessage:
    if not isinstance(message.content, list):
        return message
    kept = [block for block in message.content if not _is_thinking(block)]
    if len(kept) == len(message.content):
        return message
    return message.model_copy(update={"content": kept})


def _visible_form(message: BaseMessage) -> str:
    """What of `message` a thinking block is bound to, as stable text."""
    content = message.content
    if isinstance(content, list):
        content = [block for block in content if not _is_thinking(block)]
    return json.dumps(
        {
            "type": message.type,
            "content": content,
            "tool_calls": getattr(message, "tool_calls", None) or None,
            "tool_call_id": getattr(message, "tool_call_id", None),
        },
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    )


def _prefix_hashes(messages: Sequence[BaseMessage], tools: Any) -> list[str]:
    """`result[k]` is the hash of the tools and `messages[:k]`. One pass, rolling."""
    digest = hashlib.sha256(
        json.dumps(tools, sort_keys=True, default=str).encode()
    ).hexdigest()
    hashes = [digest]
    for message in messages:
        digest = hashlib.sha256(
            (digest + _visible_form(message)).encode()
        ).hexdigest()
        hashes.append(digest)
    return hashes


class ClaudeApiChat(ChatAnthropic):
    """`ChatAnthropic` that never forces a tool and removes invalidated thinking blocks.

    See the module docstring for both rules. `_llm_type` stays `anthropic-chat`, the
    value langchain-anthropic requires before it sends a top-level `cache_control`.
    """

    _bound: OrderedDict[str, str] = PrivateAttr(default_factory=OrderedDict)
    _bound_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # type: ignore[override]
        if forces_a_tool(tool_choice):
            tool_choice = None
        return super().bind_tools(tools, tool_choice=tool_choice, **kwargs)

    # --- preserved thinking -------------------------------------------------------

    def _prepare(
        self, messages: list[BaseMessage], kwargs: dict[str, Any]
    ) -> tuple[list[BaseMessage], str]:
        """`messages` with every invalidated thinking block removed, and the hash of
        the request they make, which binds the thinking of the answer."""
        hashes = _prefix_hashes(messages, kwargs.get("tools"))
        with self._bound_lock:
            prepared = []
            for index, message in enumerate(messages):
                if isinstance(message, AIMessage) and self._invalidated(
                    message, hashes[index]
                ):
                    message = _without_thinking(message)
                prepared.append(message)
        return _with_stored_breakpoint(_with_system_breakpoint(prepared)), hashes[-1]

    def _invalidated(self, message: AIMessage, prefix_hash: str) -> bool:
        for signature in _signatures(message):
            bound = self._bound.get(signature)
            if bound is not None and bound != prefix_hash:
                return True
        return False

    def _remember(self, message: BaseMessage, request_hash: str) -> None:
        signatures = _signatures(message)
        if not signatures:
            return
        with self._bound_lock:
            for signature in signatures:
                self._bound[signature] = request_hash
                self._bound.move_to_end(signature)
            while len(self._bound) > MAX_REMEMBERED_SIGNATURES:
                self._bound.popitem(last=False)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        prepared, request_hash = self._prepare(messages, kwargs)
        result = super()._generate(prepared, stop, run_manager, **kwargs)
        for generation in result.generations:
            self._remember(generation.message, request_hash)
        return result

    async def _agenerate(
        self, messages, stop=None, run_manager=None, **kwargs
    ) -> ChatResult:
        prepared, request_hash = self._prepare(messages, kwargs)
        result = await super()._agenerate(prepared, stop, run_manager, **kwargs)
        for generation in result.generations:
            self._remember(generation.message, request_hash)
        return result

    def _stream(
        self, messages, stop=None, run_manager=None, **kwargs
    ) -> Iterator[ChatGenerationChunk]:
        prepared, request_hash = self._prepare(messages, kwargs)
        answer: AIMessageChunk | None = None
        for chunk in super()._stream(prepared, stop, run_manager, **kwargs):
            answer = _merge(answer, chunk)
            yield chunk
        if answer is not None:
            self._remember(answer, request_hash)

    async def _astream(
        self, messages, stop=None, run_manager=None, **kwargs
    ) -> AsyncIterator[ChatGenerationChunk]:
        prepared, request_hash = self._prepare(messages, kwargs)
        answer: AIMessageChunk | None = None
        async for chunk in super()._astream(prepared, stop, run_manager, **kwargs):
            answer = _merge(answer, chunk)
            yield chunk
        if answer is not None:
            self._remember(answer, request_hash)


def _with_system_breakpoint(messages: list[BaseMessage]) -> list[BaseMessage]:
    """시스템 프롬프트 끝에 캐시 경계를 하나 둔다.

    top-level `cache_control` 은 요청 맨 끝 하나에만 경계를 놓는다. 그러면 뒤가 조금만 바뀌어도 늘 같은
    도구 정의와 시스템 프롬프트(QA 에서 약 3.9만 토큰)까지 캐시에서 빠지고 매번 다시 쓴다. 여기 경계를 두면
    그 부분은 뒤가 바뀌어도 읽기로 처리된다. 도구는 시스템 프롬프트보다 앞에 오므로 함께 덮인다.
    """
    if not messages or not isinstance(messages[0], SystemMessage):
        return messages
    content = messages[0].content
    if isinstance(content, str):
        if not content:
            return messages
        blocks: list[Any] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict) and content[-1].get("type") == "text":
        if "cache_control" in content[-1]:
            return messages
        blocks = [*content[:-1], {**content[-1], "cache_control": {"type": "ephemeral"}}]
    else:
        return messages
    return [messages[0].model_copy(update={"content": blocks}), *messages[1:]]


def per_call(message: BaseMessage) -> BaseMessage:
    """`message` 를 이번 호출에만 붙는 메시지로 표시한다. Claude 경로는 그 앞에 cache 경계를 둔다."""
    return message.model_copy(update={"additional_kwargs": {**message.additional_kwargs, PER_CALL_KEY: True}})


def _with_stored_breakpoint(messages: list[BaseMessage]) -> list[BaseMessage]:
    """저장된 대화의 끝, 곧 첫 `per_call` 메시지 바로 앞에 cache 경계를 하나 둔다.

    top-level `cache_control` 은 요청 맨 끝에 경계를 놓는다. 끝이 이번 호출에만 붙는 메시지면 그 경계로 쓴
    cache 는 그 메시지를 품고 있어 다음 호출의 접두가 되지 못하고, 다음 호출은 시스템 경계까지만 읽고 대화를
    전부 다시 쓴다(`prompt_messages` 의 PE 엔진 런 154~160 과 같은 이유). 저장된 끝에 경계를 두면 다음
    호출이 그 경계까지 읽는다. 시스템 · 여기 · top-level 로 경계는 셋이고, API 한도는 넷이다.
    """
    first = next((index for index, message in enumerate(messages) if message.additional_kwargs.get(PER_CALL_KEY)), None)
    # 답 메시지에는 달지 않는다. 마지막 블록이 경계를 받지 못하는 thinking 일 수 있다.
    if not first or isinstance(messages[first - 1], (SystemMessage, AIMessage)):
        return messages
    target = messages[first - 1]
    content = target.content
    if isinstance(content, str):
        if not content:
            return messages
        blocks: list[Any] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        if "cache_control" in content[-1]:
            return messages
        blocks = [*content[:-1], {**content[-1], "cache_control": {"type": "ephemeral"}}]
    else:
        return messages
    return [*messages[: first - 1], target.model_copy(update={"content": blocks}), *messages[first:]]


def _merge(
    answer: AIMessageChunk | None, chunk: ChatGenerationChunk
) -> AIMessageChunk | None:
    """Add `chunk` to the streamed answer so far. A signature can arrive in pieces."""
    message = chunk.message
    if not isinstance(message, AIMessageChunk):
        return answer
    return message if answer is None else answer + message
