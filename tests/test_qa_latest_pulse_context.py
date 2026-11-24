import asyncio
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, ToolMessage

from app.agents.qa.runner import _latest_pulse_context_for
from app.llm.cache_boundary import PER_CALL_KEY
from app.qa.pulse import PulseMemory, PulseReading


def enemy(value: int, object_id: int = 7) -> dict:
    return {"id": object_id, "selector": "Enemy[0]", "members": [{"on": "Enemy", "member": "Hp", "value": value}]}


def test_latest_context_keeps_intermediate_values_and_explicit_removal():
    pulse = PulseMemory()
    for index, value in enumerate((100, 90, 90, 80), start=1):
        pulse.apply(PulseReading(reading=index, scene="Battle", active=[enemy(value)]))
    pulse.apply(PulseReading(reading=5, scene="Battle"))
    assert "100 → 90 → 80" in pulse.render_latest_context()

    pulse.apply(PulseReading(reading=6, scene="Battle", gone=["Battle/Enemy[0]"]))
    context = pulse.render_latest_context()
    assert "Reported gone: Enemy[0]" in context
    assert "Enemy::Hp" not in context

    pulse.apply(PulseReading(reading=7, scene="Battle", active=[enemy(35, object_id=8)]))
    context = pulse.render_latest_context()
    assert "Reported gone:" not in context
    assert "Enemy::Hp#0: 35" in context


def test_latest_context_distinguishes_snapshot_omissions_and_inactive_objects():
    pulse = PulseMemory()
    pulse.apply(PulseReading(reading=1, scene="Battle", whole=True, active=[enemy(100)]))
    pulse.apply(PulseReading(reading=2, scene="Battle", deactive=[enemy(100)]))
    assert "Inactive: Enemy[0]" in pulse.render_latest_context()

    pulse.apply(PulseReading(reading=3, scene="Battle", whole=True, gaps=["holder-limit:8"]))
    context = pulse.render_latest_context()
    assert "Missing from the latest full snapshot (removal unconfirmed): Enemy[0]" in context
    assert "Reported gone:" not in context


def test_latest_context_names_the_scene_and_does_not_carry_the_whole_view():
    """머리줄의 씬은 이름이다. 쥔 값 전부를 그린 view 가 실리면 요약이 매 호출 그 view 만큼 커진다(로컬 런 136)."""
    pulse = PulseMemory()
    for object_id in range(200):
        pulse.apply(PulseReading(reading=1, scene="Battle", whole=True, active=[enemy(100, object_id=object_id)]))
    drawn = pulse.drawn

    context = pulse.render_latest_context()

    assert context.splitlines()[0] == "Latest Pulse observations · scene Battle · reading 1"
    assert "<<pulse>>" not in context
    assert len(context) <= 7_000
    # 요약은 다음 도구 결과의 view 가 어디서부터 그릴지를 건드리지 않는다.
    assert pulse.drawn == drawn


def test_latest_context_is_appended_once_to_each_request_after_the_stored_conversation():
    pulse = PulseMemory()
    channel = SimpleNamespace(scene=SimpleNamespace(pulse=pulse))
    middleware = _latest_pulse_context_for(channel)
    original = [ToolMessage(content="previous response", tool_call_id="last")]
    request = SimpleNamespace(messages=original, override=lambda **fields: SimpleNamespace(**fields))

    async def capture(call):
        return call.messages

    assert asyncio.run(middleware.awrap_model_call(request, capture)) == original
    pulse.apply(PulseReading(reading=1, scene="Battle", active=[enemy(100)]))
    first = asyncio.run(middleware.awrap_model_call(request, capture))
    pulse.apply(PulseReading(reading=2, scene="Battle", active=[enemy(80)]))
    second = asyncio.run(middleware.awrap_model_call(request, capture))

    assert len(original) == 1
    # 요약이 바뀌어도 앞의 대화는 그대로다. 그래야 다음 호출이 앞을 `cache` 에서 읽는다.
    assert first[:-1] == second[:-1] == original
    assert isinstance(second[-1], HumanMessage)
    assert "100 → 80" in second[-1].content
    assert second[-1].additional_kwargs[PER_CALL_KEY] is True


def test_claude_런은_지난_요약을_지우지_않고_그_자리에_다시_넣는다():
    """지우면 대화 기록을 고친 것이라 그 자리부터 cache 를 다시 쓴다. 그 턴에만 보이게 표시해 남긴다."""
    from langchain_core.messages import AIMessage

    from app.llm.cache_boundary import TURN_SCOPED_KEY

    pulse = PulseMemory()
    channel = SimpleNamespace(scene=SimpleNamespace(pulse=pulse))
    middleware = _latest_pulse_context_for(channel, keep_earlier=True)

    async def capture(call):
        return call.messages

    pulse.apply(PulseReading(reading=1, scene="Battle", active=[enemy(100)]))
    first_stored = [ToolMessage(content="first", tool_call_id="a", id="t1")]
    first = asyncio.run(middleware.awrap_model_call(SimpleNamespace(messages=first_stored, override=lambda **f: SimpleNamespace(**f)), capture))
    pulse.apply(PulseReading(reading=2, scene="Battle", active=[enemy(80)]))
    second_stored = [*first_stored, AIMessage(content="", id="a1"), ToolMessage(content="second", tool_call_id="b", id="t2")]
    second = asyncio.run(middleware.awrap_model_call(SimpleNamespace(messages=second_stored, override=lambda **f: SimpleNamespace(**f)), capture))

    # 요청에 실리는 내용은 같다. 표시(per_call)는 가장 새 요약에만 남는다 — 저장된 대화 끝 경계가 그 앞에 선다.
    assert [message.content for message in second[:2]] == [message.content for message in first]
    assert [message.id for message in second[2:4]] == ["a1", "t2"]
    assert all(message.additional_kwargs.get(TURN_SCOPED_KEY) for message in (second[1], second[4]))
    assert second[4].additional_kwargs[PER_CALL_KEY] is True
    assert PER_CALL_KEY not in second[1].additional_kwargs
