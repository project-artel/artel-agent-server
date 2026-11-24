import asyncio

import pytest

from app.agents.qa.arch import QaArchSpec, resolve_arch, structure_of
from app.agents.qa.tools import QaRunState, build_tools
from app.qa.channel import QaRunChannel


def setup_code(operator_message=None, **options):
    frames = []

    async def send(frame):
        frames.append(frame)
        if operator_message and frame["type"] == "TOOL_RESULT":
            channel.on_chat({"payload": {"message": operator_message}})

    arch = resolve_arch(QaArchSpec(code_mode=True, **options), "openai/gpt-6-luna")
    state = QaRunState(total_steps=1)
    channel = QaRunChannel(qa_try_id=1, send=send)
    tools = build_tools(channel, state, arch)
    assert [entry.name for entry in tools] == ["run_code"]
    return tools[0], frames, arch, state


def invoke(tool, source):
    return asyncio.run(tool.ainvoke({"step": 1, "thought": "Read this case", "source": source}))


@pytest.mark.parametrize("compact", [True, False])
def test_code_is_the_only_model_tool(compact):
    tool, _, arch, state = setup_code(compaction=compact, macros="off")
    assert structure_of(arch)[0] == ("run_code",)
    assert ("compact_context:" in state.code_tool_catalog) == compact
    assert "run_macro:" not in state.code_tool_catalog


def test_conditional_break_records_only_executed_calls():
    tool, frames, _, _ = setup_code()
    result = invoke(tool, """for _ in range(3):
    result = start_test_case(case_id=123)
    if '' in result:
        break
""")
    assert result
    frames = [frame for frame in frames if frame["type"] in {"TOOL", "TOOL_RESULT"}]
    assert [frame["type"] for frame in frames] == ["TOOL", "TOOL_RESULT"]
    assert frames[0]["payload"]["tool"] == "start_test_case"
    assert frames[0]["messageId"] == frames[1]["correlationId"]
    assert frames[1]["payload"]["content"] == result


@pytest.mark.parametrize("invalid", ["import os", "open(path='/tmp/file')", "run_code(source='')",
                                      "start_test_case(**args)", "for _ in range(100):\n    break"])
def test_rejects_entire_invalid_program_before_dispatch(invalid):
    tool, frames, _, _ = setup_code()
    result = invoke(tool, "start_test_case(case_id=1)\n" + invalid)
    assert "Stopped after 0 tool call(s)" in result
    assert not frames


def test_operator_instruction_interrupts_before_second_call():
    tool, frames, _, _ = setup_code(operator_message="Pause here.")
    result = invoke(tool, "get_test_case(case_id=1)\nget_test_case(case_id=2)")
    assert "Pause here." in result
    assert sum(frame["type"] == "TOOL" for frame in frames) == 1


def test_phase_refusal_returns_control_before_retry():
    tool, frames, _, state = setup_code(phase_cycle="full")
    invoke(tool, "for _ in range(8):\n    click_button(target_id=1)")
    assert state.phase_cycle.refusals == 1
    assert state.phase_cycle.forced_passes == 0
    assert sum(frame["type"] == "TOOL" for frame in frames) == 1


def test_scene_result_selects_game_input_and_returns_its_outcome():
    from tests.test_qa_tools import actions, make, scene

    async def play():
        channel, state, _, frames = make()
        channel.on_game_state(scene({"Score": {"value": 0}}))
        arch = resolve_arch(QaArchSpec(code_mode=True, phase_cycle="off"), "openai/gpt-6-luna")
        code, = build_tools(channel, state, arch)
        source = "view = observe_scene(current_scene=True)\nif 'Score' in view:\n    click_button(target_id=42)"

        async def answer_input():
            while not actions(frames):
                await asyncio.sleep(0)
            channel.on_action_result({
                "correlationId": actions(frames)[0]["messageId"],
                "payload": {"results": [{"id": 1, "success": True}]},
            })

        result, _ = await asyncio.wait_for(asyncio.gather(
            code.ainvoke({"step": 1, "thought": "Read score and click", "source": source}),
            answer_input(),
        ), timeout=3)
        assert "button_click: ok" in result
        assert [frame["payload"]["tool"] for frame in frames if frame["type"] == "TOOL"] == [
            "observe_scene", "click_button",
        ]
        assert actions(frames)[0]["payload"]["actions"][0]["params"] == [42]

    asyncio.run(play())


def test_call_limit_stops_a_sequence_after_sixteen_calls():
    tool, frames, _, _ = setup_code()
    result = invoke(tool, "for _ in range(16):\n    start_test_case(case_id=1)\nstart_test_case(case_id=2)")
    assert "Stopped after 16 tool call(s)" in result
    assert len([frame for frame in frames if frame["type"] == "TOOL"]) == 16
