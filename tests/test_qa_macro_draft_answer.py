"""macro 초안이 판정 뒤에 어떻게 내밀어지고 `REVIEW_DRAFT` 에서 어떻게 답받는지 재는 테스트.

초안은 통과한 판정에만 붙는다. 내민 초안은 읽은 것으로 치므로 `edit_macro` 가 바로 받는다.
phase 를 강제하는 런에서 초안을 내민 판정은 `UPDATE_MEMORY` 앞에 `REVIEW_DRAFT` 를 두고, 그
phase 는 `register_macro` 나 `decline_macro_draft` 로만 끝난다. `skip_memory_update` 는 다시 지식
질문 하나에만 답한다. 제안 문구는 다른 곳에서 바뀌므로 상태와 gate 의 거절 문구만 본다.

`decline_macro_draft` 를 부르는 테스트는 그 tool(`macro_tools.py`)이 있어야 돈다.
"""

import asyncio

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools.phase import RunPhase
from app.llm.models import LLMModel
from app.qa.channel import QaRunChannel
from app.qa.pulse import PulseReading

DRAFT = "replay_step_1"
REVIEW_REFUSAL = "REVIEW_DRAFT ends when you call one of: `decline_macro_draft`, `register_macro`"


def make(macros: str, phase_cycle: PhaseCycleMode = PhaseCycleMode.lite):
    async def send(frame: dict) -> None:
        pass

    channel = QaRunChannel(qa_try_id=9, send=send, action_timeout=0.05, write_timeout=0.05)
    channel.scene.pulse.apply(
        PulseReading.model_validate(
            {
                "scene": "Battle",
                "whole": True,
                "active": [
                    {
                        "selector": "Root[0]/Canvas[1]/CombineButton[2]",
                        "id": 51,
                        "text": "Combine",
                        "rect": {"x": 100, "y": 100, "w": 80, "h": 30},
                    }
                ],
            }
        )
    )
    state = QaRunState(total_steps=3)
    arch = resolve_arch(
        QaArchSpec(vision=VisionMode.on, phase_cycle=phase_cycle, macros=macros),
        LLMModel.gpt_6_luna,
    )
    tools = {one.name: one for one in build_tools(channel, state, arch)}
    return state, tools


async def send_actions(tools, step: int = 1) -> None:
    for _ in range(3):
        await tools["press_key"].ainvoke(
            {"step": step, "thought": "t", "key_code": "Space", "duration_seconds": 0.1}
        )
    await tools["click"].ainvoke({"step": step, "thought": "t", "target": "#51"})


async def report(tools, passed: bool, step: int = 1) -> str:
    return await tools["report_step"].ainvoke(
        {"step": step, "passed": passed, "message": "m", "thought": "t", "learned": ""}
    )


async def skip(tools, reason: str, step: int = 1) -> str:
    return await tools["skip_memory_update"].ainvoke({"step": step, "reason": reason})


async def decline(tools, name: str, reason: str, step: int = 1) -> str:
    return await tools["decline_macro_draft"].ainvoke(
        {"step": step, "thought": "t", "name": name, "reason": reason}
    )


async def offered_draft(macros: str = "on", phase_cycle: PhaseCycleMode = PhaseCycleMode.lite):
    state, tools = make(macros, phase_cycle)
    await send_actions(tools)
    await report(tools, passed=True)
    return state, tools


def test_a_failed_verdict_gets_no_draft_and_a_later_pass_of_the_same_step_does() -> None:
    async def scenario() -> None:
        state, tools = make("on")
        await send_actions(tools)

        await report(tools, passed=False)
        assert state.macros.drafts == {}
        assert state.offered_drafts == {}
        assert state.drafted_steps == set()
        # 초안이 없으니 `REVIEW_DRAFT` 도 없다.
        assert state.phase_cycle.phase is RunPhase.update_memory

        # 실패 판정 뒤에 `UPDATE_MEMORY` 를 지나 다시 판정하는 길을 gate 앞에서 흉내 낸다.
        state.phase_cycle.phase = RunPhase.act
        await report(tools, passed=True)
        assert DRAFT in state.macros.drafts
        assert state.offered_drafts == {1: DRAFT}
        assert state.phase_cycle.phase is RunPhase.review_draft

    asyncio.run(scenario())


def test_an_offered_draft_puts_the_run_in_review_draft() -> None:
    async def scenario() -> None:
        state, _tools = await offered_draft()

        assert state.offered_drafts == {1: DRAFT}
        assert state.phase_cycle.phase is RunPhase.review_draft

    asyncio.run(scenario())


def test_an_offered_draft_can_be_edited_without_read_macro() -> None:
    async def scenario() -> None:
        state, tools = await offered_draft()
        assert state.macros.was_read(DRAFT)

        answer = await tools["edit_macro"].ainvoke(
            {"step": 1, "thought": "t", "name": DRAFT, "old_text": "0.1", "new_text": "0.2"}
        )

        assert "not read" not in answer
        assert "0.2" in state.macros.draft(DRAFT)
        # 고친 것은 답이 아니다. 아직 등록도 거절도 안 했다.
        assert state.phase_cycle.phase is RunPhase.review_draft

    asyncio.run(scenario())


def test_skipping_the_memory_update_does_not_answer_the_draft() -> None:
    """종전에 34 개 중 약 20 개의 초안이 이 한 호출로 답 없이 지나갔다."""

    async def scenario() -> None:
        state, tools = await offered_draft()

        refused = await skip(tools, "the tutorial lock is still on")

        assert REVIEW_REFUSAL in refused
        assert "Noted" not in refused
        assert state.phase_cycle.phase is RunPhase.review_draft
        assert state.macros.registered(DRAFT) is None

    asyncio.run(scenario())


def test_editing_then_registering_ends_review_draft_and_update_memory_is_asked_on_its_own() -> None:
    async def scenario() -> None:
        state, tools = await offered_draft()

        await tools["edit_macro"].ainvoke(
            {"step": 1, "thought": "t", "name": DRAFT, "old_text": "0.1", "new_text": "0.2"}
        )
        await tools["register_macro"].ainvoke({"step": 1, "thought": "t", "name": DRAFT})
        assert state.macros.registered(DRAFT) is not None
        assert state.phase_cycle.phase is RunPhase.update_memory

        # 지식 질문은 따로 남아 있다. 초안 이름을 안 대도 받는다.
        assert state.phase_cycle.refusal_for("capture_screen") is not None
        passed = await skip(tools, "the tutorial lock is still on")
        assert "Noted" in passed
        assert state.phase_cycle.refusal_for("capture_screen") is None

    asyncio.run(scenario())


def test_a_registration_that_fails_keeps_review_draft_open() -> None:
    """등록이 거절된 호출로 `REVIEW_DRAFT` 가 끝나면 초안은 답 없이 사라진다."""

    async def scenario() -> None:
        state, tools = await offered_draft()

        # `edit_macro` 는 문법을 깨는 수정을 받지 않으므로, 거절되는 등록은 대개 이름을
        # 잘못 적은 경우다.
        refused = await tools["register_macro"].ainvoke(
            {"step": 1, "thought": "t", "name": "no_such_draft"}
        )

        assert "nothing was registered" in refused
        assert state.macros.registered(DRAFT) is None
        assert state.phase_cycle.phase is RunPhase.review_draft
        # 맞는 이름으로 다시 등록하면 끝난다.
        await tools["register_macro"].ainvoke({"step": 1, "thought": "t", "name": DRAFT})
        assert state.phase_cycle.phase is RunPhase.update_memory

    asyncio.run(scenario())


def test_declining_the_draft_ends_review_draft_and_records_the_reason() -> None:
    """`decline_macro_draft` 가 있어야 돈다."""

    async def scenario() -> None:
        state, tools = await offered_draft()

        await decline(tools, DRAFT, "the tutorial only runs once")

        assert state.declined_drafts == {DRAFT: "the tutorial only runs once"}
        assert state.macros.registered(DRAFT) is None
        assert state.phase_cycle.phase is RunPhase.update_memory
        assert state.phase_cycle.refusal_for("capture_screen") is not None

        passed = await skip(tools, "nothing new")
        assert "Noted" in passed

    asyncio.run(scenario())


def test_declining_a_draft_that_was_not_offered_keeps_review_draft_open() -> None:
    """`decline_macro_draft` 가 있어야 돈다. 이름이 틀린 거절은 그 tool 이 `hold` 한다."""

    async def scenario() -> None:
        state, tools = await offered_draft()

        await decline(tools, "replay_step_9", "wrong one")

        assert state.declined_drafts == {}
        assert state.phase_cycle.phase is RunPhase.review_draft

    asyncio.run(scenario())


def test_a_step_without_an_offered_draft_is_answered_as_before() -> None:
    async def scenario() -> None:
        state, tools = await offered_draft()
        await tools["register_macro"].ainvoke({"step": 1, "thought": "t", "name": DRAFT})
        await skip(tools, "nothing new")

        # step 2 는 손으로 보낸 것이 없어 초안이 없다. 판정은 곧장 `UPDATE_MEMORY` 로 간다.
        await report(tools, passed=True, step=2)
        assert 2 not in state.offered_drafts
        assert state.phase_cycle.phase is RunPhase.update_memory

        passed = await skip(tools, "nothing new", step=2)
        assert "Noted" in passed

    asyncio.run(scenario())


def test_a_run_with_macros_off_is_answered_as_before() -> None:
    async def scenario() -> None:
        state, tools = await offered_draft("off")

        assert state.offered_drafts == {}
        assert state.phase_cycle.phase is RunPhase.update_memory
        passed = await skip(tools, "nothing new")
        assert "Noted" in passed

    asyncio.run(scenario())


def test_a_run_without_a_phase_gate_still_gets_the_draft_and_no_phase() -> None:
    """`off` 와 `in_verdict` 는 phase 를 안 세므로 `REVIEW_DRAFT` 도 없다. 초안은 종전대로 붙는다."""

    async def scenario() -> None:
        for phase_cycle in (PhaseCycleMode.off, PhaseCycleMode.remember_in_verdict):
            state, tools = make("on", phase_cycle)
            await send_actions(tools)
            if phase_cycle is PhaseCycleMode.off:
                await tools["report_step"].ainvoke(
                    {"step": 1, "passed": True, "message": "m", "thought": "t"}
                )
            else:
                await report(tools, passed=True)

            assert state.phase_cycle is None
            assert state.offered_drafts == {1: DRAFT}

    asyncio.run(scenario())
