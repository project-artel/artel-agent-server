"""Planner·editor 가 SDK `list_inputs` 로 이 빌드가 읽는 입력을 읽는다 (ARTEL-1432).

매크로를 쓰는 세 자리 — step 계획, FREE 의 scene 계획, editor 의 다시 쓰기 — 가 같은 절을 읽는지, 옛 SDK 가 거절하면
그 런에서 다시 묻지 않는지, 빈 목록을 "읽지 않는다"로 쓰지 않는지를 본다. `FakeGame` 위에서 돈다.
"""

import asyncio

from app.agents.editor import MacroEditor
from app.agents.planner_executer.inputs import MAX_ITEMS, BuildInputs, InputsReader, fetch_inputs, inputs_text
from app.agents.planner_executer.macros import RegisteredMacros, parse_step
from app.agents.planner_executer.planner import Planner
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.qa.jev import JevClient
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_free import MENU_CASE, Raw, _free, _plan
from tests.test_planner_executer_steps import MACROS, STUCK, Asked, FakeJev, _config, _judge, _menu, _scenario

RED_RUNNER = {
    "keys": ["Escape"],
    "otherKeys": False,
    "axes": ["Horizontal", "Jump"],
    "actions": [],
}
SOKOBAN = {
    "keys": [],
    "otherKeys": False,
    "axes": [],
    "actions": [{"name": "Player/Move", "keys": ["W", "S", "A", "D"]}, {"name": "Player/Undo", "keys": ["U"]}],
}


def _answering(answer: dict) -> FakeGame:
    fake = _menu()
    fake.answers["list_inputs"] = lambda params: answer
    return fake


# --- 절의 모양 -------------------------------------------------------------------------------------


def test_the_section_names_keys_axes_and_enabled_actions_with_the_action_each_takes() -> None:
    text = inputs_text(BuildInputs.model_validate({**RED_RUNNER, "actions": SOKOBAN["actions"], "otherKeys": True}))

    assert text.startswith("Inputs this build reads (from the SDK):\n")
    assert "`Input.GetKey*` (send with `press_key`/`hold_key`): Escape" in text
    assert "a key that is not listed may still work" in text
    assert "A key press never reaches these" in text and "`set_input_button(" in text and ": Horizontal, Jump" in text
    assert "  - Player/Move: W, S, A, D\n  - Player/Undo: U" in text


def test_empty_lists_are_unknown_not_unread_so_the_section_is_left_out() -> None:
    assert inputs_text(BuildInputs()) is None
    assert inputs_text(None) is None
    # 키가 묶이지 않은 action 은 실을 것이 없다.
    assert inputs_text(BuildInputs.model_validate({"actions": [{"name": "UI/Point", "keys": []}]})) is None


def test_long_lists_are_cut_and_say_how_many_are_left() -> None:
    names = [f"Key{index}" for index in range(MAX_ITEMS + 3)]
    actions = [{"name": f"Map/Action{index}", "keys": ["A"]} for index in range(MAX_ITEMS + 2)]

    text = inputs_text(BuildInputs.model_validate({"keys": names, "actions": actions}))

    assert f"Key{MAX_ITEMS - 1}, … 3 more" in text and f"Key{MAX_ITEMS}," not in text
    assert "  - … 2 more actions" in text


# --- SDK 에 묻기 ----------------------------------------------------------------------------------


def test_the_sdk_answer_is_read_a_refusal_means_an_old_sdk() -> None:
    answered = _answering(RED_RUNNER)
    old = _menu()
    old.old_sdk = True

    async def go():
        return await fetch_inputs(answered.channel), await fetch_inputs(old.channel)

    read, refused = asyncio.run(go())

    assert read.supported and read.inputs.axes == ["Horizontal", "Jump"]
    assert not refused.supported and refused.inputs is None
    # 질의는 게임을 바꾸는 action 기록에 섞이지 않는다.
    assert answered.actions == [] and answered.queries == ["list_inputs"]


def test_a_failure_that_is_not_a_refusal_or_a_bad_answer_is_unknown_this_time_only() -> None:
    failing = _menu()
    failing.refuse.add(("list_inputs", None))
    misshapen = _answering({"actions": None})

    async def go():
        return await fetch_inputs(failing.channel), await fetch_inputs(misshapen.channel)

    failed, bad = asyncio.run(go())

    assert failed.supported and failed.inputs is None
    assert bad.supported and bad.inputs is None


def test_an_old_sdk_is_asked_once_per_run() -> None:
    fake = _menu()
    fake.old_sdk = True
    reader = InputsReader(fake.channel)

    async def go():
        return [await reader.read() for _ in range(3)]

    assert asyncio.run(go()) == [None, None, None]
    assert fake.queries == ["list_inputs"]


# --- 매크로를 쓰는 세 자리 ---------------------------------------------------------------------------


def test_the_planner_reads_the_inputs_above_the_step_it_writes() -> None:
    fake = _answering(RED_RUNNER)
    asked = Asked(MACROS)
    config = _config()

    async def go():
        fake.push_reading()
        planner = Planner(config, fake.channel, config.arch.planner, RegisteredMacros(fake.channel), ask=asked)
        return await planner.write(_scenario(), 1)

    asyncio.run(go())

    ((_, user),) = asked.calls
    assert "Inputs this build reads (from the SDK):" in user
    assert user.index("Inputs this build reads") < user.index("Write step 1 now:")
    assert fake.queries == ["list_inputs"]


def test_the_planner_leaves_the_section_out_when_the_sdk_is_old() -> None:
    fake = _menu()
    fake.old_sdk = True
    asked = Asked(MACROS, MACROS)
    config = _config()

    async def go():
        fake.push_reading()
        planner = Planner(config, fake.channel, config.arch.planner, RegisteredMacros(fake.channel), ask=asked)
        await planner.write(_scenario(), 1)
        await planner.write(_scenario(), 1)

    asyncio.run(go())

    assert all("Inputs this build reads" not in user for _, user in asked.calls)
    assert fake.queries == ["list_inputs"]


def test_a_scene_plan_reads_the_inputs_too() -> None:
    fake = _answering(SOKOBAN)
    asked = Raw(_plan([MENU_CASE.case_id]))
    config = _config()

    async def go():
        fake.push_reading()
        planner = Planner(config, fake.channel, config.arch.planner, RegisteredMacros(fake.channel), ask=asked)
        return await planner.plan_scene(_free(MENU_CASE), [1])

    plan = asyncio.run(go())

    assert plan.macros is not None
    ((_, user),) = asked.calls
    assert "  - Player/Undo: U" in user
    assert user.index("Inputs this build reads") < user.index("Scene on screen:")


def test_the_editor_reads_the_inputs_when_it_rewrites_the_macros() -> None:
    fake = _answering(RED_RUNNER)
    asked = Asked(MACROS)
    config = _config()
    turns = [{"step": 1, "turn": 3, "scene": "Menu", "picked": "press_start", "outcome": "passed", "sent": []}]

    async def go():
        fake.push_reading()
        registered = RegisteredMacros(fake.channel)
        macros = await parse_step(STUCK, registered)
        editor = MacroEditor(config, fake.channel, config.arch.planner, registered, ask=asked)
        return await editor.repair(_scenario(), 1, macros, "press_start", turns)

    repaired = asyncio.run(go())

    assert repaired.refusal is None
    ((_, user),) = asked.calls
    assert ": Horizontal, Jump" in user
    assert user.index("Inputs this build reads") < user.index("Current screen:")


def test_the_planner_and_the_editor_share_one_reader_so_an_old_sdk_is_asked_once() -> None:
    fake = _menu()
    fake.old_sdk = True
    asked = Asked(STUCK, MACROS)  # the planner's answer, then the editor's
    judge, _ = _judge({"passed": True, "reason": "The map opened.", "state_detail": "", "insight": ""})
    config = _config(no_action_grace_seconds=0.2, repair_after=2, analysis="off")

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=asked, judge=judge, jev=JevClient(post=FakeJev()))
        return await runner.run_with_deadline(fake.channel, _scenario())

    state, failure = asyncio.run(go())

    assert failure is None and state.step_results[0].passed
    assert asked.calls[1][0].startswith("You fix the macros of ONE test step")
    assert fake.queries == ["list_inputs"]
