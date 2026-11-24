"""pe12: 판정 step 이 기대 결과를 다 보이기 전에 끝나지 않는다.

- evaluator 는 기대 항목마다 판정하고, 통과는 코드가 모든 항목이 yes 일 때만 정한다.
- 화면을 말하는 항목은 게임 값만으로 yes 가 되지 않는다.
- case 의 판정 step 이 `done()` 으로 멈추면 그 자리에서 판정을 받는다. 근거 없는 항목만 있으면 planner 가 그 항목을 이유로
  step 을 다시 쓰고, 없으면 그 판정을 step 의 판정으로 쓴다.
"""

import asyncio

from app.agents.evaluator.verdict import Evaluator, ItemCheck, settle_checks
from app.agents.planner_executer.runner import PlannerExecuterRunner, verdict_step
from app.qa.jev import JevClient
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from tests.test_planner_executer_steps import PLAYED, MACROS, Asked, FakeJev, _config, _menu, _scenario, _verdicts


def _check(met: str, about: str = "game", shown_by: str = "game_value", expected: str = "x") -> ItemCheck:
    return ItemCheck(expected=expected, observed="seen", met=met, about=about, shown_by=shown_by)


def test_the_verdict_passes_only_when_every_expected_result_is_met() -> None:
    assert settle_checks([])[1] is None
    assert settle_checks([_check("yes"), _check("yes")])[1] is True
    assert settle_checks([_check("yes"), _check("no")])[1] is False
    assert settle_checks([_check("yes"), _check("unknown")])[1] is False


def test_a_screen_result_shown_by_a_game_value_and_an_object_on_screen_is_met() -> None:
    checks, passed = settle_checks([
        ItemCheck(expected="two cards come into the hand", observed="two cards present, count 2", met="yes",
                  about="screen", shown_by=["game_value", "screen_text"]),
    ])

    assert passed is True and checks[0].met == "yes"


def test_a_result_about_the_screen_is_not_met_by_a_game_value_alone() -> None:
    checks, passed = settle_checks([
        _check("yes", about="screen", shown_by="game_value", expected="the line shows in full"),
        _check("yes", about="screen", shown_by="screen_text", expected="the next line comes"),
    ])

    assert passed is False
    assert [check.met for check in checks] == ["unknown", "yes"]


VERDICT_WITH_CHECKS = {
    "passed": True, "reason": "The line went on.", "state_detail": "", "game_insight": "", "evidence_sufficient": True,
    "checks": [
        {"expected": "a typing line finishes at once", "observed": "no press while typing", "met": "unknown",
         "about": "screen", "shown_by": "none"},
        {"expected": "a finished line moves on", "observed": "text changed after the press", "met": "yes",
         "about": "screen", "shown_by": "screen_text"},
    ],
}


def test_the_evaluator_turns_an_unshown_result_into_a_failure_and_says_why() -> None:
    async def judge(system: str, user: str):
        assert '"checks"' in system
        return VERDICT_WITH_CHECKS

    verdict = asyncio.run(Evaluator(_config(), judge).judge(_scenario(), 1, PLAYED))

    assert verdict.passed is False and not verdict.evidence_sufficient
    assert verdict.missing == ("a typing line finishes at once",) and verdict.contradicted == ()
    assert "a typing line finishes at once" in verdict.message
    assert [check["met"] for check in verdict.details()["checks"]] == ["unknown", "yes"]


def test_only_the_last_step_of_a_case_with_an_expected_result_is_a_verdict_step() -> None:
    scenario = QaScenario(
        title="Two steps of one case",
        steps=[
            QaStep(action="Open.", case_id=5, case=QaCaseRef(id=5, expected="It opens.")),
            QaStep(action="Close.", case_id=5, case=QaCaseRef(id=5, expected="It opens.")),
            QaStep(action="Wait."),
        ],
    )

    assert [verdict_step(scenario, step) for step in (1, 2, 3)] == [False, True, False]


def _run(asked, judge, **knobs):
    config = _config(no_action_grace_seconds=0.2, analysis="off", **knobs)
    fake = _menu()

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=asked, judge=judge, jev=JevClient(post=FakeJev(picks=("press_start",))))
        return await runner.run_with_deadline(fake.channel, _scenario())

    state, failure = asyncio.run(go())
    return fake, state, failure


def test_a_verdict_step_whose_done_held_with_a_result_unshown_is_written_again_with_that_result() -> None:
    answers = [VERDICT_WITH_CHECKS, {**VERDICT_WITH_CHECKS, "passed": False}]
    judged: list[str] = []

    async def judge(system: str, user: str):
        judged.append(user)
        return answers.pop(0)

    asked = Asked(MACROS, MACROS, MACROS, MACROS)
    fake, state, failure = _run(asked, judge)

    assert failure is None and state.finished
    checked = [f["payload"]["decision"] for f in fake.frames("LOG") if f["payload"].get("kind") == "coverage_check"]
    assert len(checked) == 1 and checked[0]["missing"] == ["a typing line finishes at once"]
    # 다시 쓰는 planner 가 근거 없는 항목을 이유로 읽는다.
    assert any("a typing line finishes at once" in user for _system, user in asked.calls[1:])
    # 확인은 step 당 한 번이다. 다시 쓴 계획 뒤의 판정은 최종 판정 한 번이다.
    assert len(judged) == 2
    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"


def test_a_verdict_step_whose_results_are_all_shown_keeps_the_judgement_made_at_done() -> None:
    judged: list[str] = []

    async def judge(system: str, user: str):
        judged.append(user)
        return {**VERDICT_WITH_CHECKS, "checks": [{**VERDICT_WITH_CHECKS["checks"][1]}]}

    asked = Asked(MACROS)
    fake, state, failure = _run(asked, judge)

    assert failure is None and state.finished
    assert len(judged) == 1 and len(asked.calls) == 1
    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "COMPLETED"


def test_a_contradicted_result_is_a_verdict_not_a_reason_to_play_on() -> None:
    contradicted = {
        **VERDICT_WITH_CHECKS, "passed": False,
        "checks": [{**VERDICT_WITH_CHECKS["checks"][0]}, {**VERDICT_WITH_CHECKS["checks"][1], "met": "no"}],
    }

    async def judge(system: str, user: str):
        return contradicted

    asked = Asked(MACROS)
    fake, state, failure = _run(asked, judge)

    assert failure is None and len(asked.calls) == 1
    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"


def test_coverage_check_off_judges_once_after_play() -> None:
    judged: list[str] = []

    async def judge(system: str, user: str):
        judged.append(user)
        return VERDICT_WITH_CHECKS

    fake, state, failure = _run(Asked(MACROS), judge, coverage_check="off")

    assert len(judged) == 1
    assert not [f for f in fake.frames("LOG") if f["payload"].get("kind") == "coverage_check"]


# --- pe13: evaluator 가 근거를 잘리지 않은 채, 돈 행동의 코드와 함께 읽는다 ---------------------------------

from app.agents.evaluator.verdict import evidence_text, request  # noqa: E402
from app.agents.planner_executer.executer import StepPlay  # noqa: E402
from app.agents.planner_executer.macros import action_code  # noqa: E402

CHECKING = """def typing() -> bool:
    return scene() == "Story"


def finish_typing() -> None:
    \"\"\"Press while a line is typed and see it finish at once.\"\"\"
    wait_until(typing(), 3, "no line is being typed")
    press_key("Space", 0.05)
    wait_until(scene() == "Story", 0.5, "the line did not finish at once")


def other() -> None:
    \"\"\"Not run.\"\"\"
    press_key("A", 0.05)


def done() -> bool:
    return scene() == "Map"
"""


def test_an_action_s_code_comes_with_the_helpers_it_calls_and_nothing_else() -> None:
    code = action_code(CHECKING, ["finish_typing", "missing"])

    assert list(code) == ["finish_typing"]
    assert "def finish_typing" in code["finish_typing"] and "def typing" in code["finish_typing"]
    assert "def other" not in code["finish_typing"] and "def done" not in code["finish_typing"]


def test_the_evaluator_reads_the_code_of_the_written_actions_that_ran() -> None:
    play = StepPlay(
        "done",
        turns=[{"turn": 0, "picked": "finish_typing", "outcome": "passed", "sent": [{"ok": True}]}],
        action_sources=action_code(CHECKING, ["finish_typing", "other"]),
    )

    user = request(_scenario(), 1, play)

    assert "Code of the actions the plan wrote that ran above" in user
    assert 'wait_until(scene() == "Story", 0.5, "the line did not finish at once")' in user
    assert "def other" not in user


def test_every_evidence_name_and_every_change_reaches_the_evaluator() -> None:
    names = {f"Obj[{index}] text": f"line {index}" for index in range(150)}
    evidence = [
        {"at": "start", "values": {"(active scene)": "Battle", **names}},
        *({"at": f"turn {turn}", "values": {"(active scene)": "Battle", **names, "Counter": turn}} for turn in range(20)),
    ]
    play = StepPlay("done", evidence=evidence)

    text = evidence_text(play)

    assert "Obj[149] text" in text and "more names left out" not in text
    assert "turn 19: 19" in text and "changes …" not in text


# --- pe13: 준비 조건, 기다림, fact -------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from app.agents.planner_executer.developer_macros import DeveloperMacro, load_developer_macros  # noqa: E402
from app.agents.planner_executer.executer import Executer  # noqa: E402
from app.agents.planner_executer.prompts import developer_macro_list  # noqa: E402


class _Binder:
    def __init__(self, own=None) -> None:
        self.own = own

    async def ready(self, name, binding):
        return self.own


def test_a_contract_without_a_ready_of_its_own_takes_the_planner_s() -> None:
    executer = Executer(_menu().channel, _config().arch.planner, JevClient(post=FakeJev()))
    planner_ready, contract_ready = object(), object()

    async def go():
        bare = SimpleNamespace(binder=_Binder(), readies={"developer:advance": planner_ready})
        owned = SimpleNamespace(binder=_Binder(contract_ready), readies={"developer:advance": planner_ready})
        return (
            await executer._contract_ready(bare, "developer:advance", ()),
            await executer._contract_ready(owned, "developer:advance", ()),
            await executer._contract_ready(bare, "developer:advance", (("target", "Enemy[0]"),)),
        )

    bare, owned, bound = asyncio.run(go())

    assert bare is planner_ready and owned is contract_ready and bound is None


PRESS = '''def press_any_key() -> None:
    """Press Space once."""
    press_key("Space", 0.05)
'''
GUARDED = '''def open_menu() -> None:
    """Open the menu."""
    require(scene() == "Menu", "Go to the menu first.")
    press_key("M", 0.05)
'''


def test_the_planner_is_told_which_developer_macros_have_no_ready_condition() -> None:
    loaded = asyncio.run(load_developer_macros((
        DeveloperMacro("press_any_key", PRESS, "1.0", None, False, None),
        DeveloperMacro("open_menu", GUARDED, "1.0", None, False, None),
    )))

    text = developer_macro_list(loaded.usable.values())

    assert "write `ready_press_any_key()`" in text
    assert "ready_open_menu" not in text


def test_every_declared_fact_goes_to_jev_with_the_planner_s_choice_first(tmp_path) -> None:
    from tests.test_planner_executer_facts import _declare

    declared = _declare(tmp_path)
    executer = Executer(_menu().channel, _config().arch.planner, JevClient(post=FakeJev()), facts=declared)
    every = [fact.name for fact in declared.facts]

    chosen = executer._fact_sources_for(SimpleNamespace(facts=("player_hp",)))
    none_chosen = executer._fact_sources_for(SimpleNamespace(facts=()))

    assert [name for name, _scope, _member in none_chosen] == every and len(every) == 3
    assert [name for name, _scope, _member in chosen] == ["player_hp"] + [name for name in every if name != "player_hp"]


def test_an_undeclared_single_action_asks_whether_to_wait() -> None:
    from tests.test_planner_executer_pe11 import ONLY_PRESS
    from tests.test_planner_executer_steps import _play

    jev = FakeJev(picks=("none_fits", "press_start"))
    source = ONLY_PRESS.replace('\n\ndef ready_press_start() -> bool:\n    return scene() == "Menu"\n', "")

    play = _play(_menu(), jev, source)

    assert len(jev.asked("noul")) == 2 and not jev.asked("choice")
    assert [turn["picked"] for turn in play.turns if "picked" in turn] == ["none_fits", "press_start"]
    assert play.stopped == "done" and "sent" not in play.turns[0]
    assert play.turns[0]["options"] == ["press_start"] and play.turns[0]["picked_by"] == "jev"
    assert play.turns[0]["wait_asked_because"] == ["press_start"]


def test_an_action_with_several_targets_is_one_option_listing_them() -> None:
    from app.agents.planner_executer.executer import group_targets

    options = {
        "developer:place @ Card[1]": "Place a card.\n\nThis option acts on `Card[1]`.",
        "developer:place @ Card[2]": "Place a card.\n\nThis option acts on `Card[2]`.",
        "developer:end_turn": "End the turn.",
    }
    macros = SimpleNamespace(description=lambda name: {"developer:place": "Place a card."}[name])

    offered, targets_of = group_targets(options, macros, lambda target: ["Fire"] if target == "Card[1]" else [])

    assert list(offered) == ["developer:end_turn", "developer:place"]
    assert targets_of == {"developer:place": ["developer:place @ Card[1]", "developer:place @ Card[2]"]}
    assert offered["developer:place"].startswith("Place a card.")
    assert '`Card[1]` ("Fire"); `Card[2]`' in offered["developer:place"]


# --- pe14: prefer_ ---------------------------------------------------------------------------------

import pytest  # noqa: E402

from app.agents.planner_executer.macros import RegisteredMacros, parse_step  # noqa: E402
from app.agents.qa.macro.errors import MacroRejection  # noqa: E402
from tests.planner_executer_fakes import FakeGame  # noqa: E402

PREFERRING = MACROS + """

def prefer_strike(enemy: object) -> bool:
    return text(enemy) == "Weak"
"""


def _two_enemies() -> FakeGame:
    return FakeGame("Menu", {"Root/Start": {"text": "Start"}, "Root/A": {"text": "Weak"}, "Root/B": {"text": "Strong"}})


def test_the_executer_takes_the_target_the_planner_s_prefer_rule_holds_for() -> None:
    fake = _two_enemies()

    async def go():
        fake.push_reading()
        macros = await parse_step(PREFERRING, RegisteredMacros(fake.channel), contract_keys=("developer:strike",))
        executer = Executer(fake.channel, _config().arch.planner, JevClient(post=FakeJev()))
        record: dict = {}
        preferred = await executer._preferred(
            "developer:strike", ["developer:strike @ Root/A", "developer:strike @ Root/B"], {}, macros, 1, record
        )
        return preferred, record

    preferred, record = asyncio.run(go())

    assert preferred == ["developer:strike @ Root/A"]
    assert record["prefer"] == {"developer:strike @ Root/A": "met", "developer:strike @ Root/B": "unmet"}


@pytest.mark.parametrize(
    "source, refusal",
    [
        (MACROS + "\n\ndef prefer_nothing(x: object) -> bool:\n    return True\n", "matches no action"),
        (MACROS + "\n\ndef prefer_strike() -> bool:\n    return True\n", "with the action's target as its parameter"),
    ],
)
def test_a_prefer_rule_must_name_an_action_and_take_its_target(source: str, refusal: str) -> None:
    fake = _two_enemies()

    async def go():
        fake.push_reading()
        return await parse_step(source, RegisteredMacros(fake.channel), contract_keys=("developer:strike",))

    with pytest.raises(MacroRejection, match=refusal):
        asyncio.run(go())


# --- pe16: TC 사전 조건 --------------------------------------------------------------------------

from app.agents.planner_executer.runner import case_precondition  # noqa: E402


def test_a_precondition_belongs_to_the_first_step_of_its_case() -> None:
    scenario = QaScenario(
        title="Case of two steps",
        steps=[
            QaStep(action="Open.", case_id=5, case=QaCaseRef(id=5, precondition="The map shows no window.", expected="x")),
            QaStep(action="Close.", case_id=5, case=QaCaseRef(id=5, precondition="The map shows no window.", expected="x")),
            QaStep(action="Wait."),
        ],
    )

    assert [case_precondition(scenario, step) for step in (1, 2, 3)] == ["The map shows no window.", None, None]


@pytest.mark.parametrize("answer, status", [(0.9, "held"), (0.1, "unmet"), (0.45, "unknown")])
def test_the_precondition_is_read_against_the_whole_screen(answer: float, status: str) -> None:
    fake = _menu()
    jev = FakeJev(goal=(answer,))

    async def go():
        fake.push_reading()
        executer = Executer(fake.channel, _config().arch.planner, JevClient(post=jev))
        return await executer.precondition("The menu shows Start.")

    checked = asyncio.run(go())

    assert checked["status"] == status and checked["condition"] == "The menu shows Start."
    (asked,) = jev.asked("noul")
    assert "Does this hold in the game right now: The menu shows Start." in asked["questions"]["answer"]["instructions"]


def _with_precondition_scenario() -> QaScenario:
    return QaScenario(
        title="Start",
        steps=[QaStep(
            action="Press Start.", case_id=5,
            case=QaCaseRef(id=5, precondition="No window covers the menu.", expected="The map opens."),
        )],
    )


def test_an_unmet_precondition_has_the_planner_write_the_step_again_to_make_it_hold() -> None:
    fake = _menu()
    calls: list[str] = []
    judged: list[str] = []

    async def asked(system: str, user: str) -> str:
        calls.append(user)
        if len(calls) == 1:
            return f"```python\n{MACROS}```"
        # 다시 쓰기는 지금 계획(첫 판)에 대한 수정이다. 바꿀 것이 없다는 답도 받는다.
        return '```changes\n{"base": 1, "replace": {}, "remove": {}, "add": []}\n```'

    async def judge(system: str, user: str):
        judged.append(user)
        return {"passed": True, "reason": "ok", "state_detail": "", "game_insight": "", "checks": [], "precondition_held": None}

    config = _config(no_action_grace_seconds=0.2, analysis="off", coverage_check="off")

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=asked, judge=judge, jev=JevClient(post=FakeJev(goal=(0.1,))))
        return await runner.run_with_deadline(fake.channel, _with_precondition_scenario())

    state, failure = asyncio.run(go())

    assert failure is None and state.finished
    checked = [f["payload"]["decision"] for f in fake.frames("LOG") if f["payload"].get("kind") == "precondition_check"]
    assert len(checked) == 1 and checked[0]["status"] == "unmet"
    assert len(calls) >= 2 and "No window covers the menu." in calls[1]
    assert 'precondition, checked against the screen when this case started: "No window covers the menu."' in judged[0]
    assert "written again to make it hold" in judged[0]


def test_a_step_run_with_its_precondition_broken_is_recorded_apart_from_a_failure() -> None:
    async def judge(system: str, user: str):
        return {**VERDICT_WITH_CHECKS, "checks": [{**VERDICT_WITH_CHECKS["checks"][1]}], "precondition_held": False}

    verdict = asyncio.run(Evaluator(_config(), judge).judge(_scenario(), 1, PLAYED))

    assert verdict.passed is False and verdict.precondition_unmet
    assert verdict.message.startswith("Precondition not met: ") or verdict.message.startswith("사전 조건 불충족: ")
    assert verdict.details()["precondition"] == "unmet"
