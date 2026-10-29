"""게임 예외를 안 본 수 한 줄과 `get_errors` 로 보이는 규칙 (ARTEL-1219).

SDK 쪽(ARTEL-1218, artel-sdk PR #154)은 따로 들어간다. 여기 fixture 는 그 계약의 모양이다 —
`errors` 는 항목 `{type, message, stackHead, firstFrame, lastFrame, count, truncated}` 의 목록이고,
새 항목이나 `count` 가 는 항목만 frame 당 20개까지 실린다. 넘친 것은 다음 `pulse` 로 넘어간다.
`truncated` 는 `message` 가 200자에서 잘렸다는 뜻이다.
"""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools.phase import ANY_PHASE
from app.qa.channel import QaRunChannel
from app.qa.game_errors import GameErrorLedger, errors_in


def item(message: str = "story advance handler", count: int = 1, **extra) -> dict:
    return {
        "type": "NullReferenceException",
        "message": message,
        "stackHead": "StoryManager.Advance () (at Assets/Scripts/StoryManager.cs:88)",
        "firstFrame": 120,
        "lastFrame": 120 + count,
        "count": count,
        **extra,
    }


def pulse(errors, **extra) -> dict:
    return {"type": "PULSE", "payload": {"reading": 1, "frame": 200, "errors": errors, **extra}}


def make(mode: PhaseCycleMode = PhaseCycleMode.off):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=0.05, write_timeout=0.05)
    state = QaRunState(total_steps=2)
    arch = resolve_arch(QaArchSpec(vision=VisionMode.off, phase_cycle=mode), "openai/gpt-6-luna")
    tools = {tool.name: tool for tool in build_tools(channel, state, arch)}
    return channel, state, tools


def test_new_items_are_unseen_and_the_line_counts_them() -> None:
    ledger = GameErrorLedger()
    assert ledger.summary_line() is None

    ledger.apply({"errors": [item("a"), item("b")]})

    assert ledger.unseen_count() == 2
    assert ledger.summary_line() == "unseen errors: 2"


def test_taking_marks_seen_and_a_growing_count_is_not_counted_again() -> None:
    """같은 예외가 매 frame 나는 빌드에서 `get_errors` 한 번 뒤 N 이 0 으로 남아야 한다."""
    ledger = GameErrorLedger()
    ledger.apply({"errors": [item(count=1)]})
    taken = ledger.take()
    assert [entry.count for entry in taken] == [1]

    for count in range(2, 40):
        ledger.apply({"errors": [item(count=count)]})

    assert ledger.unseen_count() == 0
    assert ledger.summary_line() is None
    assert [entry.count for entry in ledger.take(include_seen=True)] == [39]


def test_a_new_item_after_reading_is_counted() -> None:
    ledger = GameErrorLedger()
    ledger.apply({"errors": [item("a")]})
    ledger.take()
    ledger.apply({"errors": [item("a", count=5), item("b")]})

    assert ledger.unseen_count() == 1
    assert [entry.message for entry in ledger.take()] == ["b"]


def test_items_differing_in_the_stack_head_are_two_items() -> None:
    ledger = GameErrorLedger()
    ledger.apply({"errors": [item(), item(stackHead="Other.Method ()")]})
    assert ledger.unseen_count() == 2


def test_errors_are_read_off_a_list_and_nothing_else() -> None:
    assert [entry.message for entry in errors_in({"errors": [item(), item("b")]})] == [
        "story advance handler",
        "b",
    ]
    assert errors_in({"reading": 3}) == []
    assert errors_in({"errors": {"items": [item()]}}) == []
    assert errors_in(None) == []


def test_a_malformed_item_is_dropped_and_the_rest_kept() -> None:
    listed = errors_in({"errors": [item(), {"count": "many"}, "text"]})
    assert len(listed) == 1


def test_the_channel_reads_errors_off_pulse_and_action_result() -> None:
    channel, _state, _tools = make()
    channel.on_pulse(pulse([item("from pulse")]))
    channel.on_action_result(
        {"type": "ACTION_RESULT", "payload": {"results": [], "errors": [item("from action")]}}
    )
    assert channel.game_errors.unseen_count() == 2


def test_every_tool_answer_ends_with_the_line_and_get_errors_clears_it() -> None:
    async def run() -> None:
        channel, _state, tools = make()
        channel.on_pulse(pulse([item()]))

        looked = await tools["observe_scene"].ainvoke({"step": 1, "thought": "look"})
        assert looked.rstrip().endswith("unseen errors: 1")

        listed = await tools["get_errors"].ainvoke({"step": 1, "thought": "check"})
        assert "1 unseen error(s), now marked seen:" in listed
        assert "NullReferenceException: story advance handler (×1, first frame 120, last frame 121)" in listed
        assert "at StoryManager.Advance ()" in listed
        assert "`report_issue`" in listed
        assert "unseen errors" not in listed

        channel.on_pulse(pulse([item(count=9)]))
        again = await tools["observe_scene"].ainvoke({"step": 1, "thought": "look"})
        assert "unseen errors" not in again

        empty = await tools["get_errors"].ainvoke({"step": 1, "thought": "check"})
        assert empty.startswith("No unseen errors. 1 error(s) you have already read")

        everything = await tools["get_errors"].ainvoke(
            {"step": 1, "thought": "all", "include_seen": True}
        )
        assert "(×9," in everything

    asyncio.run(run())


def test_a_run_without_errors_reads_no_line() -> None:
    async def run() -> None:
        channel, _state, tools = make()
        channel.on_pulse({"type": "PULSE", "payload": {"reading": 1, "frame": 10}})
        looked = await tools["observe_scene"].ainvoke({"step": 1, "thought": "look"})
        assert "unseen errors" not in looked
        assert (await tools["get_errors"].ainvoke({"step": 1, "thought": "x"})) == (
            "The game has reported no errors this run."
        )

    asyncio.run(run())


def test_a_message_the_sdk_cut_is_marked() -> None:
    async def run() -> None:
        channel, _state, tools = make()
        channel.on_pulse(pulse([item("x" * 200, truncated=True)]))
        listed = await tools["get_errors"].ainvoke({"step": 1, "thought": "x"})
        assert "[message cut at 200 chars]" in listed

    asyncio.run(run())


def test_the_sdks_own_logs_are_listed_apart_and_never_counted() -> None:
    """SDK 자신의 `[Artel]` Error log 는 게임 결함이 아니다. N 에 넣으면 매 런 오탐 후보가 된다."""

    async def run() -> None:
        channel, _state, tools = make()
        channel.on_pulse(pulse([item("[Artel] pulse send failed", type="Error")]))
        looked = await tools["observe_scene"].ainvoke({"step": 1, "thought": "look"})
        assert "unseen errors" not in looked

        channel.on_pulse(pulse([item("game broke")]))
        assert channel.game_errors.summary_line() == "unseen errors: 1"
        listed = await tools["get_errors"].ainvoke({"step": 1, "thought": "x"})
        assert listed.startswith("1 unseen error(s), now marked seen:\n- NullReferenceException: game broke")
        assert "1 log(s) from the ARTEL SDK itself, not from the game" in listed
        assert "[Artel] pulse send failed" in listed

    asyncio.run(run())


def test_get_errors_is_not_refused_between_act_and_verify() -> None:
    """판정 직전에 부르는 tool 이다. ACT 를 지난 phased run 에서 거절되면 규칙이 지켜질 수 없다."""
    assert "get_errors" in ANY_PHASE

    async def run() -> None:
        channel, state, tools = make(PhaseCycleMode.lite)
        channel.on_pulse(pulse([item()]))
        await tools["press_key"].ainvoke({"step": 1, "key_code": "Return", "duration_seconds": 0.1, "thought": "advance"})
        answer = await tools["get_errors"].ainvoke({"step": 1, "thought": "check"})
        assert answer.startswith("1 unseen error(s)")
        channel.evaluation.attempts[31] = uuid4()
        channel.evaluation.report = AsyncMock()
        verdict = await tools["report_test_case"].ainvoke(
            {"case_id": 31, "message": "expected: x\nobserved: x", "verdict": "PASSED", "thought": "ok"}
        )
        channel.evaluation.report.assert_awaited_once()
        assert not verdict.startswith("Not now")

    asyncio.run(run())
