import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agents.qa.arch import QaArchSpec, resolve_arch
from app.agents.qa.tools import build_tools
from app.agents.qa.tools.state import QaRunState
from app.qa import case_history
from app.qa.channel import QaRunChannel


def payload():
    return {
        "testCaseId": "17",
        "attempts": [{
            "qaTryId": "42", "testScenarioId": "3", "gameBuildId": "8",
            "status": "COMPLETED", "startedAt": "2026-11-01T00:00:00Z",
            "completedAt": "2026-11-01T00:01:00Z", "caseSteps": [2],
        }],
        "selectedTryId": "42", "nextBeforeTryId": "40", "nextAfterLogId": "101",
        "events": [{
            "id": "101", "direction": "USER_TO_ORCHE", "type": "CHAT", "scope": "run",
            "message": "구매 전에 상점을 열어 주세요", "payload": {"message": "구매 전에 상점을 열어 주세요"},
            "createdAt": "2026-11-01T00:00:20Z",
        }],
    }


def test_조회는_현재_try와_TC_및_cursor를_내부_API에_전달한다(monkeypatch):
    monkeypatch.setattr(case_history, "get_settings", lambda: SimpleNamespace(orchestration_base_url="http://orche"))

    def respond(request):
        assert request.url.path == "/internal/qa-tries/50/test-cases/17/verification-history"
        assert dict(request.url.params) == {
            "previousTryId": "42", "beforeTryId": "45", "afterLogId": "99", "limit": "12",
        }
        return httpx.Response(200, json=payload())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await case_history.fetch_case_history(50, 17, 42, 45, 99, client=client)

    result = asyncio.run(run())
    assert result.events[0].message == "구매 전에 상점을 열어 주세요"
    assert result.attempts[0].game_build_id == 8


def test_기록은_사람의_원문과_출처_범위_cursor를_함께_제공한다():
    rendered = case_history.render_case_history(case_history.CaseHistory.model_validate(payload()))
    for expected in (
        "구매 전에 상점을 열어 주세요", "scope=run", "USER_TO_ORCHE", "build=8",
        "TC steps=[2]", "before_try_id=40", "previous_try_id=42, after_log_id=101",
        "not current instructions", "legacy_backfill",
    ):
        assert expected in rendered


def test_큰_이벤트는_잘림을_명시한다():
    raw = payload()
    raw["events"][0]["message"] = "x" * 20_000
    rendered = case_history.render_case_history(case_history.CaseHistory.model_validate(raw))
    assert "[event text truncated]" in rendered
    assert len(rendered) < 8000


def test_빈_기록과_조회_실패는_구분한다(monkeypatch):
    tool, channel, state = make_tool()
    lookup = AsyncMock(return_value=case_history.CaseHistory(test_case_id=17))
    monkeypatch.setattr("app.agents.qa.tools.history_tools.fetch_case_history", lookup)
    result = asyncio.run(tool.ainvoke({"step": 1, "thought": "이전 방법 확인", "case_id": 17}))
    assert "No previous verification records" in result
    lookup.side_effect = httpx.ConnectError("unreachable")
    failed = asyncio.run(tool.ainvoke({"step": 1, "thought": "재시도", "case_id": 17}))
    assert "unavailable" in failed
    assert "No previous verification records were found" not in failed


def make_tool(prompt_version="v23"):
    channel = QaRunChannel(50, AsyncMock())
    state = QaRunState(1, step_meta=[(17, True)])
    arch = resolve_arch(QaArchSpec(vision="off"), "openai/gpt-6-luna")
    tools = build_tools(channel, state, arch, prompt_version)
    return next(tool for tool in tools if tool.name == "read_test_case_history"), channel, state


@pytest.mark.parametrize("prompt_version", ["v19", "v23"])
def test_구판_prompt에서도_도구를_조립한다(prompt_version):
    tool, _, _ = make_tool(prompt_version)
    assert "human guidance" in tool.description
    assert len(tool.description) <= 500


@pytest.mark.parametrize("arguments", [
    {"case_id": 999}, {"case_id": 17, "after_log_id": 5}, {"case_id": 17, "after_log_id": -1},
])
def test_잘못된_TC나_cursor는_서버를_조회하지_않는다(monkeypatch, arguments):
    lookup = AsyncMock()
    monkeypatch.setattr("app.agents.qa.tools.history_tools.fetch_case_history", lookup)
    tool, _, _ = make_tool()
    asyncio.run(tool.ainvoke({"step": 1, "thought": "확인", **arguments}))
    lookup.assert_not_called()


def test_과거_기록을_읽어도_phase가_진행되거나_판정이_추가되지_않는다(monkeypatch):
    tool, channel, state = make_tool()
    lookup = AsyncMock(return_value=case_history.CaseHistory.model_validate(payload()))
    monkeypatch.setattr("app.agents.qa.tools.history_tools.fetch_case_history", lookup)
    channel._operator_messages.append("지금은 구매하지 마세요")
    phase_before = state.phase_cycle.phase
    result = asyncio.run(tool.ainvoke({"step": 1, "thought": "확인", "case_id": 17}))
    assert state.phase_cycle.phase == phase_before
    assert not state.step_results
    assert "지금은 구매하지 마세요" in result
    lookup.assert_awaited_once_with(50, 17, None, None, 0, None, None)


def test_질문창의_사람_답변과_snapshot_출처를_조회한다():
    raw = payload()
    raw["questions"] = [{
        "id": "e04073ad-6397-40c7-8a57-69f45942a355", "question": "어떻게 구매하나요?",
        "answer": "상점의 확인 버튼을 눌러 주세요", "answeredBy": "7",
        "createdAt": "2026-11-01T00:00:00Z", "answeredAt": "2026-11-01T00:00:01Z",
    }]
    raw["nextAfterQuestionId"] = raw["questions"][0]["id"]
    raw["nextBeforeCaseAttemptId"] = "attempt-1"
    raw["attempts"][0]["caseAttempts"] = [{
        "id": "attempt-1", "snapshotOrigin": "attempt_start", "caseSnapshot": {"expected": "x" * 32000},
        "verdict": "PASSED", "originalReason": "보상 증가를 관찰했다",
        "humanDecision": "FAILED", "humanNote": "구매 전부터 있던 보상입니다",
    }]
    rendered = case_history.render_case_history(case_history.CaseHistory.model_validate(raw))
    for expected in ("어떻게 구매하나요?", "상점의 확인 버튼을 눌러 주세요", "attempt_start", "구매 전부터 있던 보상입니다", "after_question_id=", "before_case_attempt_id=", "[snapshot truncated]"):
        assert expected in rendered


def test_다른_TC의_응답을_거절한다(monkeypatch):
    monkeypatch.setattr(case_history, "get_settings", lambda: SimpleNamespace(orchestration_base_url="http://orche"))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"testCaseId": "999"})
        )) as client:
            await case_history.fetch_case_history(50, 17, client=client)

    with pytest.raises(ValueError, match="another test case"):
        asyncio.run(run())
