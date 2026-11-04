"""시스템 프롬프트 끝의 캐시 경계(실험). 뒤가 바뀌어도 도구 · 시스템 프롬프트는 캐시에서 읽힌다."""

from langchain_core.messages import HumanMessage, SystemMessage

from app.llm.claude_api import _with_system_breakpoint


def test_문자열_시스템_프롬프트는_경계를_단_블록이_된다() -> None:
    out = _with_system_breakpoint([SystemMessage(content="rules"), HumanMessage(content="hi")])

    assert out[0].content == [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}]
    assert out[1].content == "hi"


def test_블록_시스템_프롬프트는_마지막_블록에만_경계를_단다() -> None:
    system = SystemMessage(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])

    out = _with_system_breakpoint([system])

    assert out[0].content == [{"type": "text", "text": "a"}, {"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}}]


def test_시스템_프롬프트가_없으면_그대로다() -> None:
    messages = [HumanMessage(content="hi")]

    assert _with_system_breakpoint(messages) is messages
