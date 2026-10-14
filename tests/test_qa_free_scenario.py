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
    assert "report_case(case_id, passed, message) once for EVERY test case" in text
    assert "Not having reached a case yet is NOT a verdict" in text
    assert "There is no time or token budget for you to save" in text
    assert "SETUP-FAILED:" in text
    assert "report_step" not in text
    assert "Execute the steps below in order" not in text
    assert "press F" not in text and "click Shop" not in text
    assert "try:" not in text and "via:" not in text

    items = json.loads(text[text.index("[") : text.rindex("]") + 1])
    assert items == [
        {
            "case_id": 21,
            "scene": "Battle",
            "precondition": "적과 조우",
            "test_step": "Fire 를 쓴다",
            "expected": "적 HP 가 줄어든다",
        },
        {
            "case_id": 22,
            "scene": "Town",
            "test_step": "상점을 연다",
            "expected": "상점 목록이 보인다",
        },
        {
            "case_id": 23,
            "scene": "Battle",
            "precondition": "MP 0",
            "test_step": "Fire 를 쓴다",
            "expected": "MP 부족 안내가 뜬다",
        },
    ]


class FakeJudge:
    """미도달 확률을 정해 둔 판정기. 받은 message 를 남긴다."""

    def __init__(self, probability: float | None) -> None:
        self.probability = probability
        self.asked: list[tuple[str, str, str]] = []

    async def unreached_probability(self, test_step: str, expected: str, message: str):
        self.asked.append((test_step, expected, message))
        return self.probability


def make_free(judge: FakeJudge | None = None):
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
    state.step_expected = [step.case.expected for step in scenario.steps]
    state.unreached_judge = judge
    tools = {tool.name: tool for tool in build_tools(channel, state, arch=_NO_PHASE_GATE)}
    return channel, state, tools, sent


def report(tools, case_id: int, passed: bool, message: str = "", overwrite: bool = False):
    args = {
        "case_id": case_id,
        "passed": passed,
        "message": message or f"TC {case_id}",
        "thought": "판정한다",
    }
    if overwrite:
        args["overwrite"] = True
    return tools["report_case"].ainvoke(args)


def test_free_run_has_report_case_instead_of_report_step() -> None:
    _, _, tools, _ = make_free()
    assert "report_case" in tools
    assert "report_step" not in tools
    assert "overwrite" in tools["report_case"].args
    assert "case_id" in tools["report_case"].args


def test_ordered_run_keeps_report_step() -> None:
    channel = QaRunChannel(qa_try_id=7, send=lambda frame: None, action_timeout=0.05, write_timeout=0.05)
    state = QaRunState(total_steps=1, step_meta=[(None, False)])
    names = {tool.name for tool in build_tools(channel, state, arch=_NO_PHASE_GATE)}
    assert "report_step" in names and "report_case" not in names


def test_free_reports_arrive_out_of_order() -> None:
    channel, state, tools, sent = make_free()

    async def run() -> None:
        third = await report(tools, 23, True)
        assert "2 test case(s) still without a verdict (case_id): 21, 22." in third
        assert "continue with step" not in third

        first = await report(tools, 21, False)
        assert "1 test case(s) still without a verdict (case_id): 22." in first
        assert "A failed test case is not a reason to stop." in first
        assert "overwrite=true" in first

        ledger = render_progress_ledger(state, channel)
        assert "Test cases still without a verdict (case_id): 22" in ledger
        assert "case 21: FAIL" in ledger
        assert "not a sign that time is running out" in ledger
        assert "Continue with step" not in ledger

        last = await report(tools, 22, True)
        assert "Every test case now has a verdict" in last

        closed = await tools["finish_run"].ainvoke(
            {"passed": False, "summary": "done", "thought": "닫는다"}
        )
        assert closed == "The run is closed."

    asyncio.run(run())

    assert state.finished
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


def test_unknown_case_id_records_nothing() -> None:
    _, state, tools, sent = make_free()
    answer = asyncio.run(report(tools, 99, True))
    assert "case_id 99 is not in this scenario" in answer
    assert "21, 22, 23" in answer
    assert state.step_results == [] and sent == []


def test_overwrite_takes_two_calls_and_happens_once() -> None:
    _, state, tools, sent = make_free()

    async def run() -> tuple[str, str, str, str]:
        await report(tools, 21, False, "아직 전투 중")
        warned = await report(tools, 21, True, "적 HP 가 10 에서 4 로 줄었다")
        overwrote = await report(tools, 21, True, "적 HP 가 10 에서 4 로 줄었다", overwrite=True)
        warned_again = await report(tools, 21, False, "다시 실패", overwrite=False)
        refused = await report(tools, 21, False, "다시 실패", overwrite=True)
        return warned, overwrote, warned_again, refused

    warned, overwrote, warned_again, refused = asyncio.run(run())

    assert warned.startswith("WARNING: nothing was recorded.")
    assert "failed — 아직 전투 중" in warned
    assert "overwrite=true" in warned
    assert overwrote.startswith("Overwrote case 21: failed → passed.")
    assert "overwritten only once" in refused and "(passed) stands" in refused
    assert "overwritten only once" in warned_again

    # 기록은 하나로 바뀌어 있고, 요약은 같은 TC 를 두 번 세지 않는다.
    assert [(r.step, r.passed) for r in state.step_results] == [(1, True)]
    summary = state.build_summary()
    assert summary["steps"]["passed"] == 1 and summary["steps"]["failed"] == 2
    verdict_frames = [f for f in sent if f["type"] == MessageType.STATUS.value]
    assert [f["payload"]["status"] for f in verdict_frames] == ["FAILED", "COMPLETED"]


def test_unreached_failure_is_sent_back_then_recorded_after_the_cap() -> None:
    judge = FakeJudge(0.95)
    _, state, tools, sent = make_free(judge)

    async def run() -> list[str]:
        return [await report(tools, 22, False, "아직 상점에 가지 않았다") for _ in range(3)]

    first, second, third = asyncio.run(run())

    assert first.startswith("Nothing was recorded — this reads as case 22 not reached")
    assert second.startswith("Nothing was recorded")
    assert third.startswith("Recorded.")
    assert judge.asked[0] == ("상점을 연다", "상점 목록이 보인다", "아직 상점에 가지 않았다")
    assert len(judge.asked) == 2
    assert [(r.step, r.passed) for r in state.step_results] == [(2, False)]


def test_attempted_failure_and_passes_are_recorded_without_refusal() -> None:
    judge = FakeJudge(0.1)
    _, state, tools, _ = make_free(judge)

    async def run() -> tuple[str, str]:
        failed = await report(tools, 21, False, "Fire 를 썼지만 적 HP 가 그대로다")
        passed = await report(tools, 22, True, "상점 목록이 보였다")
        return failed, passed

    failed, passed = asyncio.run(run())
    assert failed.startswith("Recorded.") and passed.startswith("Recorded.")
    # 통과 판정은 묻지 않는다.
    assert len(judge.asked) == 1


def test_judge_failure_records_the_verdict() -> None:
    _, state, tools, _ = make_free(FakeJudge(None))
    answer = asyncio.run(report(tools, 21, False, "아직 전투 중"))
    assert answer.startswith("Recorded.")
    assert len(state.step_results) == 1


def test_unreached_judge_asks_jev_and_reads_the_attempted_probability() -> None:
    from app.qa.reach import UnreachedJudge

    bodies: list[dict] = []

    async def decide(body: dict) -> dict:
        bodies.append(body)
        return {"answers": {"verdict": {"noul": 0.25}}}

    probability = asyncio.run(
        UnreachedJudge(decide).unreached_probability("웨이브를 격파한다", "클리어 화면", "전투 진행 중")
    )
    assert probability == 0.75
    # Jev 결정 API 는 `state` 가 없으면 400 이다.
    assert bodies[0]["state"] == {
        "test_step": "웨이브를 격파한다",
        "expected": "클리어 화면",
        "tester_report": "전투 진행 중",
    }
    assert bodies[0]["questions"]["verdict"]["type"] == "noul"


def test_unreached_judge_fails_open() -> None:
    from app.qa.reach import UnreachedJudge

    async def decide(body: dict) -> dict:
        raise RuntimeError("402")

    assert asyncio.run(UnreachedJudge(decide).unreached_probability("a", "b", "c")) is None


def test_ordered_phase_refusal_does_not_name_report_case() -> None:
    from app.agents.qa.arch import PhaseCycleMode as Mode

    arch = default_resolved_arch().model_copy(update={"phase_cycle": Mode.lite})
    channel = QaRunChannel(qa_try_id=7, send=lambda frame: None, action_timeout=0.05, write_timeout=0.05)
    state = QaRunState(total_steps=1, step_meta=[(None, False)])
    build_tools(channel, state, arch=arch)
    refusal = state.phase_cycle.refusal_for("record_knowledge")
    assert "`report_step`" in refusal and "report_case" not in refusal


def test_free_run_config_records_report_case_and_its_own_fingerprint() -> None:
    from app.qa.run_config import resolve_run_config

    ordered = resolve_run_config()
    free = resolve_run_config(execution_mode="FREE")
    assert "report_step" in ordered.tools and "report_case" not in ordered.tools
    assert "report_case" in free.tools and "report_step" not in free.tools
    assert free.agent_fingerprint != ordered.agent_fingerprint
    assert free.agent_arch == ordered.agent_arch


def test_learned_refusal_names_report_case_in_a_free_run() -> None:
    from app.agents.qa.arch import PhaseCycleMode as Mode

    arch = default_resolved_arch().model_copy(update={"phase_cycle": Mode.remember_in_verdict})
    channel = QaRunChannel(qa_try_id=7, send=lambda frame: None, action_timeout=0.05, write_timeout=0.05)
    scenario = free_scenario()
    state = QaRunState(
        total_steps=len(scenario.steps),
        step_meta=_step_plan(scenario),
        step_texts=[step.action for step in scenario.steps],
        execution_mode="FREE",
    )
    tools = {tool.name: tool for tool in build_tools(channel, state, arch=arch)}
    answer = asyncio.run(
        tools["report_case"].ainvoke(
            {"case_id": 21, "passed": True, "message": "줄었다", "thought": "판정한다"}
        )
    )
    assert "Call `report_case` again" in answer
    assert state.step_results == []
