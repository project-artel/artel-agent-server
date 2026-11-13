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
