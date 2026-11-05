"""런 단위 행동 장부, 수정 단위 복구, 준비 조건, 효과 기준값."""

import asyncio

import pytest

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.executer import Executer, StepPlay
from app.agents.planner_executer.ledger import ActionLedger, FailureKind, classify
from app.agents.planner_executer.macros import RegisteredMacros, parse_step
from app.agents.planner_executer.plan_edit import PlanDraft, merge, read_changes
from app.agents.qa.macro.errors import MacroRejection
from app.qa.jev import JevClient
from tests.test_planner_executer_steps import MACROS, FakeJev, _menu


def test_failures_are_sorted_by_kind() -> None:
    assert classify({"outcome": "failed", "reason": "WAIT_TIMEOUT at x", "unseen_selectors": ["A/B"]}) == FailureKind.STRUCTURAL
    assert classify({"outcome": "failed", "reason": "SCENE_MISMATCH at x: require(...)"}) == FailureKind.PRECONDITION
    assert classify({"outcome": "failed", "reason": "SELECTOR_NOT_FOUND at x"}) == FailureKind.TARGET_GONE
    assert classify({"outcome": "unobserved", "reason": "ACTION_REJECTED"}) == FailureKind.UNOBSERVED
    assert classify({"outcome": "passed", "effect": "unmet"}) == FailureKind.EFFECT_UNMET
    assert classify({"outcome": "passed", "effect": "already"}) is None
    assert classify({"outcome": "failed", "reason": "WAIT_TIMEOUT at x"}) == FailureKind.TIMEOUT


def _macros(source: str = MACROS):
    fake = _menu()

    async def go():
        fake.push_reading()
        return await parse_step(source, RegisteredMacros(fake.channel))

    return asyncio.run(go())


def test_only_a_structural_defect_of_the_same_source_blocks_the_action() -> None:
    macros = _macros()
    ledger = ActionLedger()
    play = StepPlay("x", turns=[
        {"turn": 0, "picked": "press_start", "outcome": "failed", "reason": "SCENE_MISMATCH at x"},
        {"turn": 1, "picked": "look_around", "outcome": "failed", "reason": "WAIT_TIMEOUT at y",
         "unseen_selectors": ["Root/Ghost"]},
    ])
    ledger.record(4, play, macros, None)

    refusal = ledger.check(macros, None)
    assert refusal is not None and refusal.startswith("PLAN_CHECK:") and "`look_around`" in refusal
    assert "`press_start`" not in refusal
    assert "`look_around`: WAIT_TIMEOUT at y (step 4)" in ledger.lessons()
    # 고친 수정본(다른 소스)은 막지 않는다.
    fixed = _macros(MACROS.replace('wait_until(scene() == "Menu", 1, "Not on the menu.")', 'wait_until(scene() == "Menu", 2, "Not yet.")'))
    assert ledger.check(fixed, None) is None


DRAFT = PlanDraft(
    version=2,
    source=MACROS,
    calls=(("developer:tap", "tap"), ("developer:tap#2", 'tap(find(name="X"))')),
    actions=("press_start", "look_around", "developer:tap", "developer:tap#2"),
    after_text='{"press_start": ["look_around"]}',
)


def test_a_rewrite_keeps_what_it_does_not_name_and_drops_what_it_replaces() -> None:
    changes = read_changes(
        '{"base": 2, "replace": {"look_around": "it never settles"}, "remove": {"developer:tap#2": "wrong target"}}', DRAFT
    )
    new = 'def wait_menu() -> None:\n    """Wait for the menu."""\n    wait_until(scene() == "Menu", 1, "No menu.")\n'

    source, calls = merge(DRAFT, changes, new, ["other"])

    assert "def press_start()" in source and "def done()" in source and "def wait_menu()" in source
    assert "def look_around()" not in source
    assert calls == ["tap", "other"]


@pytest.mark.parametrize(
    ("text", "said"),
    [
        (None, "a rewrite answers with a ```changes block"),
        ('{"base": 1}', "based on plan 1, but the plan now is 2"),
        ('{"base": 2, "remove": {"nope": "x"}}', "not an action of the plan"),
        ('{"base": 2, "remove": {"press_start": ""}}', "maps each action to the reason"),
    ],
)
def test_a_changes_block_is_refused_when_it_cannot_apply(text, said) -> None:
    with pytest.raises(MacroRejection) as refused:
        read_changes(text, DRAFT)
    assert said in str(refused.value)


READY = MACROS + '''

def ready_press_start() -> bool:
    return scene() == "Nowhere"
'''


def test_an_action_whose_ready_predicate_is_false_is_not_offered() -> None:
    fake = _menu()
    jev = FakeJev(picks=("look_around",))
    spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.2, state_source="pulse_memory", max_turns_per_step=2)

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(READY, RegisteredMacros(fake.channel))
        return await Executer(fake.channel, spec, JevClient(post=jev)).play(1, "Press Start.", macros)

    play = asyncio.run(go())

    assert play.turns[0]["not_ready"] == ["press_start"]
    assert "press_start" not in play.turns[0]["options"]


ALREADY = MACROS + '''

def effect_press_start() -> bool:
    return scene() != "Nowhere"
'''


def test_an_effect_that_already_held_before_the_action_is_not_counted_as_its_effect() -> None:
    fake = _menu()
    spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.2, state_source="pulse_memory")

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(ALREADY, RegisteredMacros(fake.channel))
        return await Executer(fake.channel, spec, JevClient(post=FakeJev())).play(1, "Press Start.", macros)

    play = asyncio.run(go())

    assert play.turns[0]["picked"] == "press_start" and play.turns[0]["effect"] == "already"
    assert not play.turns[0].get("misses")
