"""macro 초안이 판정 뒤에 어떻게 내밀어지고 `skip_memory_update` 에서 어떻게 답받는지 재는 테스트.

초안은 통과한 판정에만 붙는다. 내민 초안은 읽은 것으로 치므로 `edit_macro` 가 바로 받는다.
`UPDATE_MEMORY` 를 건너뛰려는 호출이 초안 이름을 안 대면 한 번만 되돌아오고, 이름을 대거나
등록하면 첫 호출에 통과한다. 제안 문구는 다른 곳에서 바뀌므로 상태와 이 파일의 거절 문구만
본다.
"""

import asyncio

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools.phase import RunPhase
from app.llm.models import LLMModel
from app.qa.channel import QaRunChannel
from app.qa.pulse import PulseReading

DRAFT = "replay_step_1"
REFUSAL = "has no answer yet"


def make(macros: str):
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
        QaArchSpec(vision=VisionMode.on, phase_cycle=PhaseCycleMode.lite, macros=macros),
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


async def offered_draft(macros: str = "on"):
    state, tools = make(macros)
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

        # 실패 판정 뒤에 `UPDATE_MEMORY` 를 지나 다시 판정하는 길을 gate 앞에서 흉내 낸다.
        state.phase_cycle.phase = RunPhase.act
        await report(tools, passed=True)
        assert DRAFT in state.macros.drafts
        assert state.offered_drafts == {1: DRAFT}

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

    asyncio.run(scenario())


def test_a_reason_that_does_not_name_the_draft_is_refused_once() -> None:
    async def scenario() -> None:
        state, tools = await offered_draft()
        assert state.phase_cycle.phase is RunPhase.update_memory

        refused = await skip(tools, "the tutorial lock is still on")
        assert REFUSAL in refused and DRAFT in refused
        assert state.phase_cycle.phase is RunPhase.update_memory
        assert state.phase_cycle.refusal_for("capture_screen") is not None

        passed = await skip(tools, "the tutorial lock is still on")
        assert REFUSAL not in passed and "Noted" in passed
        assert state.phase_cycle.refusal_for("capture_screen") is None

    asyncio.run(scenario())


def test_a_reason_that_names_the_draft_passes_at_once() -> None:
    async def scenario() -> None:
        state, tools = await offered_draft()

        passed = await skip(tools, f"{DRAFT} would only repeat once, so it is not kept")

        assert "Noted" in passed
        assert state.phase_cycle.refusal_for("capture_screen") is None

    asyncio.run(scenario())


def test_registering_the_draft_then_skipping_passes_at_once() -> None:
    async def scenario() -> None:
        state, tools = await offered_draft()

        await tools["edit_macro"].ainvoke(
            {"step": 1, "thought": "t", "name": DRAFT, "old_text": "0.1", "new_text": "0.2"}
        )
        await tools["register_macro"].ainvoke({"step": 1, "thought": "t", "name": DRAFT})
        assert state.macros.registered(DRAFT) is not None

        passed = await skip(tools, "registered it above")
        assert "Noted" in passed

    asyncio.run(scenario())


def test_a_step_without_an_offered_draft_is_answered_as_before() -> None:
    async def scenario() -> None:
        state, tools = await offered_draft()

        # 초안은 step 1 에만 있다. 다른 step 의 답은 그것을 묻지 않는다.
        passed = await skip(tools, "nothing new", step=2)

        assert "Noted" in passed
        assert state.asked_drafts == set()

    asyncio.run(scenario())


def test_a_run_with_macros_off_is_answered_as_before() -> None:
    async def scenario() -> None:
        state, tools = await offered_draft("off")

        assert state.offered_drafts == {}
        passed = await skip(tools, "nothing new")
        assert "Noted" in passed

    asyncio.run(scenario())
