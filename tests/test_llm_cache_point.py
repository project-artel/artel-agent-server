"""캐시 경계를 어디에 찍는가.

경계 뒤는 캐시에 안 들어간다. 그래서 매 턴 바뀌는 것 — QA 런의 화면 그림 — 이 경계
앞에 들어가면 다음 턴에 되읽을 것이 사라진다. 실측으로 그 손해가 $0.87 대 $14.22 였다
(ARTEL-868).
"""

from langchain_core.messages import AIMessage, HumanMessage

from app.agents.qa.vision import TRANSIENT_MESSAGE_KEY, build_capture_message
from app.llm.chat_model import CACHE_POINT, _CachingChatBedrockConverse


def blocks_of(message):
    content = message.content
    return content if isinstance(content, list) else [{"type": "text", "text": content}]


def has_cache_point(message) -> bool:
    return any(isinstance(b, dict) and "cachePoint" in b for b in blocks_of(message))


def mark(messages):
    """`_with_cache_point` 만 떼어 쓴다. 모델을 만들면 자격증명이 필요하다."""
    return _CachingChatBedrockConverse._with_cache_point(None, messages)


def transient_image():
    message = build_capture_message("capture-1", "AAAA", "image/png", "This is the screen.")
    message.additional_kwargs[TRANSIENT_MESSAGE_KEY] = True
    return message


def test_the_boundary_goes_on_the_last_stored_message() -> None:
    """평소에는 맨 끝이다. 다음 턴이 이 프롬프트 전체를 prefix 로 갖는다."""
    marked = mark([HumanMessage(content="plan"), AIMessage(content="thinking")])

    assert not has_cache_point(marked[0])
    assert has_cache_point(marked[1])


def test_the_boundary_goes_before_a_transient_picture() -> None:
    """그림은 경계 밖이다.

    안에 두면 그림이 캐시된 prefix 의 일부가 되고, 다음 턴은 그림이 바뀌어 있으므로
    그 prefix 를 못 읽는다. 이 arm 이 피하려는 손해가 바로 그것이다.
    """
    image = transient_image()
    marked = mark([HumanMessage(content="plan"), AIMessage(content="thinking"), image])

    assert has_cache_point(marked[1])
    assert not has_cache_point(marked[2])
    # 그림 자체는 손대지 않고 그대로 실려 간다.
    assert marked[2] is image


def test_a_prompt_that_is_only_transient_gets_no_boundary() -> None:
    """찍을 자리가 없으면 안 찍는다. 경계는 되읽을 것이 앞에 있어야 뜻이 있다."""
    assert not any(has_cache_point(m) for m in mark([transient_image()]))


def test_the_boundary_is_not_written_twice() -> None:
    """Anthropic 은 요청당 네 개까지 받고, 재시도로 같은 메시지가 두 번 지나간다."""
    once = mark([HumanMessage(content="plan")])
    twice = mark(once)

    assert sum(1 for b in blocks_of(twice[0]) if b == CACHE_POINT) == 1
