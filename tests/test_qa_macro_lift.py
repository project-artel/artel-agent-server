"""손으로 보낸 action 을 기록하고 macro 초안으로 올리는 것을 재는 테스트 (ARTEL-915, ARTEL-916).

세 층이다. target 하나를 바꿔 올리는 것, 한 step 의 기록을 초안 하나로 만드는 것, 그리고
실제 tool 로 손으로 누른 뒤 `report_step` 의 답에 초안이 붙는 것.
"""

import asyncio

import pytest

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.macro.lift import (
    DispatchRecord,
    LiftedTarget,
    draft_for_step,
    lift_target,
)
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.tools import QaRunState, build_tools
from app.llm.models import LLMModel
from app.qa.channel import QaRunChannel
from app.qa.pulse import PulseMemory, PulseReading


def pulse(*objects: dict, scene: str = "Battle") -> PulseMemory:
    memory = PulseMemory()
    memory.apply(
        PulseReading.model_validate(
            {"scene": scene, "whole": True, "active": list(objects)}
        )
    )
    return memory


def obj(selector: str, instance_id: int, text: str | None = None, rect: dict | None = None) -> dict:
    return {"selector": selector, "id": instance_id, "text": text, "rect": rect}


BOARD = pulse(
    obj("Root[0]/Canvas[1]/CombineButton[2]", 51, "Combine", {"x": 100, "y": 100, "w": 80, "h": 30}),
    obj("Root[0]/Hand[2]/Card(Clone)[3]", 41, "shoot", {"x": 10, "y": 400, "w": 60, "h": 90}),
    obj("Root[0]/Hand[2]/Card(Clone)[4]", 42, "slash", {"x": 80, "y": 400, "w": 60, "h": 90}),
    obj("Root[0]/Hand[2]/Card(Clone)[5]", 43, "slash", {"x": 150, "y": 400, "w": 60, "h": 90}),
    obj("Root[0]/Canvas[1]", 1, None, {"x": 0, "y": 0, "w": 1280, "h": 720}),
)


# --- target 하나 ------------------------------------------------------------------


def test_an_id_on_a_fixed_path_lifts_to_its_selector() -> None:
    assert lift_target("#51", BOARD) == LiftedTarget("selector", "Root[0]/Canvas[1]/CombineButton[2]")


def test_an_id_on_a_spawned_object_with_a_unique_label_lifts_to_find_label() -> None:
    """경로 selector 는 앞의 형제가 사라지면 다른 카드를 가리킨다. spawn 된 것은 글자로 찾는다."""
    assert lift_target("#41", BOARD) == LiftedTarget("label", "shoot")


def test_a_spawned_object_whose_label_and_name_repeat_is_not_lifted() -> None:
    """`slash` 가 두 장이고 이름도 둘 다 `Card(Clone)` 이다. 지어내지 않는다."""
    reason = lift_target("#42", BOARD)

    assert isinstance(reason, str) and "not unique" in reason


def test_a_coordinate_lifts_to_the_smallest_object_under_it() -> None:
    """Canvas 전체와 버튼이 둘 다 그 좌표를 덮는다. 작은 쪽이 누른 대상이다."""
    assert lift_target("140,115", BOARD) == LiftedTarget("selector", "Root[0]/Canvas[1]/CombineButton[2]")


def test_a_coordinate_with_nothing_under_it_is_not_lifted() -> None:
    reason = lift_target("2000,2000", BOARD)

    assert isinstance(reason, str) and "nothing" in reason


def test_an_unknown_id_is_not_lifted() -> None:
    assert isinstance(lift_target("#999", BOARD), str)


def test_a_fixed_path_the_pulse_has_not_reported_is_trusted_as_written() -> None:
    assert lift_target("Root[0]/Menu[3]/Start[0]", BOARD) == LiftedTarget("selector", "Root[0]/Menu[3]/Start[0]")


# --- 한 step 의 초안 ------------------------------------------------------------------


def press(step: int, key: str = "Space", landed: bool = True) -> DispatchRecord:
    return DispatchRecord(
        tool="press_key", step=step, scene="Story",
        arguments={"key_code": key, "duration_seconds": 0.1}, landed=landed,
    )


def click(step: int, target: LiftedTarget | None, landed: bool = True, unliftable: str = "") -> DispatchRecord:
    return DispatchRecord(
        tool="click", step=step, scene="Battle",
        arguments={"target": "#51", "button": 0},
        targets={"target": target} if target else {}, unliftable=unliftable, landed=landed,
    )


def test_repeated_key_presses_fold_into_a_range_loop() -> None:
    """대화를 넘기는 키 연타. L1 의 손조작 중 가장 많은 것이 이것이다."""
    draft, why = draft_for_step([press(3) for _ in range(5)], 3, set())

    assert why == ""
    assert "for _ in range(5):" in draft.source
    assert 'press_key("Space", 0.1)' in draft.source
    assert 'require(scene() == "Story"' in draft.source
    macro_definition_from_source(draft.name, draft.source)


def test_a_spawned_target_is_bound_with_find_right_before_its_use() -> None:
    records = [
        click(5, LiftedTarget("label", "shoot")),
        click(5, LiftedTarget("selector", "Root[0]/Canvas[1]/CombineButton[2]")),
    ]

    draft, _why = draft_for_step(records, 5, set())

    lines = draft.source.splitlines()
    find_line = next(i for i, line in enumerate(lines) if 'find(label="shoot")' in line)
    assert "click(t0_target" in lines[find_line + 1]
    assert 'click(selector("Root[0]/Canvas[1]/CombineButton[2]"), 0)' in draft.source


def test_a_press_that_reached_nothing_is_left_out() -> None:
    """빈 자리를 누른 것은 해 본 것이지 한 것이 아니다."""
    records = [press(2), click(2, LiftedTarget("selector", "A/B"), landed=False), press(2, "Return")]

    draft, _why = draft_for_step(records, 2, set())

    assert "click" not in draft.source
    assert draft.actions == 2


def test_one_call_that_cannot_be_lifted_stops_the_whole_draft() -> None:
    """하나를 빼고 만든 초안은 다른 일을 하는 macro 다."""
    records = [press(4), click(4, None, unliftable="nothing the game has reported lies under `9,9`")]

    draft, why = draft_for_step(records, 4, set())

    assert draft is None
    assert "9,9" in why


def test_a_single_action_is_not_a_sequence() -> None:
    assert draft_for_step([press(1)], 1, set()) == (None, "")


def test_only_the_named_step_is_drafted() -> None:
    draft, _why = draft_for_step([press(1), press(1), press(2), press(2), press(2)], 2, set())

    assert "range(3)" in draft.source


def test_a_taken_name_gets_a_suffix() -> None:
    draft, _why = draft_for_step([press(1), press(1)], 1, {"replay_step_1"})

    assert draft.name == "replay_step_1_2"


# --- 실제 tool 로: 손으로 누르고, 판정하고, 초안을 받는다 ------------------------------


def make(macros: str):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=9, send=send, action_timeout=0.05, write_timeout=0.05)
    channel.scene.pulse.apply(
        PulseReading.model_validate(
            {
                "scene": "Battle",
                "whole": True,
                "active": [
                    obj("Root[0]/Canvas[1]/CombineButton[2]", 51, "Combine", {"x": 100, "y": 100, "w": 80, "h": 30}),
                    obj("Root[0]/Hand[2]/Card(Clone)[3]", 41, "shoot", {"x": 10, "y": 400, "w": 60, "h": 90}),
                ],
            }
        )
    )
    state = QaRunState(total_steps=3)
    arch = resolve_arch(
        QaArchSpec(vision=VisionMode.on, phase_cycle=PhaseCycleMode.off, macros=macros),
        LLMModel.gpt_6_luna,
    )
    tools = {one.name: one for one in build_tools(channel, state, arch)}
    return state, tools


def run_step(tools) -> str:
    async def scenario():
        for _ in range(3):
            await tools["press_key"].ainvoke({"step": 1, "thought": "t", "key_code": "Space", "duration_seconds": 0.1})
        await tools["click"].ainvoke({"step": 1, "thought": "t", "target": "#41"})
        return await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "dialogue advanced", "thought": "t"}
        )

    return asyncio.run(scenario())


def test_hand_sent_actions_are_recorded_in_order_with_their_targets_lifted() -> None:
    state, tools = make("on")
    run_step(tools)

    assert [record.tool for record in state.dispatches] == ["press_key"] * 3 + ["click"]
    assert state.dispatches[-1].targets["target"] == LiftedTarget("label", "shoot")


def test_report_step_offers_a_draft_that_one_register_call_keeps() -> None:
    state, tools = make("on")

    answer = run_step(tools)

    assert "Macro draft ready" in answer and "replay_step_1" in answer
    assert "for _ in range(3):" in answer
    assert state.macros.draft("replay_step_1") is not None
    registered = asyncio.run(
        tools["register_macro"].ainvoke({"step": 1, "thought": "t", "name": "replay_step_1"})
    )
    assert state.macros.registered("replay_step_1") is not None, registered


def test_a_run_with_macros_off_is_never_offered_a_draft() -> None:
    state, tools = make("off")

    answer = run_step(tools)

    assert "Macro draft" not in answer
    assert state.macros.drafts == {}


@pytest.mark.parametrize("macros", ["on", "off"])
def test_recording_does_not_change_what_the_model_is_offered(macros: str) -> None:
    """기록하는 wrapper 가 tool 의 이름과 `args` 를 그대로 둔다 — fingerprint 가 안 움직인다."""
    _state, tools = make(macros)

    assert set(tools["click"].args) == {"step", "target", "thought", "button"}
