"""`report_step` 의 `performed` 와 `passed` 를 따로 받는 규칙 (ARTEL-1220).

2026-10-22 L1 측정에서 Claude Sonnet 5.5 는 거짓 기대값 5개 중 4개를 매번 통과로 보고했다. 거부된
행동을 message 에 맞게 적고도 `passed=true` 였다. 하지 못한 행동을 통과로 보고하면 step 마다 한 번
돌려보낸다.
"""

import asyncio

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.qa.channel import QaRunChannel


def make(mode: PhaseCycleMode = PhaseCycleMode.off, execution_mode: str = "ORDERED"):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=0.05, write_timeout=0.05)
    state = QaRunState(
        total_steps=2,
        step_meta=[(31, True), (32, True)],
        execution_mode=execution_mode,
    )
    arch = resolve_arch(
        QaArchSpec(vision=VisionMode.off, phase_cycle=mode), "openai/gpt-6-luna"
    )
    tools = {tool.name: tool for tool in build_tools(channel, state, arch)}
    return state, tools, sent


def statuses(sent: list[dict]) -> list[dict]:
    return [frame["payload"] for frame in sent if frame["type"] == "STATUS"]


REFUSED_BUT_PASSED = {
    "step": 1,
    "message": "expected: 발동 버튼이 나타난다\nobserved: 버튼이 숨겨져 있어 누를 수 없음",
    "performed": False,
    "passed": True,
    "thought": "버튼이 없다",
}


def test_an_unperformed_pass_is_sent_back_once_and_records_nothing() -> None:
    async def run() -> None:
        state, tools, sent = make()
        answer = await tools["report_step"].ainvoke(REFUSED_BUT_PASSED)

        assert answer.startswith("Nothing was recorded")
        assert "`performed=false`" in answer
        assert "`report_step` again with `passed=false`" in answer
        assert state.step_results == []
        assert statuses(sent) == []

    asyncio.run(run())


def test_the_same_report_sent_again_is_recorded() -> None:
    """기대값이 "누를 수 없다" 인 step 에서는 이 조합이 맞다. 두 번째는 받는다."""

    async def run() -> None:
        state, tools, sent = make()
        await tools["report_step"].ainvoke(REFUSED_BUT_PASSED)
        answer = await tools["report_step"].ainvoke(REFUSED_BUT_PASSED)

        assert answer.startswith("Recorded.")
        assert [result.passed for result in state.step_results] == [True]
        assert [payload["status"] for payload in statuses(sent)] == ["COMPLETED"]

    asyncio.run(run())


def test_an_unperformed_failure_is_recorded_at_once() -> None:
    async def run() -> None:
        state, tools, sent = make()
        answer = await tools["report_step"].ainvoke({**REFUSED_BUT_PASSED, "passed": False})

        assert answer.startswith("Recorded.")
        assert [result.passed for result in state.step_results] == [False]
        assert [payload["status"] for payload in statuses(sent)] == ["FAILED"]

    asyncio.run(run())


def test_the_refusal_is_per_step() -> None:
    """step 1 에서 한 번 돌려보냈다고 step 2 의 같은 조합이 그냥 지나가지 않는다."""

    async def run() -> None:
        state, tools, _sent = make()
        await tools["report_step"].ainvoke(REFUSED_BUT_PASSED)
        await tools["report_step"].ainvoke(REFUSED_BUT_PASSED)
        answer = await tools["report_step"].ainvoke({**REFUSED_BUT_PASSED, "step": 2})

        assert answer.startswith("Nothing was recorded")
        assert [result.step for result in state.step_results] == [1]

    asyncio.run(run())


def test_a_phased_run_stays_in_verify_after_the_refusal() -> None:
    """돌려보낸 판정이 VERIFY 를 끝내면 `UPDATE_MEMORY` 가 없는 판정을 두고 묻는다."""

    async def run() -> None:
        state, tools, _sent = make(PhaseCycleMode.lite)
        await tools["report_step"].ainvoke({**REFUSED_BUT_PASSED, "learned": ""})
        answer = await tools["skip_memory_update"].ainvoke(
            {"step": 1, "reason": "없음", "thought": "다음으로"}
        )

        assert state.step_results == []
        assert answer.startswith("Not now")
        assert "VERIFY ends when you call one of: `report_step`" in answer

    asyncio.run(run())


def test_a_free_run_names_report_case_in_the_refusal() -> None:
    async def run() -> None:
        state, tools, _sent = make(execution_mode="FREE")
        answer = await tools["report_case"].ainvoke(
            {
                "case_id": 31,
                "message": REFUSED_BUT_PASSED["message"],
                "performed": False,
                "passed": True,
                "thought": "버튼이 없다",
            }
        )

        assert "`report_case` again with `passed=false`" in answer
        assert state.step_results == []

    asyncio.run(run())
