"""이번 호출에만 붙는 메시지와, 그 앞에 두는 Anthropic cache 경계.

QA run 은 저장하지 않는 메시지를 호출마다 대화 끝에 붙인다(`latest_pulse_context` 의 최신 `pulse` 요약).
Anthropic 의 top-level `cache_control` 은 요청 맨 끝에 경계를 놓으므로, 끝이 그런 메시지면 그 경계로 쓴
cache 는 그 메시지를 품고 있어 다음 호출의 접두가 되지 못한다. 다음 호출은 시스템 경계까지만 읽고 대화를
전부 다시 쓴다(`prompt_messages` 의 PE 엔진 런 154~160 과 같은 이유). 그래서 첫 `per_call` 메시지 바로
앞, 저장된 대화의 끝에 경계를 하나 더 둔다. 시스템 · 여기 · top-level 로 경계는 셋이고, API 한도는 넷이다.

OpenAI 와 Google 은 같은 접두를 스스로 찾아 읽으므로 경계가 필요 없다. 표시만 무시하면 된다.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage

# `additional_kwargs` 에 둔다 — langchain 의 Anthropic · OpenAI 변환은 사람 메시지의 `additional_kwargs` 를
# 요청에 싣지 않는다.
PER_CALL_KEY = "artel_per_call"

EPHEMERAL = {"type": "ephemeral"}


def per_call(message: BaseMessage) -> BaseMessage:
    """`message` 를 이번 호출에만 붙는 메시지로 표시한다."""
    return message.model_copy(update={"additional_kwargs": {**message.additional_kwargs, PER_CALL_KEY: True}})


def first_per_call(messages: list[BaseMessage]) -> int | None:
    """첫 `per_call` 메시지의 자리. 없거나 맨 앞이면 경계를 둘 저장된 메시지가 없으므로 None 이다."""
    first = next((index for index, message in enumerate(messages) if message.additional_kwargs.get(PER_CALL_KEY)), None)
    return first or None


def with_breakpoint(content: Any) -> list[Any] | None:
    """`content` 의 마지막 block 에 경계를 단 content. 달 수 없거나 이미 달려 있으면 None 이다."""
    if isinstance(content, str):
        return [{"type": "text", "text": content, "cache_control": EPHEMERAL}] if content else None
    if isinstance(content, list) and content and isinstance(content[-1], dict) and "cache_control" not in content[-1]:
        return [*content[:-1], {**content[-1], "cache_control": EPHEMERAL}]
    return None
