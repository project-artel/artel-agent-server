"""`decline_macro_draft`: 통과한 step 의 초안을 이름과 이유를 대고 거절하는 tool."""

import asyncio

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.llm.models import LLMModel
from app.qa.channel import QaRunChannel

DRAFT = "def replay_step_1() -> None:\n    press_key(\"Space\", 0.1)\n    press_key(\"Space\", 0.1)\n"


class _RecordingCycle:
    """`hold` 가 불렸는지만 세는 phase 상태 기계 대역."""

    def __init__(self) -> None:
        self.held = 0

    def hold(self) -> None:
        self.held += 1


def make(macros: str):
    async def send(frame: dict) -> None:
        return None

    channel = QaRunChannel(qa_try_id=9, send=send, action_timeout=0.05, write_timeout=0.05)
    state = QaRunState(total_steps=3)
    arch = resolve_arch(
        QaArchSpec(vision=VisionMode.on, phase_cycle=PhaseCycleMode.off, macros=macros),
        LLMModel.gpt_6_luna,
    )
    tools = {one.name: one for one in build_tools(channel, state, arch)}
    return state, tools


def offered():
    state, tools = make("on")
    state.offered_drafts[1] = "replay_step_1"
    state.macros.write("replay_step_1", DRAFT)
    state.phase_cycle = _RecordingCycle()
    return state, tools


def decline(tools, **overrides) -> str:
    arguments = {
        "step": 1,
        "thought": "t",
        "name": "replay_step_1",
        "reason": "the dialogue length changes every build",
    }
    arguments.update(overrides)
    return asyncio.run(tools["decline_macro_draft"].ainvoke(arguments))


def test_a_name_that_is_not_the_offered_draft_is_refused_and_names_the_offered_one() -> None:
    state, tools = offered()

    answer = decline(tools, name="replay_step_2")

    assert "`replay_step_1`" in answer and "Nothing was recorded" in answer
    assert state.declined_drafts == {}


def test_a_step_with_no_offered_draft_says_so() -> None:
    state, tools = offered()

    answer = decline(tools, step=2)

    assert "no macro draft was offered for step 2" in answer
    assert state.declined_drafts == {}


def test_an_empty_reason_is_refused_and_holds_the_phase() -> None:
    state, tools = offered()

    answer = decline(tools, reason="   ")

    assert "`reason` is empty" in answer and "nothing was recorded" in answer
    assert state.declined_drafts == {}
    assert state.phase_cycle.held == 1


def test_the_offered_name_and_a_reason_are_recorded_and_the_draft_text_stays() -> None:
    state, tools = offered()

    answer = decline(tools)

    assert "dropped" in answer and "later runs" in answer
    assert state.declined_drafts == {"replay_step_1": "the dialogue length changes every build"}
    assert state.macros.draft("replay_step_1") == DRAFT
    assert state.phase_cycle.held == 0


def test_a_declined_draft_can_still_be_registered_if_the_agent_changes_its_mind() -> None:
    state, tools = offered()
    decline(tools)

    asyncio.run(
        tools["register_macro"].ainvoke({"step": 1, "thought": "t", "name": "replay_step_1"})
    )

    assert state.macros.registered("replay_step_1") is not None


def test_a_run_with_macros_off_has_no_such_tool() -> None:
    _state, tools = make("off")

    assert "decline_macro_draft" not in tools
