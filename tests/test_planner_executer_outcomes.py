"""행동 결과의 뜻: 효과를 본 것만 성공이고, 막힌 까닭을 가른다.

`artel/docs/architecture/2026-11-02-pe-mcp-structural-review.md` 의 P1 넷. 불변식: 실행 전에 읽지 못한 조건은 transition 의
증거가 아니다, 효과가 안 난 행동은 `after` 로 다음 행동을 강제하지 않는다, 읽히지 않는 등록 계약은 계약 없이 돌지 않는다,
준비된 행동이 없을 때 주소가 틀린 것으로 보이면 그렇다고 말하고 계획을 다시 쓸 기회는 그대로 준다.

이 파일은 `artel/docs/architecture/2026-11-02-pe-comprehensive-audit.md` 의 F1·F4·F6: 효과가 안 난 계약 행동은 Jev·Evaluator·ledger
모두에게 그렇게 보이고, 주소 의심은 준비 조건이 읽는 주소와 켜진 객체로만 하며, 효과가 난 반복은 제자리걸음이 아니다.
"""

import asyncio
import itertools
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.agents.evaluator.verdict import turn_line
from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.contract import EffectContract
from app.agents.planner_executer.developer_macros import DeveloperMacro, bind_guidance_calls, load_developer_macros
from app.agents.planner_executer.executer import ActionRun, Executer, StepPlay, address_hints, ready_source, recent_action_lines
from app.agents.planner_executer.ledger import FailureKind, classify
from app.agents.planner_executer.macros import RegisteredMacros, parse_guidance_step
from app.agents.qa.macro.errors import MacroRejection
from app.qa.jev import JevClient
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_guidance import CONTRACT, DONE, STRIKE, _battle
from tests.test_planner_executer_steps import FakeJev

REST = '''def rest() -> None:
    """Rest a moment."""
    press_key("Space", 0.1)
'''

NEXT_LINE = '''def next_line() -> None:
    """Show the next dialogue line."""
    press_key("Space", 0.05)


def ready_next_line() -> bool:
    return exists(selector("ChatWindow/Prompt"))


def effect_next_line() -> bool:
    return absent(selector("ChatWindow/Prompt"))
'''


def _spec(**overrides) -> PlannerExecuterArchSpec:
    return PlannerExecuterArchSpec(**({"no_action_grace_seconds": 0.05, "state_source": "pulse_memory"} | overrides))


async def _step(fake: FakeGame, macros: tuple[DeveloperMacro, ...], names: list[str]):
    fake.push_reading()
    loaded = await load_developer_macros(macros)
    actions, contracts, binder = await bind_guidance_calls(names, loaded)
    return await parse_guidance_step(DONE, RegisteredMacros(fake.channel), actions, contracts, binder, "Play it.")


def test_an_effect_that_could_not_be_read_before_the_action_is_not_a_transition() -> None:
    async def judge(before: str) -> tuple[dict, ActionRun]:
        engine = object.__new__(Executer)
        ticks = itertools.count()
        engine._clock = lambda: float(next(ticks))

        async def met(*_args) -> str:
            return "met"

        engine._predicate = met
        record: dict = {}
        run = await engine._judge_contract(ActionRun("passed", None, []), (object(), EffectContract("transition", 0.1), before), 1, record)
        return record, run

    record, run = asyncio.run(judge("unknown"))
    assert record["result"] == "unobserved" and run.outcome == "unobserved"

    record, run = asyncio.run(judge("unmet"))
    assert record["result"] == "effect_met" and run.outcome == "passed"


def test_a_registered_contract_that_no_longer_reads_is_not_run_without_it() -> None:
    async def bind() -> None:
        loaded = await load_developer_macros(
            (DeveloperMacro("rest", REST, "1.0", None, False, {"params": {}, "effect": {"mode": "invalid"}}),)
        )
        assert loaded.usable["rest"].contract is None and loaded.usable["rest"].contract_error
        await bind_guidance_calls(["rest"], loaded)

    with pytest.raises(MacroRejection, match="registered contract cannot be read"):
        asyncio.run(bind())


def test_an_action_whose_effect_did_not_come_does_not_force_the_next_one() -> None:
    fake = _battle()
    fake.reactions.clear()  # 클릭해도 맞지 않는다 — 효과가 안 난다.
    jev = FakeJev(picks=("developer:strike @ Enemy(Clone)[1]", "developer:strike @ Enemy(Clone)[2]"))

    async def go() -> StepPlay:
        macros = await _step(
            fake,
            (DeveloperMacro("strike", STRIKE, "1.0", None, False, CONTRACT), DeveloperMacro("rest", REST, "1.0", None, False)),
            ["strike", "rest"],
        )
        macros = replace(macros, after={"developer:strike": ("developer:rest",)})
        return await Executer(fake.channel, _spec(max_turns_per_step=2), JevClient(post=jev)).play(1, "Clear the wave.", macros)

    first, second = asyncio.run(go()).turns[:2]

    assert first["result"] == "effect_unmet"
    assert "after" not in second and any(option.startswith("developer:strike") for option in second["options"])


def _dialogue() -> FakeGame:
    return FakeGame("Story", {"Canvas[0]/ChatWindow[0]/Prompt[0]": {"path": "Canvas/ChatWindow/Prompt", "text": "Next"}})


def _stuck(fake: FakeGame, source: str, can_replan: bool) -> StepPlay:
    contract = {"params": {}, "effect": {"mode": "state"}}

    async def go() -> StepPlay:
        macros = await _step(fake, (DeveloperMacro("next_line", source, "1.0", None, False, contract),), ["next_line"])
        spec = _spec(max_turns_per_step=5, max_replans_per_step=1)
        return await Executer(fake.channel, spec, JevClient(post=FakeJev(picks=()))).play(1, "Read on.", macros, can_replan=can_replan)

    return asyncio.run(go())


def test_nothing_ready_because_of_an_address_missing_its_front_says_so_and_still_replans() -> None:
    play = _stuck(_dialogue(), NEXT_LINE, can_replan=True)

    assert "suspected address mismatch" in play.stopped and "Canvas/ChatWindow/Prompt" in play.stopped
    assert play.replan_requested is True and play.stopped.startswith("REPLAN: ")
    assert play.unseen_selectors == ["ChatWindow/Prompt"]


def test_an_address_only_the_effect_reads_raises_no_suspicion() -> None:
    ready_reads_right = NEXT_LINE.replace('exists(selector("ChatWindow/Prompt"))', 'text(selector("Canvas/ChatWindow/Prompt")) == "Done"')

    kept = ready_source(ready_reads_right, "next_line")
    assert kept.startswith("def ready_next_line") and "effect_next_line" not in kept
    play = _stuck(_dialogue(), ready_reads_right, can_replan=True)
    assert "suspected" not in play.stopped and play.replan_requested is True


def test_a_switched_off_object_is_not_an_address_suspect() -> None:
    def pulse(live: bool) -> SimpleNamespace:
        return SimpleNamespace(held={"x": SimpleNamespace(path="Old/Prompt", selector="Old[0]/Prompt[0]", live=live)}, gone_at={})

    assert address_hints('selector("Prompt")', pulse(live=False)) == {}
    assert address_hints('selector("Prompt")', pulse(live=True)) == {"Prompt": ["Old/Prompt"]}


def test_a_contract_effect_that_did_not_come_reads_as_such_to_jev_the_evaluator_and_the_ledger() -> None:
    turn = {"turn": 1, "scene": "Test", "done": False, "picked": "developer:move", "outcome": "passed", "result": "effect_unmet", "sent": [{}]}

    assert recent_action_lines([turn]) == ["turn 1: developer:move → passed, sent 1 input to the game; its effect did not show"]
    assert "engine result: effect_unmet" in turn_line(turn)
    assert classify(turn) is FailureKind.EFFECT_UNMET
    assert classify(turn | {"result": "effect_met"}) is None


def test_an_action_repeated_with_its_effect_showing_each_time_is_not_stuck() -> None:
    fake = _battle()

    def hit(index: int):
        def react(game) -> None:
            game.objects[f"Enemy(Clone)[{index}]"]["members"]["Enemy.hit"] = True
        return react

    fake.reactions[("press", "Enemy(Clone)[1]")] = hit(1)
    fake.reactions[("press", "Enemy(Clone)[2]")] = hit(2)
    contract = {**CONTRACT, "effect": {"mode": "state", "within": 0.1}}
    jev = FakeJev(picks=("developer:strike @ Enemy(Clone)[1]", "developer:strike @ Enemy(Clone)[2]", "developer:strike @ Enemy(Clone)[1]"))

    async def go() -> StepPlay:
        macros = await _step(fake, (DeveloperMacro("strike", STRIKE, "1.0", None, False, contract),), ["strike"])
        spec = _spec(max_turns_per_step=3, max_same_action_in_a_row=2)
        return await Executer(fake.channel, spec, JevClient(post=jev)).play(1, "Hit them.", macros, can_ask_planner=True)

    play = asyncio.run(go())
    assert [turn.get("result") for turn in play.turns[:2]] == ["effect_met", "effect_met"]
    assert not (play.stopped or "").startswith("REPAIR: `developer:strike` was picked")


def test_nothing_ready_for_another_reason_asks_for_a_new_plan_when_the_budget_allows() -> None:
    right = NEXT_LINE.replace('exists(selector("ChatWindow/Prompt"))', 'text(selector("Canvas/ChatWindow/Prompt")) == "Done"').replace(
        'absent(selector("ChatWindow/Prompt"))', 'absent(selector("Canvas/ChatWindow/Prompt"))'
    )

    play = _stuck(_dialogue(), right, can_replan=True)
    assert play.replan_requested is True and play.stopped.startswith("REPLAN: no action was ready")

    play = _stuck(_dialogue(), right, can_replan=False)
    assert play.replan_requested is False and play.stopped.startswith("no action was ready")


def test_a_failure_just_before_a_replan_still_reaches_jev_after_it() -> None:
    fake = _battle()
    jev = FakeJev(picks=("developer:strike @ Enemy(Clone)[1]",))
    earlier = {"turn": 3, "scene": "Battle", "done": False, "picked": "developer:strike @ Enemy(Clone)[2]", "outcome": "passed",
               "result": "effect_unmet", "sent": [{}]}

    async def go() -> StepPlay:
        macros = await _step(fake, (DeveloperMacro("strike", STRIKE, "1.0", None, False, CONTRACT),), ["strike"])
        return await Executer(fake.channel, _spec(max_turns_per_step=1), JevClient(post=jev)).play(1, "Clear the wave.", macros, history=[earlier])

    first = asyncio.run(go()).turns[0]

    assert first["recent_actions"] == [
        "before the new plan, turn 3: developer:strike @ Enemy(Clone)[2] → passed, sent 1 input to the game; its effect did not show"
    ]


def test_a_source_held_back_for_an_unseen_object_comes_back_once_the_object_is_reported() -> None:
    from app.agents.planner_executer.ledger import ActionLedger, FailureKind, LedgerEntry

    ledger = ActionLedger([LedgerEntry("developer:next_line", "v1", FailureKind.STRUCTURAL, 2, 4, None, "WAIT_TIMEOUT", ("Late/Prompt",))])

    def pulse(*paths: str) -> SimpleNamespace:
        return SimpleNamespace(held={path: SimpleNamespace(path=path, selector=path, live=True) for path in paths}, gone_at={})

    assert ledger.structural(pulse("Canvas/Other")) and "next_line" in ledger.lessons(pulse("Canvas/Other"))
    assert ledger.structural(pulse("Late/Prompt")) == {} and ledger.lessons(pulse("Late/Prompt")) == ""
    # pulse 를 모르면 전처럼 막는다.
    assert ledger.structural()
