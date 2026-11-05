"""TC 판정은 지식 작성과 분리하고 phase의 memory gate는 유지한다."""

import asyncio
from tests.qa_attempts import configure_attempts, start_case, report_case

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools.phase import MAX_CONSECUTIVE_REFUSALS
from app.llm.models import LLMModel
from app.qa.channel import QaRunChannel
from app.qa.scene_context import SceneCapability, SceneContext, SceneContextEntry


def arch_for(mode: PhaseCycleMode):
    return resolve_arch(
        QaArchSpec(vision=VisionMode.on, phase_cycle=mode), "openai/gpt-6-luna"
    )


def make(mode: PhaseCycleMode, total_steps: int = 2, timeout: float = 0.05):
    """A channel, a run state and the tool set one `phase_cycle` value builds."""
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=timeout, write_timeout=timeout)
    state = QaRunState(total_steps=total_steps)
    configure_attempts(channel, state)
    tools = {tool.name: tool for tool in build_tools(channel, state, arch_for(mode))}
    return channel, state, tools, sent


def test_tc_verdict_schema_does_not_require_knowledge_in_any_phase_mode():
    schemas = []
    for mode in PhaseCycleMode:
        _, _, tools, _ = make(mode)
        schemas.append(tools['report_test_case'].args)
        assert 'learned' not in tools['report_test_case'].args
        assert 'capability_key' not in tools['report_test_case'].args
        assert 'record_capability_verdict' in tools and 'record_knowledge' in tools
    assert all(s == schemas[0] for s in schemas)


def test_tc_verdict_is_saved_without_a_memory_update():
    async def run():
        channel, state, tools, sent = make(PhaseCycleMode.remember_in_verdict)
        await start_case(tools)
        await report_case(tools)
        assert state.case_attempt_results[0].verdict == 'PASSED'
        assert state.knowledge_records_attempted == 0
        assert not any(frame['type'].startswith('KNOWLEDGE') for frame in sent)
    asyncio.run(run())


def test_tc_report_keeps_the_phase_memory_gate_after_the_verdict():
    from app.agents.qa.tools.phase import RunPhase
    async def run():
        _, state, tools, _ = make(PhaseCycleMode.lite)
        await start_case(tools)
        await report_case(tools)
        assert state.phase_cycle.phase == RunPhase.update_memory
        response = await tools['skip_memory_update'].ainvoke({'step': 1, 'reason': '재사용할 발견 없음'})
        assert 'Noted' in response
        assert state.phase_cycle.refusal_for('observe_scene') is None
    asyncio.run(run())
