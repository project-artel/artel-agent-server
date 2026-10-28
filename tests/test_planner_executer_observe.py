"""The case's `observe` names go to the SDK as one `watch`, and a case whose name the SDK could not
resolve fails as `INCONCLUSIVE` without a model call (ARTEL-1279)."""

import asyncio

import pytest

from app.agents.planner_executer import executer as executer_module
from app.agents.planner_executer.observe import observe_names, watch
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.qa.jev import JevClient
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import (
    MACROS,
    Asked,
    FakeJev,
    _config,
    _judge,
    _menu,
    _verdicts,
)


@pytest.fixture(autouse=True)
def _quick(monkeypatch) -> None:
    monkeypatch.setattr(executer_module, "SETTLE_QUIET_SECONDS", 0.001)
    monkeypatch.setattr(executer_module, "IDLE_LOOK_SECONDS", 0.01)


def _scenario(*observe: str) -> QaScenario:
    return QaScenario(
        title="Start",
        steps=[
            QaStep(
                action="Press Start.",
                case_id=5,
                case=QaCaseRef(
                    id=5, expected="The map opens.", observe=list(observe) or None
                ),
            )
        ],
    )


def _run(fake: FakeGame, scenario: QaScenario, judge, **knobs):
    config = _config(no_action_grace_seconds=0.2, **knobs)

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(
            config, ask=Asked(MACROS), judge=judge, jev=JevClient(post=FakeJev())
        )
        return await runner.run_with_deadline(fake.channel, scenario)

    return asyncio.run(go())


def test_observe_names_are_collected_once_each_in_step_order() -> None:
    scenario = QaScenario(
        steps=[
            QaStep(
                case_id=1,
                case=QaCaseRef(id=1, observe=["Game.Player::hp", "Progress::stage"]),
            ),
            QaStep(
                case_id=2,
                case=QaCaseRef(id=2, observe=["Progress::stage", "Shop::gold"]),
            ),
            QaStep(case_id=3, case=QaCaseRef(id=3)),
        ]
    )
    assert observe_names(scenario) == [
        "Game.Player::hp",
        "Progress::stage",
        "Shop::gold",
    ]


def test_no_observe_names_sends_no_watch() -> None:
    fake = _menu()
    assert asyncio.run(watch(fake.channel, _scenario())) == {}
    assert fake.actions == []


def test_the_scenarios_names_go_out_as_one_watch_and_the_unresolvable_ones_are_kept_by_case() -> (
    None
):
    fake = _menu()
    fake.answers["watch"] = lambda params: {
        "resolved": [{"name": "Game.Player::hp", "kind": "component", "holders": 1}],
        "unresolvable": [{"name": "Progress::stage", "reason": "not reachable"}],
        "walk": {"visited": 12, "ms": 0.8, "exhausted": False},
    }

    missing = asyncio.run(
        watch(fake.channel, _scenario("Game.Player::hp", "Progress::stage"))
    )

    assert fake.actions == [("watch", [["Game.Player::hp", "Progress::stage"]])]
    assert missing == {5: [{"name": "Progress::stage", "reason": "not reachable"}]}
    (log,) = [
        f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == "watch"
    ]
    assert log["message"] == "watch: 1/2 observe names resolved"


def test_an_sdk_without_watch_leaves_every_name_unresolvable() -> None:
    fake = _menu()
    fake.refuse.add(("watch", ("Game.Player::hp",)))

    missing = asyncio.run(watch(fake.channel, _scenario("Game.Player::hp")))

    assert list(missing) == [5] and missing[5][0]["name"] == "Game.Player::hp"
    assert missing[5][0]["reason"].startswith("Nothing at selector")


def test_a_failed_case_with_an_unresolvable_name_is_inconclusive_without_asking_the_model() -> (
    None
):
    fake = _menu()
    fake.answers["watch"] = lambda params: {
        "resolved": [],
        "unresolvable": [{"name": "Progress::stage", "reason": "no such type"}],
        "walk": {},
    }
    judge, seen = _judge(
        {"passed": False, "reason": "Stage did not change."},
        {"overview": "o", "patterns": [], "insights": []},
    )

    _run(fake, _scenario("Progress::stage"), judge)

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"
    assert verdict["analysis"]["category"] == "INCONCLUSIVE"
    assert verdict["analysis"]["cause"].startswith(
        "UNOBSERVABLE: the SDK could not find `Progress::stage` (no such type)"
    )
    assert verdict["analysis"]["reproduction"] == []
    # the verdict and the run review asked the judge; the failure analysis did not
    assert len(seen) == 2


def test_a_failed_case_whose_names_all_resolved_is_analysed_as_before() -> None:
    fake = _menu()
    fake.answers["watch"] = lambda params: {
        "resolved": [{"name": "Progress::stage"}],
        "unresolvable": [],
        "walk": {},
    }
    judge, seen = _judge(
        {"passed": False, "reason": "Stage did not change."},
        {
            "category": "INCONCLUSIVE",
            "cause": "c",
            "reproduction": [],
            "insight": "i",
            "actual": "a",
        },
        {"overview": "o", "patterns": [], "insights": []},
    )

    _run(fake, _scenario("Progress::stage"), judge)

    (verdict,) = _verdicts(fake)
    assert verdict["analysis"]["cause"] == "c"
    assert len(seen) == 3
