"""`report_step_view` knob (ARTEL-960).

`report_step` 은 게임을 조작하지 않는다. knob 을 끄면 그 답에서 `pulse` view 가 빠지고, 그 사이
일어난 일은 다음 view 가 이어서 말한다.
"""

import asyncio

import app.agents  # noqa: F401  (import order: avoids a circular import through app.qa)
from app.agents.qa.arch import PhaseCycleMode, default_resolved_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.qa.channel import QaRunChannel
from app.qa.pulse import PulseMemory, PulseReading


def _pulse(reading: int, frame: int, objects: list[dict], whole: bool = False, gone=(), changed=()):
    return {
        "schema": 2,
        "reading": reading,
        "frame": frame,
        "scene": "Battle",
        "whole": whole,
        "active": objects,
        "deactive": [],
        "gone": list(gone),
        "changed": list(changed),
    }


def _enemy(selector: str, hp: int) -> dict:
    return {
        "scene": "Battle",
        "id": 1,
        "path": selector,
        "selector": selector,
        "members": [{"on": "Combat.Enemy", "member": "Hp", "value": hp, "asked": True}],
    }


def _tools(report_step_view: bool):
    async def send(_frame: dict) -> None:
        return None

    channel = QaRunChannel(qa_try_id=7, send=send)
    state = QaRunState(total_steps=3)
    arch = default_resolved_arch().model_copy(
        update={"phase_cycle": PhaseCycleMode.off, "report_step_view": report_step_view}
    )
    tools = {tool.name: tool for tool in build_tools(channel, state, arch=arch)}
    return channel, tools


_VERDICT = {"step": 1, "passed": True, "message": "됐다", "thought": "HP 가 줄었다"}


def test_report_step_view_is_on_by_default() -> None:
    assert default_resolved_arch().report_step_view is True


def test_report_step_keeps_its_view_when_the_knob_is_on() -> None:
    channel, tools = _tools(report_step_view=True)
    channel.on_pulse({"payload": _pulse(1, 10, [_enemy("Slime[1]", 30)], whole=True)})

    answer = asyncio.run(tools["report_step"].ainvoke(_VERDICT))

    assert "<<pulse>>" in answer


def test_report_step_answers_with_one_line_when_the_knob_is_off() -> None:
    channel, tools = _tools(report_step_view=False)
    channel.on_pulse({"payload": _pulse(1, 10, [_enemy("Slime[1]", 30)], whole=True)})
    # 첫 view 는 scene 전환 page 다. 실제 run 에서 첫 action 결과가 이것을 그린다.
    channel.scene.pulse.since_action(None)
    channel.on_pulse({
        "payload": _pulse(2, 20, [_enemy("Slime[1]", 25)], changed=["Battle/Slime[1]|Combat.Enemy::Hp"])
    })

    answer = asyncio.run(tools["report_step"].ainvoke(_VERDICT))

    assert "<<pulse>>" not in answer
    assert "pulse: 1 values moved since the last view" in answer
    assert "continue with step 2" in answer


def test_a_deferred_value_comes_back_in_the_next_view_as_changed_earlier() -> None:
    memory = PulseMemory()
    memory.apply(PulseReading.model_validate(_pulse(1, 10, [_enemy("Slime[1]", 30)], whole=True)))
    memory.render(since=0)
    memory.apply(PulseReading.model_validate(_pulse(2, 20, [_enemy("Slime[1]", 25)])))
    memory.defer_view()
    memory.apply(PulseReading.model_validate(_pulse(3, 30, [])))

    view = memory.render(since=memory.after_frame(25))

    assert "Enemy.Hp = 25" in view and "(changed earlier)" in view


def test_an_object_gone_while_the_view_was_deferred_is_still_reported() -> None:
    """`gone` 은 window 를 탄다. 넘긴 자리의 소식을 다음 view 가 이어받지 않으면 영영 안 나온다."""

    def run(defer: bool) -> str:
        memory = PulseMemory()
        memory.apply(PulseReading.model_validate(
            _pulse(1, 10, [_enemy("Slime[1]", 30), _enemy("Rock[2]", 9)], whole=True)
        ))
        memory.render(since=0)
        memory.apply(PulseReading.model_validate(_pulse(2, 20, [], gone=["Battle/Rock[2]"])))
        if defer:
            memory.defer_view()
        memory.apply(PulseReading.model_validate(_pulse(3, 30, [_enemy("Slime[1]", 20)])))
        return memory.render(since=memory.after_frame(25))

    assert "gone from the scene: Rock[2]" in run(defer=True)
    # 넘기지 않은 run 에서 이 소식은 그 사이의 view 가 이미 말했어야 하는 것이다. 여기서는
    # 그 view 가 없었으므로 안 나온다 — 그래서 `defer_view` 가 필요하다.
    assert "Rock[2]" not in run(defer=False)
