"""나가는 payload 가 접두 캐시를 깨지 않는가."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.llm.chat_model import build_chat_model
from app.llm.models import LLMModel


def _payload(messages):
    return build_chat_model(LLMModel.gpt_5_6_luna)._get_request_payload(messages)


def _with_tool_call():
    return [
        SystemMessage(content="시스템 프롬프트"),
        HumanMessage(content="시작해"),
        AIMessage(
            content="",
            tool_calls=[{"name": "observe_scene", "args": {"thought": "본다"}, "id": "call_1"}],
        ),
        ToolMessage(content="도구 결과", tool_call_id="call_1"),
    ]


def test_도구_호출_메시지가_content_null_로_나가지_않는다():
    """langchain 이 `content or None` 로 일부러 null 을 넣는데, 그 null 하나가
    OpenRouter 를 통한 접두 캐싱을 깬다.

    OpenRouter 에 직접 쏘아 갈랐다 — 같은 모델·같은 메시지 목록으로 일곱 턴 돌려
    null 은 4,510 에 고정, 빈 문자열은 4,724에서 6,389까지 자랐다(ARTEL-614)."""
    payload = _payload(_with_tool_call())

    assistant = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert assistant, "assistant 메시지가 없다"
    assert assistant[0]["content"] is not None
    # tool_calls 는 그대로 실려야 한다. content 만 손대는 수정이다.
    assert assistant[0]["tool_calls"]


def test_다른_역할의_메시지는_건드리지_않는다():
    """content 를 채우는 것은 assistant 뿐이다. tool 이나 user 의 None 은 다른 뜻일
    수 있고, 이 수정이 답하는 문제도 아니다."""
    payload = _payload(_with_tool_call())

    kinds = {m["role"]: m.get("content") for m in payload["messages"]}
    assert kinds["system"] == "시스템 프롬프트"
    assert kinds["user"] == "시작해"
    assert kinds["tool"] == "도구 결과"


def test_도구_호출이_없는_assistant_는_원래_내용을_지킨다():
    payload = _payload(
        [
            HumanMessage(content="안녕"),
            AIMessage(content="반갑다"),
        ]
    )

    assistant = [m for m in payload["messages"] if m["role"] == "assistant"][0]
    assert assistant["content"] == "반갑다"
