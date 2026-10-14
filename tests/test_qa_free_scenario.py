"""FREE 시나리오: 순서 없이 검증하는 TC 묶음 (ARTEL-1029).

FREE 는 스텝 하나가 TC 하나이고 agent 가 순서와 방법을 정한다. 여기서는 첫 메시지, 스텝별
`(case_id, is_verification)` 표, 순서 없이 온 `report_step` 의 답을 본다. ORDERED 가 읽는
첫 메시지는 이 기능 전과 한 글자도 달라지면 안 되므로 그 글을 그대로 고정한다.
"""

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


# ARTEL-1029 전 develop 의 `_plan` 이 위 `ordered_scenario` 에 낸 글 그대로다.
_ORDERED_PLAN_BEFORE_FREE = (
    "Scenario: 상점 구매 — 골드로 물약을 산다.\n\n"
    "Execute the steps below in order and call report_step(step, passed, message) for EVERY step.\n"
    "- A step with `verify` is the TEST CASE's verdict: `passed` = whether that expected result "
    "actually occurred. That verdict is the whole case's verdict.\n"
    "- Any other step: `passed` = whether you carried the action out (did it happen), not a "
    "quality judgment.\n"
    "- A `precondition` must hold before that case's first step. If you cannot reach it, report "
    "its steps passed=false with a message beginning 'SETUP-FAILED:' and do NOT report_issue "
    "(an unreachable precondition is not a defect).\n"
    "- `try:` and `via:` are advisory hints, not required.\n\n"
    "[\n"
    "  {\n"
    '    "step": 1,\n'
    '    "do": "타이틀에서 시작을 누른다  (try: Enter)"\n'
    "  },\n"
    "  {\n"
    '    "step": 2,\n'
    '    "do": "상점에 들어간다",\n'
    '    "case_id": 11,\n'
    '    "precondition": "골드 100 이상"\n'
    "  },\n"
    "  {\n"
    '    "step": 3,\n'
    '    "do": "물약을 산다  (via: click Buy)",\n'
    '    "case_id": 11,\n'
    '    "verify": "골드가 50 줄어든다"\n'
    "  },\n"
    "  {\n"
    '    "step": 4,\n'
    '    "do": "인벤토리를 연다",\n'
    '    "case_id": 12,\n'
    '    "verify": "물약이 보인다"\n'
    "  }\n"
    "]\n\n"
    "Begin. Observe the screen first."
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


def test_ordered_plan_is_unchanged() -> None:
    assert _plan(ordered_scenario()) == _ORDERED_PLAN_BEFORE_FREE
    assert _step_plan(ordered_scenario()) == [(None, False), (11, False), (11, True), (12, True)]


def test_free_step_plan_makes_every_step_a_verdict() -> None:
    assert _step_plan(free_scenario()) == [(21, True), (22, True), (23, True)]


def test_free_plan_lists_every_case_and_no_hints() -> None:
    scenario = free_scenario()
    scenario.steps[0].hint = "press F"
    scenario.steps[1].input = "click Shop"
    text = _plan(scenario)

    assert "You choose the order and the method" in text
    assert "exactly once for EVERY test case" in text
    assert "SETUP-FAILED:" in text
    assert "Execute the steps below in order" not in text
    assert "press F" not in text and "click Shop" not in text
    assert "try:" not in text and "via:" not in text

    items = json.loads(text[text.index("[") : text.rindex("]") + 1])
    assert items == [
        {
            "step": 1,
            "case_id": 21,
            "scene": "Battle",
            "precondition": "적과 조우",
            "test_step": "Fire 를 쓴다",
            "expected": "적 HP 가 줄어든다",
        },
        {
            "step": 2,
            "case_id": 22,
            "scene": "Town",
            "test_step": "상점을 연다",
            "expected": "상점 목록이 보인다",
        },
        {
            "step": 3,
            "case_id": 23,
            "scene": "Battle",
            "precondition": "MP 0",
            "test_step": "Fire 를 쓴다",
            "expected": "MP 부족 안내가 뜬다",
        },
    ]


def make_free():
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=0.05, write_timeout=0.05)
    scenario = free_scenario()
    state = QaRunState(
        total_steps=len(scenario.steps),
        step_meta=_step_plan(scenario),
        step_texts=[step.action for step in scenario.steps],
        execution_mode=scenario.execution_mode,
    )
    tools = {tool.name: tool for tool in build_tools(channel, state, arch=_NO_PHASE_GATE)}
    return channel, state, tools, sent


def test_free_reports_arrive_out_of_order() -> None:
    channel, state, tools, sent = make_free()

    async def report(step: int, passed: bool) -> str:
        return await tools["report_step"].ainvoke(
            {"step": step, "passed": passed, "message": f"TC {step}", "thought": "판정한다"}
        )

    async def run() -> None:
        third = await report(3, True)
        assert "2 test case(s) still without a verdict: 1, 2." in third
        assert "continue with step" not in third

        first = await report(1, False)
        assert "1 test case(s) still without a verdict: 2." in first
        assert "A failed test case is not a reason to stop." in first

        ledger = render_progress_ledger(state, channel)
        assert "Steps still without a verdict: 2" in ledger
        assert "Choose which of these to verify next." in ledger
        assert "Continue with step" not in ledger

        last = await report(2, True)
        assert "Every test case now has a verdict" in last
        assert "finish_run" in last

        closed = await tools["finish_run"].ainvoke(
            {"passed": False, "summary": "done", "thought": "닫는다"}
        )
        assert closed == "The run is closed."

    asyncio.run(run())

    assert state.finished
    assert state.finish_attempts == 1
    verdicts = [
        frame["payload"]
        for frame in sent
        if frame["type"] == MessageType.STATUS.value and frame["payload"].get("step")
    ]
    assert [(v["step"], v["case_id"], v["is_verification"]) for v in verdicts] == [
        (3, 23, True),
        (1, 21, True),
        (2, 22, True),
    ]
    cases = state.build_summary()["cases"]
    assert [(unit["case_id"], unit["passed"]) for unit in cases["items"]] == [
        (21, False),
        (22, True),
        (23, True),
    ]


def test_free_re_report_does_not_count_twice() -> None:
    _, state, tools, _ = make_free()

    async def run() -> str:
        await tools["report_step"].ainvoke(
            {"step": 1, "passed": False, "message": "x", "thought": "판정한다"}
        )
        return await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "y", "thought": "다시 판정한다"}
        )

    again = asyncio.run(run())
    assert "2 test case(s) still without a verdict: 2, 3." in again
    assert state.unreported_steps() == [2, 3]
