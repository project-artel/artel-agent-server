"""step 의 대상 계약(`target.py`): 대상의 뜻은 런 시작 때 고정되고, 다른 대상 위의 결과는 통과로 남지 않는다.

런 229 의 step 5 는 2스테이지의 클리어 화면으로 통과했다. 런 239 는 타이틀에서 진행도 값으로 고정해 step 3·6 의 대상을 가려내지
못했다. 불변식: 같은 화면·다른 대상은 통과 불가, 같은 대상·조건 충족은 통과, 대상을 정하지 못했거나 끝에 확인하지 못하면 근거
부족의 통과 아님, 확인할 값은 step 을 시작할 때 선언된 지금 값(`current`)에서만 고르고 진행도 값은 쓰지 않는다, 해석은 planner 의
계획을 읽지 않는다. 경계 사례는 `tests/fixtures/target_contract/cases.json` 에 있다.
"""

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest

from app.agents.evaluator.verdict import StepVerdict
from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.executer import Executer, StepPlay
from app.agents.planner_executer.identities import GameIdentity
from app.agents.planner_executer.macros import RegisteredMacros, parse_step
from app.agents.planner_executer.target import (
    DraftChange,
    DraftRelative,
    DraftStep,
    StepContracts,
    StepTarget,
    TargetDraft,
    change_holds,
    cross_check,
    evaluator_note,
    fix_intents,
    guard,
    observe,
    planner_note,
    target_state,
)
from app.qa.jev import JevClient
from app.qa.state_set import StateSnapshot
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import MACROS, Asked, FakeJev, _config, _judge, _menu, _verdicts
from tests.test_state_set import _quick  # noqa: F401 - the fixture makes play quick

CASES = json.loads((Path(__file__).parent / "fixtures" / "target_contract" / "cases.json").read_text())
DECLARED = tuple(
    GameIdentity(item["name"], item["kind"], item["base"], item["meaning"], item["changes_when"], item["identifies"], item["role"])
    for item in CASES["declarations"]
)
STAGE_ONE = StepTarget(
    4, "bound", "stage 1", (("StageData.stagePosition", 0),), (("MapMove.StagePosition", "plus_one"), ("StageData.stagePosition", "same")),
    kind="stage", index=0,
)


def _draft(*steps: DraftStep) -> TargetDraft:
    return TargetDraft(steps=list(steps))


def _passed() -> StepVerdict:
    return StepVerdict(True, "The clear screen appeared.", "(active scene): Map → GameClearScene")


@pytest.mark.parametrize("case", CASES["cases"], ids=[case["name"] for case in CASES["cases"]])
def test_the_boundary_cases_keep_their_answers(case: dict) -> None:
    intent = case["intent"]
    fixed = StepTarget(
        1, "fixed", intent["target"], changes=tuple(tuple(change) for change in intent.get("changes", [])), kind=intent["kind"], index=intent["index"]
    )

    target = observe(fixed, case["start"], DECLARED)

    expect = case["expect"]
    assert target.status == expect["status"], target.reason
    if "identity" in expect:
        assert [list(entry) for entry in target.identity] == expect["identity"]
    verdict = dataclasses.replace(_passed(), passed=case["evaluator_passed"])
    judged, check = guard(verdict, target, case["start"], case["last"])
    assert judged.passed is expect["passed"] and check.state == expect["state"]
    if not expect["passed"]:
        # 대상이 어긋나거나 확인되지 않은 통과 아님은 모두 근거 부족이다. 원인은 실패 분석이 가린다.
        assert not judged.evidence_sufficient and judged.message.startswith("TARGET-CHECK:")
    if "consistent" in expect:
        assert check.consistent is expect["consistent"]


def test_the_meaning_is_fixed_once_and_a_later_step_counts_from_the_one_it_names() -> None:
    kinds = frozenset({"stage"})
    draft = _draft(
        DraftStep(step=1, target=None),
        DraftStep(step=3, target="stage 1, the first node", kind="stage", index=0),
        DraftStep(step=4, target="stage 1", kind="Stage", relative_to=DraftRelative(step=3), changes=[DraftChange(name="MapMove.StagePosition", change="plus_one")]),
        DraftStep(step=5, target="stage 1, the one cleared in step 4", kind="stage", relative_to=DraftRelative(step=4)),
        DraftStep(step=6, target="the next node", kind="stage", relative_to=DraftRelative(step=4, offset=1)),
        DraftStep(step=7, target="stage 2, the one entered in step 6", kind="stage", relative_to=DraftRelative(step=6)),
    )

    contracts = fix_intents(draft, steps=7, version=1, kinds=kinds)

    assert [contracts.get(step).status for step in range(1, 8)] == ["none", "none", "fixed", "fixed", "fixed", "fixed", "fixed"]
    assert [contracts.get(step).index for step in range(3, 8)] == [0, 0, 0, 1, 1]
    # 뜻만 정한다. 확인할 값은 아직 없다.
    assert all(contracts.get(step).identity == () for step in range(1, 8))
    assert contracts.get(4).changes == (("MapMove.StagePosition", "plus_one"),)


def test_a_meaning_that_cannot_be_fixed_is_unknown() -> None:
    draft = _draft(
        DraftStep(step=1, target="stage 1", kind="stage level"),
        DraftStep(step=2, target="stage 1", kind="stage"),
        DraftStep(step=3, target="stage 2", kind="stage", relative_to=DraftRelative(step=4, offset=1)),
        DraftStep(step=4, target="stage 1", kind="stage", relative_to=DraftRelative(step=1)),
        DraftStep(step=5, target="stage 0", kind="stage", index=-1),
    )

    contracts = fix_intents(draft, steps=5, version=1, kinds=frozenset({"stage"}))

    assert [contracts.get(step).status for step in range(1, 6)] == ["unknown"] * 5
    assert "'stage level' is not one a declared value tells apart" in contracts.get(1).reason
    assert "no index" in contracts.get(2).reason and "not an earlier step" in contracts.get(3).reason
    assert "has no fixed stage" in contracts.get(4).reason and "below 0" in contracts.get(5).reason
    # 지금 것을 알려 주는 값이 선언되지 않았으면 대상이 있는 step 은 확인할 길이 없다.
    assert fix_intents(_draft(DraftStep(step=1, target="stage 1", kind="stage", index=0)), 1, 1, frozenset()).get(1).status == "unknown"


def test_the_observed_value_is_a_declared_current_one_and_never_a_progress_one() -> None:
    fixed = StepTarget(6, "fixed", "stage 2", changes=(("MapMove.StagePosition", "same"), ("Ghost.value", "same")), kind="stage", index=1)
    now = {"@persistent/GameObject[5] StageDataSingleton.stagePosition": 0, "static MapMove.StagePosition": 1}

    target = observe(fixed, now, DECLARED)

    assert target.status == "bound" and target.identity == (("StageDataSingleton.stagePosition", 1),)
    # 읽히지 않는 움직임은 빠진다.
    assert target.changes == (("MapMove.StagePosition", "same"),)
    # 선언의 시작 수를 더한다.
    counted_from_one = (dataclasses.replace(DECLARED[0], base=1),)
    assert observe(fixed, now, counted_from_one).identity == (("StageDataSingleton.stagePosition", 2),)
    # 진행도 값만 있으면 대상을 확인할 수 없다.
    only_progress = tuple(item for item in DECLARED if item.role == "progress")
    assert observe(fixed, now, only_progress).status == "unknown"
    # 여러 객체가 다른 값으로 가진 값은 끝에 확인할 수 없다.
    split = {"@persistent/A StageDataSingleton.stagePosition": 0, "@persistent/B StageDataSingleton.stagePosition": 1}
    assert "several objects" in observe(fixed, split, DECLARED).reason
    # 대상을 따지지 않거나 이미 정하지 못한 step 은 그대로다.
    assert observe(StepTarget(1, "none"), now, DECLARED) == StepTarget(1, "none")


def test_the_target_is_matched_against_the_values_and_the_moves_against_the_contract() -> None:
    assert target_state(STAGE_ONE, {"Stage[0] StageData.stagePosition": 0})[0] == "match"
    assert target_state(STAGE_ONE, {"Stage[0] StageData.stagePosition": 1})[0] == "mismatch"
    assert target_state(STAGE_ONE, {"Stage[0] StageData.stagePosition": "(not read)"})[0] == "unknown"
    assert target_state(StepTarget(1, "none"), {})[0] == "none"

    assert change_holds("plus_one", 0, 1) and change_holds("same", 0, 0) and not change_holds("increase", 2, 1)
    assert change_holds("plus_one", "a", "b") is None
    first = {"static MapMove.StagePosition": 0, "Stage[0] StageData.stagePosition": 0}
    assert cross_check(STAGE_ONE, first, {"static MapMove.StagePosition": 1, "Stage[0] StageData.stagePosition": 0})[0] is True
    assert cross_check(STAGE_ONE, first, {"static MapMove.StagePosition": 2, "Stage[0] StageData.stagePosition": 1})[0] is False


def test_values_that_moved_unlike_the_contract_do_not_pass_and_a_failure_stays_as_it_was() -> None:
    first = {"static MapMove.StagePosition": 0, "Stage[0] StageData.stagePosition": 0}

    verdict, check = guard(_passed(), STAGE_ONE, first, first)
    assert not verdict.passed and not verdict.evidence_sufficient and "moved unlike the contract" in verdict.message

    verdict, check = guard(_passed(), STAGE_ONE, first, {"static MapMove.StagePosition": 1, "Stage[0] StageData.stagePosition": 0})
    assert verdict.passed and check.state == "match" and check.consistent is True and check.overruled is None

    failed = dataclasses.replace(_passed(), passed=False, message="No clear screen.")
    verdict, check = guard(failed, STAGE_ONE, first, {"static MapMove.StagePosition": 2, "Stage[0] StageData.stagePosition": 1})
    assert verdict is failed and check.state == "mismatch" and check.overruled is None
    assert guard(_passed(), StepTarget(1, "none"), None, None) == (_passed(), None)
    unknown = StepTarget(4, "unknown", "stage 1", reason="no declared value tells the current stage apart")
    verdict, check = guard(_passed(), unknown, None, None)
    assert not verdict.passed and not verdict.evidence_sufficient and check.state == "unknown"


def test_the_planner_reads_the_target_and_the_next_goal_and_the_evaluator_reads_the_target() -> None:
    contracts = StepContracts(1, {4: STAGE_ONE})

    note = planner_note(contracts, 4, "Enter the clear screen")
    assert "stage 1 (told apart by StageData.stagePosition = 0)" in note and "you cannot change it" in note
    assert "The next step will ask: Enter the clear screen" in note
    assert planner_note(None, 4, "x") == ""
    assert "A result on another target is not this step's result." in evaluator_note(STAGE_ONE)
    assert evaluator_note(None) == "" and evaluator_note(StepTarget(1, "none")) == ""


class StageReader:
    """`STATE_SET` 자리. 지금 들어간 스테이지(객체 멤버)와 도달한 가장 먼 스테이지(static)를 낸다."""

    def __init__(self, fake: FakeGame, current: int, furthest: int) -> None:
        self.fake, self.current, self.furthest = fake, current, furthest

    async def __call__(self, qa_try_id: int, spec: dict) -> StateSnapshot:
        return StateSnapshot.model_validate({
            "stateVersion": 1, "sync": "SYNCED", "scene": self.fake.scene,
            "entities": [{"entityId": "e1", "scope": "persistent", "active": True, "status": "KNOWN", "selector": "Stage[0]",
                          "path": "Stage", "members": {"Game.StageData::stagePosition#0": {"status": "KNOWN", "value": self.current}}}],
            "statics": [{"memberId": "Map.MapMove::StagePosition", "observation": {"status": "KNOWN", "value": self.furthest}}],
        })


def _play_targeted(current: int, can_replan: bool) -> StepPlay:
    fake = _menu()
    target = dataclasses.replace(STAGE_ONE, step=1)

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(MACROS, RegisteredMacros(fake.channel))
        spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.2)
        executer = Executer(fake.channel, spec, JevClient(post=FakeJev()), state_reader=StageReader(fake, current, 0))
        return await executer.play(1, "Press Start.", macros, can_replan=can_replan, target=target)

    return asyncio.run(go())


def test_the_turn_where_done_holds_records_whether_it_holds_on_the_target() -> None:
    # 멈춤은 막지 않는다 — identity 가 틀린 해석이면 막을수록 play 가 목표를 지나친다. 판정은 `guard` 가 거른다.
    play = _play_targeted(current=1, can_replan=True)
    assert play.stopped == "done" and play.turns[-1]["target"] == "mismatch" and not play.replan_requested
    # 대상 값은 check 와 상관없이 근거 기록에 실린다.
    assert play.evidence[0]["values"]["Stage[0] StageData.stagePosition"] == 1

    play = _play_targeted(current=0, can_replan=False)
    assert play.stopped == "done" and play.turns[-1]["target"] == "match"


def test_a_run_fixes_the_meaning_once_and_does_not_pass_a_target_nobody_can_confirm() -> None:
    fake = _menu()
    judge, _ = _judge({"passed": True, "reason": "The map scene shows Stage 0.", "state_detail": "", "game_insight": ""})
    asked_resolver: list[str] = []

    async def resolve(system: str, user: str) -> TargetDraft:
        asked_resolver.append(user)
        return _draft(DraftStep(step=1, target="stage 1", kind="stage", index=0))

    from app.agents.planner_executer.runner import PlannerExecuterRunner
    from tests.test_planner_executer_steps import _scenario

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(
            _config(no_action_grace_seconds=0.2, target_contract="on"), ask=Asked(MACROS), judge=judge, jev=JevClient(post=FakeJev()),
            resolve=resolve,
        )
        return await runner.run_with_deadline(fake.channel, _scenario())

    state, failure = asyncio.run(go())

    # 런 시작에 한 번만 해석한다. 다시 해석하지 않는다.
    assert failure is None and len(asked_resolver) == 1
    # 해석은 시나리오와 시작 값만 읽는다. planner 의 답은 그 뒤에 온다.
    assert "def press_start" not in asked_resolver[0] and '"step": 1' in asked_resolver[0]
    # 선언이 없어 지금 스테이지를 확인할 값이 없다 — 모델이 통과라 해도 통과로 남지 않는다.
    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED" and verdict["message"].startswith("TARGET-CHECK: the step's target could not be fixed")
    assert verdict["verdict"]["target"]["status"] == "unknown"
    kinds = [frame["payload"].get("kind") for frame in fake.frames("LOG")]
    assert "step_contract" in kinds and "target_check" in kinds


def test_the_resolver_reads_the_declared_meanings_and_kinds() -> None:
    from app.agents.planner_executer.target import TargetResolver
    from tests.test_planner_executer_steps import _scenario

    asked: list[str] = []

    async def ask(system: str, user: str) -> TargetDraft:
        asked.append(user)
        return _draft(DraftStep(step=1, target="stage 1", kind="stage", index=0))

    contracts = asyncio.run(TargetResolver(_config(), ask, DECLARED).resolve(_scenario(), {"static MapMove.StagePosition": 0}))

    assert contracts.get(1).status == "fixed" and contracts.get(1).index == 0
    assert "tells the current one apart: map point, stage" in asked[0]
    assert "StageDataSingleton.stagePosition (current stage; ordinal, counting from 0)" in asked[0]
    assert "MapMove.StagePosition (progress stage; ordinal, counting from 0)" in asked[0]


def test_a_helper_only_the_ready_condition_calls_changes_the_contract_revision() -> None:
    from app.agents.planner_executer.contract import registered_contract
    from tests.test_planner_executer_guidance import CONTRACT, STRIKE

    source = STRIKE.replace('return member(enemy, "Combat.Enemy::Hp") > 0', "return can_hit(enemy)") + (
        "\ndef can_hit(enemy: object) -> bool:\n    return True\n"
    )
    before = registered_contract("strike", CONTRACT, source)
    after = registered_contract("strike", CONTRACT, source.replace("return True", "return False"))

    assert before.revision != after.revision
    assert registered_contract("strike", CONTRACT, source).revision == before.revision


def test_a_selector_two_live_objects_share_is_not_a_live_target() -> None:
    from types import SimpleNamespace

    from app.agents.planner_executer.executer import ContractInstance

    def executer(*held: tuple[str, int]) -> Executer:
        engine = object.__new__(Executer)
        pulse = SimpleNamespace(held={f"k{at}": SimpleNamespace(selector=selector, path=None, id=ident, live=True) for at, (selector, ident) in enumerate(held)})
        engine._channel = SimpleNamespace(scene=SimpleNamespace(pulse=pulse))
        return engine

    instance = ContractInstance("place", (("card", "Card(Clone)[24]"),), {"Card(Clone)[24]": 7}, "")

    assert executer(("Card(Clone)[24]", 7))._alive(instance)
    assert not executer(("Card(Clone)[24]", 7), ("Card(Clone)[24]", 9))._alive(instance)
    assert not executer(("Card(Clone)[24]", 9))._alive(instance)


def test_the_declarations_are_read_from_orchestration_and_a_failure_reads_none() -> None:
    import httpx

    from app.agents.planner_executer.identities import fetch_game_identities

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/projects/3/game-identities"
        return httpx.Response(200, json={"identities": [{
            "name": "StageData.stagePosition", "kind": "ordinal", "base": 0, "meaning": "now", "changesWhen": "on entry",
            "identifies": "stage", "role": "current", "gameBuildId": "4", "version": 2,
        }]})

    async def go(transport: httpx.MockTransport):
        async with httpx.AsyncClient(transport=transport) as client:
            return await fetch_game_identities(3, base_url="http://orch", client=client)

    (identity,) = asyncio.run(go(httpx.MockTransport(handler)))
    assert identity.name == "StageData.stagePosition" and identity.role == "current" and identity.identifies == "stage"
    assert identity.changes_when == "on entry"
    assert asyncio.run(go(httpx.MockTransport(lambda request: httpx.Response(503)))) == ()
