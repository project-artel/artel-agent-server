"""행동 완료 대신 기대값과 관측값으로 TC 판정을 대조한다 (ARTEL-1220/1270).

참고 step 보고는 제거됐으므로 기존 performed/passed 회귀를 TC checks 계약에서 검증한다.
"""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.agents.qa.arch import default_resolved_arch, PhaseCycleMode
from app.agents.qa.tools.reporting_tools import build_reporting_tools
from app.agents.qa.tools.state import QaRunState
from app.agents.qa.tools.tool_context import ToolContext
from app.qa.channel import QaRunChannel


def make(execution_mode):
    channel = QaRunChannel(qa_try_id=7, send=AsyncMock())
    state = QaRunState(total_steps=2, step_meta=[(31, True), (32, True)], execution_mode=execution_mode)
    arch = default_resolved_arch().model_copy(update={"phase_cycle": PhaseCycleMode.off, "verdict_evidence": "on"})
    channel.evaluation.attempts[31] = uuid4()
    channel.evaluation.report = AsyncMock()
    tools = {one.name: one for one in build_reporting_tools(ToolContext(channel, state, arch))}
    return channel, state, tools


@pytest.mark.parametrize("mode", ["ORDERED", "FREE"])
def test_a_refused_action_cannot_be_passed_when_the_expected_result_did_not_occur(mode):
    async def run():
        channel, state, tools = make(mode)
        answer = await tools["report_test_case"].ainvoke({"case_id": 31, "verdict": "PASSED",
            "message": "expected: 발동 버튼이 나타난다\nobserved: 버튼이 숨겨져 누를 수 없음", "thought": "대조",
            "checks": [{"expected": "발동 버튼 표시", "observed": "버튼 숨김", "met": "no"}]})
        assert "판정을 기록하지 않았습니다" in answer
        channel.evaluation.report.assert_not_awaited()
        assert state.case_attempt_results == []
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["ORDERED", "FREE"])
def test_the_observed_mismatch_is_recorded_as_a_failed_tc(mode):
    async def run():
        channel, state, tools = make(mode)
        await tools["report_test_case"].ainvoke({"case_id": 31, "verdict": "FAILED", "message": "버튼이 표시되지 않음",
            "thought": "대조", "checks": [{"expected": "발동 버튼 표시", "observed": "버튼 숨김", "met": "no"}]})
        channel.evaluation.report.assert_awaited_once()
        assert [one.verdict for one in state.case_attempt_results] == ["FAILED"]
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["ORDERED", "FREE"])
def test_refusal_itself_can_be_the_expected_result_without_an_action_completion_verdict(mode):
    async def run():
        channel, state, tools = make(mode)
        await tools["report_test_case"].ainvoke({"case_id": 31, "verdict": "PASSED", "message": "조건이 부족해 버튼이 숨겨짐",
            "thought": "대조", "checks": [{"expected": "조건 부족 시 버튼 숨김", "observed": "버튼 숨김", "met": "yes"}]})
        channel.evaluation.report.assert_awaited_once()
        assert [one.verdict for one in state.case_attempt_results] == ["PASSED"]
        assert "report_step" not in tools and "report_case" not in tools
    asyncio.run(run())
