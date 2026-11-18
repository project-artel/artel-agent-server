"""이번 호출에만 붙는 메시지와, 그 앞에 두는 Anthropic cache 경계.

QA run 은 저장하지 않는 메시지를 호출마다 대화 끝에 붙인다(`latest_pulse_context` 의 최신 `pulse` 요약).
Anthropic 의 top-level `cache_control` 은 요청 맨 끝에 경계를 놓으므로, 끝이 그런 메시지면 그 경계로 쓴
cache 는 그 메시지를 품고 있어 다음 호출의 접두가 되지 못한다. 다음 호출은 시스템 경계까지만 읽고 대화를
전부 다시 쓴다(`prompt_messages` 의 PE 엔진 런 154~160 과 같은 이유). 그래서 첫 `per_call` 메시지 바로
앞, 저장된 대화의 끝에 경계를 하나 더 둔다. 시스템 · 여기 · top-level 로 경계는 셋이고, API 한도는 넷이다.

`fold_context` 가 대화 중간을 고쳐 쓰는 것도 같다. 경계가 시스템 끝과 대화 끝에만 있으면 `fold` 한 번에
시스템 뒤 대화 전체를 다시 쓴다. 그래서 다음 `fold` batch 까지 바뀌지 않는 앞부분의 끝(`stable`)에도 경계를
둔다. 그러면 경계는 시스템 · `stable` · 저장된 대화 끝 · top-level 로 넷이고, API 한도와 같다.

OpenAI 와 Google 은 같은 접두를 스스로 찾아 읽으므로 경계가 필요 없다. 표시만 무시하면 된다.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage

# `additional_kwargs` 에 둔다 — langchain 의 Anthropic · OpenAI 변환은 사람 메시지의 `additional_kwargs` 를
# 요청에 싣지 않는다.
PER_CALL_KEY = "artel_per_call"
STABLE_KEY = "artel_stable_until_fold"
TURN_SCOPED_KEY = "artel_turn_scoped"

EPHEMERAL = {"type": "ephemeral"}


def per_call(message: BaseMessage) -> BaseMessage:
    """`message` 를 이번 호출에만 붙는 메시지로 표시한다."""
    return message.model_copy(update={"additional_kwargs": {**message.additional_kwargs, PER_CALL_KEY: True}})


def turn_scoped(message: BaseMessage) -> BaseMessage:
    """`message` 를 그 턴에만 보이고 대화에 남는 안내로 표시한다.

    Claude 요청에서는 `clear_at: "next_user_message"` 인 대화 중간 system 메시지가 된다(`app.llm.claude_api`). 그 턴이
    지나면 대화에 남은 채 비워져 입력 token 이 들지 않고, 지운 것이 아니라 `cache` 도 thinking 도 깨지지 않는다.
    """
    return message.model_copy(update={"additional_kwargs": {**message.additional_kwargs, TURN_SCOPED_KEY: True}})


def stable(message: BaseMessage) -> BaseMessage:
    """`message` 를 다음 `fold` batch 까지 바뀌지 않는 앞부분의 끝으로 표시한다."""
    return message.model_copy(update={"additional_kwargs": {**message.additional_kwargs, STABLE_KEY: True}})


def last_stable(messages: list[BaseMessage]) -> int | None:
    """마지막 `stable` 메시지의 자리. 없으면 None 이다."""
    return next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].additional_kwargs.get(STABLE_KEY)),
        None,
    )


def first_per_call(messages: list[BaseMessage]) -> int | None:
    """첫 `per_call` 메시지의 자리. 없거나 맨 앞이면 경계를 둘 저장된 메시지가 없으므로 None 이다."""
    first = next((index for index, message in enumerate(messages) if message.additional_kwargs.get(PER_CALL_KEY)), None)
    return first or None


def with_breakpoint(content: Any) -> list[Any] | None:
    """`content` 의 마지막 block 에 경계를 단 content. 달 수 없거나 이미 달려 있으면 None 이다.

    마지막 block 이 그림(`image_url`)이면 그 앞의 마지막 block 에 단다. langchain-anthropic 이 `image_url` 을
    Anthropic `image` 로 바꿀 때 다른 키를 버려서, 그림에 단 경계는 요청에 실리지 않는다. 그러면 그 호출에는
    시스템 끝과 요청 끝 경계만 남고, 다음 호출이 시스템 뒤를 전부 다시 썼다(로컬 런 137: `capture_screen`
    뒤 두 번, 각 약 14만 token).
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content, "cache_control": EPHEMERAL}] if content else None
    if not isinstance(content, list):
        return None
    index = next(
        (
            at
            for at in range(len(content) - 1, -1, -1)
            if isinstance(content[at], dict) and content[at].get("type") != "image_url"
        ),
        None,
    )
    if index is None or "cache_control" in content[index]:
        return None
    return [*content[:index], {**content[index], "cache_control": EPHEMERAL}, *content[index + 1 :]]
