"""ORDERED와 FREE 모두 TC 시도를 판정하고 step은 참고 절차로 제공한다."""

import asyncio
import json

from app.agents.qa.arch import PhaseCycleMode, default_resolved_arch
from app.agents.qa.compaction import render_progress_ledger
from app.agents.qa.runner import _plan, _step_plan
from app.agents.qa.tools import QaRunState, build_tools
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType
from app.qa.schemas import QaCaseRef, QaRunScenario, QaScenario, QaStep

_NO_PHASE_GATE = default_resolved_arch().model_copy(
    update={"phase_cycle": PhaseCycleMode.off}
)


def ordered_scenario() -> QaScenario:
    return QaScenario(
        title="상점 구매",
        description="골드로 물약을 산다.",
        steps=[
            QaStep(action="타이틀에서 시작을 누른다", hint="Enter"),
            QaStep(
                action="상점에 들어간다",
                case_id=11,
                case=QaCaseRef(
                    id=11,
                    scene="Town",
                    precondition="골드 100 이상",
                    test_step="물약 구매",
                    expected="골드가 50 줄어든다",
                ),
            ),
            QaStep(
                action="물약을 산다",
                case_id=11,
                input="click Buy",
                case=QaCaseRef(
                    id=11,
                    scene="Town",
                    precondition="골드 100 이상",
                    test_step="물약 구매",
                    expected="골드가 50 줄어든다",
                ),
            ),
            QaStep(
                action="인벤토리를 연다", case_id=12, case=QaCaseRef(id=12, expected="물약이 보인다")
            ),
        ],
    )


def free_scenario() -> QaScenario:
    def case(case_id: int, scene: str, precondition: str | None, test_step: str, expected: str):
        return QaStep(
            action=test_step,
            case_id=case_id,
            case=QaCaseRef(
                id=case_id,
                scene=scene,
                precondition=precondition,
                test_step=test_step,
                expected=expected,
            ),
        )

    return QaScenario(
        title="전투 TC 묶음",
        description="전투 규칙을 확인한다.",
        execution_mode="FREE",
        steps=[
            case(21, "Battle", "적과 조우", "Fire 를 쓴다", "적 HP 가 줄어든다"),
            case(22, "Town", None, "상점을 연다", "상점 목록이 보인다"),
            case(23, "Battle", "MP 0", "Fire 를 쓴다", "MP 부족 안내가 뜬다"),
        ],
    )


def test_execution_mode_defaults_to_ordered() -> None:
    assert QaScenario().execution_mode == "ORDERED"
    assert QaScenario.model_validate({"steps": []}).execution_mode == "ORDERED"
    assert QaScenario.model_validate({"execution_mode": None}).execution_mode == "ORDERED"
    assert QaScenario.model_validate({"execution_mode": "FREE"}).execution_mode == "FREE"


def test_execution_mode_reaches_the_run_scenario() -> None:
    parsed = QaRunScenario.model_validate(
        {
            "qa_try_id": 1,
            "test_scenario_id": 2,
            "scenario": {"title": "t", "execution_mode": "FREE", "steps": []},
        }
    )
    assert parsed.scenario.execution_mode == "FREE"


def test_free_step_plan_makes_every_step_a_verdict() -> None:
    assert _step_plan(free_scenario()) == [(21, True), (22, True), (23, True)]


def test_both_modes_list_unique_cases_and_reference_routes():
    for scenario in (ordered_scenario(), free_scenario()):
        text = _plan(scenario)
        document, _ = json.JSONDecoder().raw_decode(text[text.index('{\n'):])
        assert len(document['test_cases']) == len({step.case_id for step in scenario.steps if step.case_id})
        assert len(document['reference_steps']) == len(scenario.steps)
        assert 'start_test_case' in text and 'INCONCLUSIVE' in text
        assert 'report_step(' not in text and 'report_case(' not in text


def test_both_modes_offer_the_same_tc_tools():
    from tests.qa_attempts import configure_attempts
    async def send(frame): pass
    for mode in ('FREE', 'ORDERED'):
        channel = QaRunChannel(qa_try_id=1, send=send)
        state = QaRunState(total_steps=3, step_meta=[(21, True), (22, True), (23, True)], execution_mode=mode)
        configure_attempts(channel, state)
        tools = {t.name: t for t in build_tools(channel, state, _NO_PHASE_GATE)}
        assert {'start_test_case', 'report_test_case', 'ask_user'} <= tools.keys()
        assert not {'report_step', 'report_case', 'wait_for_operator'} & tools.keys()


def test_free_attempts_arrive_out_of_order_and_retries_do_not_overwrite():
    from tests.qa_attempts import configure_attempts, start_case, report_case
    async def run():
        async def send(frame): pass
        channel = QaRunChannel(qa_try_id=1, send=send)
        state = QaRunState(total_steps=3, step_meta=[(21, True), (22, True), (23, True)], execution_mode='FREE')
        configure_attempts(channel, state)
        tools = {t.name: t for t in build_tools(channel, state, _NO_PHASE_GATE)}
        for case_id, passed in [(23, False), (21, True), (23, True)]:
            await start_case(tools, case_id)
            await report_case(tools, case_id, passed)
        assert [(x.case_id, x.verdict) for x in state.case_attempt_results] == [(23, 'FAILED'), (21, 'PASSED'), (23, 'PASSED')]
        assert len({x.attempt_id for x in state.case_attempt_results}) == 3
        assert '22' in render_progress_ledger(state, channel)
        before = len(channel.evaluation.requests)
        assert '없는 TC' in await start_case(tools, 99)
        assert len(channel.evaluation.requests) == before
    asyncio.run(run())
