"""TC 판정과 참고 step을 분리하고 반복 검증의 원본을 보존한다."""

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.agents.qa.arch import default_resolved_arch, PhaseCycleMode
from app.agents.qa.runner import _plan
from app.agents.qa.tools.reporting_tools import build_reporting_tools
from app.agents.qa.tools.state import QaRunState
from app.agents.qa.tools.tool_context import ToolContext
from app.qa.channel import QaRunChannel
from app.qa.evaluation import ActiveRunClock, CaseAttempt, run_with_active_deadline
from app.qa.schemas import QaScenario, QaStep, QaCaseRef


def setup_tools():
    channel = QaRunChannel(qa_try_id=1, send=AsyncMock())
    state = QaRunState(total_steps=3, step_meta=[(42, False), (42, True), (None, False)])
    arch = default_resolved_arch().model_copy(update={"phase_cycle": PhaseCycleMode.off, "verdict_evidence": "off"})
    return channel, state, {tool.name: tool for tool in build_reporting_tools(ToolContext(channel, state, arch))}


def test_step_reporting_is_not_exposed_and_steps_do_not_block_finish():
    async def run():
        channel, state, tools = setup_tools()
        assert "report_step" not in tools
        assert "report_case" not in tools
        await tools["finish_run"].ainvoke({"passed": False, "summary": "검증하지 못함", "thought": "사용자 종료"})
        assert state.finished
        summary = state.build_summary()
        assert summary["steps"]["failed"] == 0
        assert summary["cases"]["failed"] == 0
        assert summary["cases"]["untested"] == 1
    asyncio.run(run())


def test_repeated_case_attempts_remain_separate_and_inconclusive_is_not_failure():
    async def run():
        channel, state, tools = setup_tools()
        async def start(case_id):
            attempt = CaseAttempt(id=uuid4())
            channel.evaluation.attempts[case_id] = attempt.id
            return attempt
        async def report(case_id, *args):
            channel.evaluation.attempts.pop(case_id)
        channel.evaluation.start = start
        channel.evaluation.report = report
        for verdict in ("FAILED", "INCONCLUSIVE"):
            await tools["start_test_case"].ainvoke({"case_id": 42, "thought": "검증"})
            await tools["report_test_case"].ainvoke({"case_id": 42, "verdict": verdict, "message": "관측 결과", "thought": "판정"})
        assert len(state.case_attempt_results) == 2
        assert len({result.attempt_id for result in state.case_attempt_results}) == 2
        summary = state.build_summary()
        assert summary["cases"]["failed"] == 0
        assert summary["cases"]["inconclusive"] == 1
        assert len(summary["attempts"]) == 2
    asyncio.run(run())


def test_plan_treats_steps_as_reference_and_reports_cases():
    scenario = QaScenario(title="검증", description="", steps=[QaStep(action="시작 클릭", case_id=42,
        case=QaCaseRef(id=42, expected="게임 시작"))])
    plan = _plan(scenario)
    assert "reference_steps" in plan
    assert "report_test_case" in plan
    assert "report_step(" not in plan
    assert "EVERY step" not in plan


def test_waiting_time_does_not_consume_active_deadline():
    with patch("app.qa.evaluation.monotonic") as now:
        now.return_value = 10
        clock = ActiveRunClock()
        now.return_value = 12
        with clock.waiting():
            now.return_value = 1000
            assert clock.elapsed == 2
        now.return_value = 1003
        assert clock.elapsed == 5


def test_active_deadline_cancels_the_run_task():
    async def run():
        cleaned = asyncio.Event()
        async def pending():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        clock = ActiveRunClock()
        try:
            await run_with_active_deadline(pending(), clock, 0.01)
        except TimeoutError:
            pass
        assert cleaned.is_set()
    asyncio.run(run())


def test_active_deadline_does_not_charge_channel_setup_time():
    async def run():
        clock = ActiveRunClock()
        clock.started -= 100

        async def work():
            await asyncio.sleep(0)
            return "완료"

        assert await run_with_active_deadline(work(), clock, 1) == "완료"

    asyncio.run(run())


def test_question_wait_blocks_other_tools_until_answered():
    from langchain_core.tools import tool
    from app.agents.qa.tools import _serialized
    async def run():
        waiting, answered = asyncio.Event(), asyncio.Event()
        acted = []
        @tool(description="사용자의 답변을 기다린다.")
        async def question() -> str:
            waiting.set()
            await answered.wait()
            return "답변"
        @tool(description="게임을 조작한다.")
        async def action() -> str:
            acted.append(True)
            return "완료"
        lock = asyncio.Lock()
        asking = asyncio.create_task(_serialized(question, lock).ainvoke({}))
        await waiting.wait()
        moving = asyncio.create_task(_serialized(action, lock).ainvoke({}))
        await asyncio.sleep(0)
        assert not acted
        answered.set()
        await asyncio.gather(asking, moving)
        assert acted == [True]
    asyncio.run(run())


def test_report_retry_keeps_the_same_payload_after_a_lost_response():
    import httpx
    from app.qa.evaluation import QaEvaluationClient
    async def run():
        evaluation = QaEvaluationClient(1)
        evaluation.url = lambda path: "http://orch/" + path
        request = httpx.Request("POST", "http://orch/report")
        client = AsyncMock()
        client.post.side_effect = [httpx.ReadTimeout("response lost", request=request),
                                  httpx.Response(503, request=request), httpx.Response(200, request=request)]
        payload = {"verdict": "FAILED", "reason": "화면 확인"}
        with patch("app.qa.evaluation.asyncio.sleep", new_callable=AsyncMock):
            await evaluation.post(client, "case-attempts/stable-id/report", payload)
        assert client.post.await_count == 3
        assert all(call.kwargs["json"] == payload for call in client.post.await_args_list)
        assert len({call.args[0] for call in client.post.await_args_list}) == 1
    asyncio.run(run())


def test_macro_drafts_only_include_actions_from_the_current_attempt():
    from app.agents.qa.macro.lift import DispatchRecord
    async def run():
        channel, state, tools = setup_tools()
        async def start(case_id):
            attempt = CaseAttempt(id=uuid4())
            channel.evaluation.attempts[case_id] = attempt.id
            return attempt
        async def report(case_id, *args):
            channel.evaluation.attempts.pop(case_id)
        channel.evaluation.start, channel.evaluation.report = start, report
        for key in ("Space", "Return"):
            await tools["start_test_case"].ainvoke({"case_id": 42, "thought": "검증"})
            state.dispatches.extend(DispatchRecord(tool="press_key", step=1, scene="Story",
                arguments={"key_code": key, "duration_seconds": 0.1}) for _ in range(3))
            await tools["report_test_case"].ainvoke({"case_id": 42, "verdict": "PASSED", "message": "확인", "thought": "판정"})
        drafts = list(state.macros.drafts.values())
        assert len(drafts) == 2
        assert "Space" in str(drafts[0]) and "Return" not in str(drafts[0])
        assert "Return" in str(drafts[1]) and "Space" not in str(drafts[1])
    asyncio.run(run())


def test_tool_retry_preserves_the_first_report_evidence():
    import httpx
    async def run():
        channel, state, tools = setup_tools()
        channel.evaluation.attempts[42] = uuid4()
        report = AsyncMock(side_effect=[httpx.ReadTimeout("lost"), None])
        channel.evaluation.report = report
        arguments = {"case_id": 42, "verdict": "FAILED", "message": "최초 관측", "thought": "판정"}
        await tools["report_test_case"].ainvoke(arguments)
        assert not state.case_attempt_results
        await tools["report_test_case"].ainvoke({**arguments, "message": "다른 문장"})
        assert report.await_args_list[0] == report.await_args_list[1]
        assert state.case_attempt_results[0].message == "최초 관측"
    asyncio.run(run())
