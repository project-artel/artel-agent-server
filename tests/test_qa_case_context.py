"""TC 원문과 진행 상태가 조회·재검증·반복 압축 뒤에도 남는지 확인한다."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.qa.case_context import (
    TC_LEDGER_ID, QaCaseContextMiddleware, detail_block, fold_case_details, render_case_ledger,
)
from app.agents.qa.runner import _plan
from app.qa.evaluation import CaseAttempt
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from test_qa_case_evaluation import setup_tools
from test_qa_compaction import FakeSummarizer, build_middleware
from test_qa_run_compacted import ScriptedModel


def setup():
    channel, state, tools = setup_tools()
    state.step_meta = [(17, True), (18, True), (17, False), (22, True), (99, True)]
    channel.evaluation.snapshots = {case_id: {
        "id": case_id, "scene": "Battle", "test_step": f"TC {case_id} title\nDo the action",
        "precondition": f"original precondition {case_id}", "expected": f"original expected {case_id}",
    } for case_id in (17, 18, 22)}
    state.case_reviews[17] = [{"note": "Wait for animation", "person_decided": "passed"}]

    async def start(case_id):
        attempt = CaseAttempt(id=uuid4())
        channel.evaluation.attempts[case_id] = attempt.id
        return attempt

    async def report(case_id, *args):
        channel.evaluation.attempts.pop(case_id)

    channel.evaluation.start = AsyncMock(side_effect=start)
    channel.evaluation.report = AsyncMock(side_effect=report)
    return channel, state, tools


def ledger_document(text):
    return json.JSONDecoder().raw_decode(text[text.index("{"):])[0]


def test_lookup_reads_snapshot_without_starting_attempt_and_rejects_unknown_id():
    async def run():
        channel, state, tools = setup()
        answer = await tools["get_test_case"].ainvoke({"case_id": 17, "thought": "read original"})
        assert "original precondition 17" in answer and "original expected 17" in answer
        assert "Wait for animation" in answer and "not the answer" in answer
        assert state.current_case_id is None
        channel.evaluation.start.assert_not_called()
        assert "not part of this scenario" in await tools["get_test_case"].ainvoke({"case_id": 123, "thought": "unknown"})
        assert "did not receive" in await tools["get_test_case"].ainvoke({"case_id": 99, "thought": "missing snapshot"})
    asyncio.run(run())


def test_current_and_unreported_ids_follow_successful_start_report_and_reverification():
    async def run():
        channel, state, tools = setup()
        answer = await tools["start_test_case"].ainvoke({"case_id": 17, "thought": "verify"})
        assert "original expected 17" in answer
        first = ledger_document(render_case_ledger(state, channel))
        assert first["all_case_ids"] == [17, 18, 22, 99]
        assert first["current_case_id"] == 17
        assert first["without_report_ids"] == [17, 18, 22, 99]
        await tools["report_test_case"].ainvoke({"case_id": 17, "verdict": "INCONCLUSIVE", "message": "blocked", "thought": "report"})
        second = ledger_document(render_case_ledger(state, channel))
        assert second["reported_ids"] == second["inconclusive_ids"] == [17]
        assert second["current_case_id"] is None
        assert second["without_report_ids"] == [18, 22, 99]
        await tools["start_test_case"].ainvoke({"case_id": 17, "thought": "retry"})
        third = ledger_document(render_case_ledger(state, channel))
        assert third["reported_ids"] == [17] and third["current_case_id"] == 17
        assert third["open_attempt_ids"] == [17]
        channel.evaluation.report.side_effect = httpx.ConnectError("unavailable")
        await tools["report_test_case"].ainvoke({"case_id": 17, "verdict": "PASSED", "message": "pass", "thought": "report"})
        assert ledger_document(render_case_ledger(state, channel))["current_case_id"] == 17
        channel.evaluation.start.side_effect = httpx.ConnectError("unavailable")
        await tools["start_test_case"].ainvoke({"case_id": 18, "thought": "next"})
        assert state.current_case_id == 17
    asyncio.run(run())


def test_large_specs_keep_all_ids_and_omit_duplicate_routes_but_small_specs_stay_inline():
    def scenario(size):
        return QaScenario(steps=[QaStep(case_id=case_id, action="step " * size,
            case=QaCaseRef(id=case_id, precondition="precondition " * size,
                           test_step="Short title\n" + "action " * size, expected="expected " * size))
            for case_id in (17, 18, 22)])
    small = _plan(scenario(1))
    assert "precondition" in ledger_document(small)["test_cases"][0]
    large = _plan(scenario(2500))
    rows = ledger_document(large)
    assert [row["case_id"] for row in rows["test_cases"]] == [17, 18, 22]
    assert all("expected" not in row and "precondition" not in row for row in rows["test_cases"])
    assert rows["reference_steps"] == []
    assert len(large) < 3000


def test_old_details_fold_but_current_and_last_lookup_remain_and_source_is_unchanged():
    messages = [ToolMessage(id=str(case_id), tool_call_id=str(case_id), content=detail_block(case_id, f"original {case_id}"))
                for case_id in (17, 18, 22)]
    folded = fold_case_details(messages, 17)
    assert "original 17" in folded[0].content and "original 22" in folded[2].content
    assert "original 18" not in folded[1].content and "get_test_case" in folded[1].content
    assert "original 18" in messages[1].content
    assert fold_case_details(folded, 17) == folded


def test_unchanged_ledger_does_not_rewrite_graph_messages():
    async def run():
        channel, state, tools = setup()
        middleware = QaCaseContextMiddleware(state, channel)
        first = await middleware.abefore_model({"messages": [HumanMessage(content="start")]}, None)
        assert first["messages"][0].id == TC_LEDGER_ID
        assert await middleware.abefore_model(first, None) is None
    asyncio.run(run())


def test_real_graph_restores_ids_and_current_spec_after_repeated_lossy_compaction():
    async def run():
        channel, state, tools = setup()
        summary_inputs = []

        class RecordingSummarizer(FakeSummarizer):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                summary_inputs.append(str(messages))
                return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

        summarizer = RecordingSummarizer(summary="No TC specifications or IDs remembered.")
        compaction = build_middleware(state, channel, summarizer, max_input_tokens=1000000,
                                      keep_messages=2, min_new_messages=0)
        # 실제 graph에서 compaction을 두 번 강제로 요청한다.
        turns = [
            {"tool_calls": [{"name": "start_test_case", "args": {"case_id": 17, "thought": "start"}, "id": "s1"}]},
            {"tool_calls": [{"name": "compact_context", "args": {"reason": "test"}, "id": "c1"}]},
            {"tool_calls": [{"name": "report_test_case", "args": {"case_id": 17, "verdict": "PASSED", "message": "observed", "thought": "report"}, "id": "r1"}]},
            {"tool_calls": [{"name": "start_test_case", "args": {"case_id": 17, "thought": "reverify"}, "id": "s2"}]},
            {"tool_calls": [{"name": "compact_context", "args": {"reason": "test again"}, "id": "c2"}]},
            {"tool_calls": [{"name": "get_test_case", "args": {"case_id": 22, "thought": "read after compaction"}, "id": "g1"}]},
            {"content": "done"},
        ]
        model = ScriptedModel(turns=turns)
        agent = create_agent(model, list(tools.values()), middleware=[compaction, QaCaseContextMiddleware(state, channel)])
        await agent.ainvoke({"messages": [HumanMessage(content="Begin")]}, {"recursion_limit": 100})
        captured = model.received
        assert state.compactions == summarizer.calls == 2
        assert len(captured) == 7
        assert all("AUTHORITATIVE TC LEDGER" not in text and "original expected" not in text for text in summary_inputs)
        for index, messages in enumerate(captured):
            ledgers = [m for m in messages if "AUTHORITATIVE TC LEDGER" in str(m.content)]
            assert len(ledgers) == 1
            document = ledger_document(ledgers[0].content)
            assert document["all_case_ids"] == [17, 18, 22, 99]
            current = [None, 17, 17, None, 17, 17, 17][index]
            assert document["current_case_id"] == current
            if current:
                assert f"original expected {current}" in ledgers[0].content
        last_ledger = next(m for m in captured[-1] if "AUTHORITATIVE TC LEDGER" in str(m.content))
        assert ledger_document(last_ledger.content)["reported_ids"] == [17]
        assert any("original expected 22" in str(message.content) for message in captured[-1])
    asyncio.run(run())
