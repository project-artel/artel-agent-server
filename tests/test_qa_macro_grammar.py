"""macro 의 tool 표가 실제 tool 과 같은 것을 게임에 보내는가.

표가 두 벌이다. `action_tools.py` 는 tool 하나하나 안에서 `JsonRpcAction` 을 조립하고,
`app/agents/qa/macro/grammar.py` 의 `TOOLS` 는 같은 조립을 데이터로 든다. 두 벌인 것은
택한 것이다 — `action_tools.py` 를 고치면 그 열여섯 tool 의 소비자가 전부 걸리므로, macro
쪽은 옆에 더했다.

**그 선택의 값을 이 파일이 치른다.** 두 벌은 어긋나고, 어긋난 쪽이 게임에 틀린 byte 를
보낸다. 그래서 여기서 재는 것은 "이름이 있나" 가 아니라 **같은 조작을 두 길로 보냈을 때
나간 것이 같은가** 다. method 가 바뀌거나 `params` 순서가 바뀌거나 배치에 action 이 하나
더 끼면 전부 여기서 걸린다.
"""

import asyncio

import pytest

from app.agents.qa.macro.grammar import (
    PAIRED_TOOLS,
    TOOLS,
    TOOLS_BY_NAME,
    MacroType,
    ToolSpec,
)
from app.agents.qa.tools import QaRunState, build_tools
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType
from app.qa.pulse import PulseReading

# macro 가 조준에 쓰는 하나. 두 길이 같은 것을 겨눠야 나간 byte 를 견줄 수 있다.
TARGET = "Root[0]/Canvas[1]/Button[2]"

# 비-target 인자에 넣을 값. 선언 타입마다 하나씩이고, macro 문법에서도 실제 tool 에서도
# 같은 값이 그대로 `params` 에 실린다.
_VALUES: dict[MacroType, object] = {
    MacroType.int_: 1,
    MacroType.float_: 0.25,
    MacroType.string: "Space",
    MacroType.bool_: True,
}


def make(timeout: float = 0.05):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(
        qa_try_id=11, send=send, action_timeout=timeout, write_timeout=timeout
    )
    state = QaRunState(total_steps=1)
    channel.scene.pulse.apply(
        PulseReading.model_validate(
            {
                "scene": "Battle",
                "whole": True,
                "active": [
                    {
                        "selector": TARGET,
                        "id": 70,
                        "offers": {"clicks": [{"on": "Button", "method": "Go"}]},
                    }
                ],
            }
        )
    )
    tools = {one.name: one for one in build_tools(channel, state)}
    return channel, state, tools, sent


def actions(sent: list[dict]) -> list[list[dict]]:
    return [
        frame["payload"]["actions"]
        for frame in sent
        if frame["type"] == MessageType.ACTION.value
    ]


def answer_the_game(channel: QaRunChannel, sent: list[dict], count: int):
    """게임이 하는 대로 답한다. batch 마다 `ACTION_RESULT` 하나."""

    async def reply() -> None:
        replied = 0
        frames = [
            frame for frame in sent if frame["type"] == MessageType.ACTION.value
        ]
        for _ in range(8000):
            frames = [
                frame for frame in sent if frame["type"] == MessageType.ACTION.value
            ]
            if len(frames) > replied:
                channel.on_action_result(
                    {
                        "correlationId": frames[replied]["messageId"],
                        "payload": {"results": [], "frame": 20 + replied},
                    }
                )
                replied += 1
                if replied >= count:
                    return
            await asyncio.sleep(0)

    return reply


def _macro_call(spec: ToolSpec) -> str:
    """그 tool 을 부르는 macro 한 줄. 모든 인자를 적는다 — 기본값에 기대지 않는다."""
    written = []
    for parameter in spec.parameters:
        if parameter.is_target:
            written.append("aim")
        else:
            written.append(repr(_VALUES[parameter.declared_type]))
    return f"{spec.name}({', '.join(written)})"


def _balanced(spec: ToolSpec) -> tuple[list[ToolSpec], int]:
    """그 tool 하나를 부르는 macro 의 statement 목록과, 그 tool 의 batch 번호.

    눌렀으면 풀어야 하는 tool 은 혼자 설 수 없다 — 파서가 끝에 남은 카운터를 거절하므로
    짝과 함께 적어야 등록이 된다. 그래서 짝을 함께 부르고, 견주는 것은 그중 이 tool 이
    만든 batch 하나다.
    """
    for pair in PAIRED_TOOLS:
        if spec.name == pair.opens:
            return [spec, TOOLS_BY_NAME[pair.closes]], 0
        if spec.name == pair.closes:
            return [TOOLS_BY_NAME[pair.opens], spec], 1
    return [spec], 0


def _direct_arguments(spec: ToolSpec, tool) -> dict:
    """같은 조작을 실제 tool 로 부를 때의 인자.

    실제 tool 의 schema 에서 이름을 읽는다. macro 쪽 표의 이름과 겹치는 자리는 그 값을
    쓰고, target 자리는 그 tool 이 selector 를 받는 이름(`target`·`from_target`·
    `to_target`)이거나 id 를 받는 이름(`target_id`)이다.
    """
    fields = set(tool.args_schema.model_fields)
    arguments: dict = {"step": 1, "thought": "comparing"}
    for parameter in spec.parameters:
        if parameter.is_target:
            if parameter.as_instance_id:
                arguments["target_id"] = 70
            else:
                arguments[parameter.name] = TARGET
            continue
        assert parameter.name in fields, (spec.name, parameter.name, sorted(fields))
        arguments[parameter.name] = _VALUES[parameter.declared_type]
    return arguments


@pytest.mark.parametrize("spec", list(TOOLS), ids=lambda one: one.name)
def test_the_macro_table_sends_what_the_real_tool_sends(spec: ToolSpec) -> None:
    """같은 조작을 두 길로 보내고 나간 것을 견준다.

    `method` 순서와 `params` 순서를 둘 다 본다. 둘 중 하나만 봐도 통과하는 어긋남이
    있고, 그것이 게임에 틀린 byte 를 보내는 쪽이다.
    """
    # 길 하나: 실제 action tool.
    direct_channel, _, direct_tools, direct_sent = make()
    tool = direct_tools[spec.name]

    async def drive_direct() -> None:
        replier = asyncio.create_task(
            answer_the_game(direct_channel, direct_sent, 1)()
        )
        await tool.ainvoke(_direct_arguments(spec, tool))
        replier.cancel()

    asyncio.run(drive_direct())

    # 길 둘: 같은 조작을 적은 macro.
    macro_channel, macro_state, macro_tools, macro_sent = make()
    statements, index = _balanced(spec)
    body = "".join(f"    {_macro_call(one)}\n" for one in statements)
    source = f"def once(aim: object) -> None:\n{body}"

    asyncio.run(
        macro_tools["write_macro"].ainvoke(
            {"step": 1, "thought": "t", "name": "once", "source": source}
        )
    )
    asyncio.run(
        macro_tools["register_macro"].ainvoke(
            {"step": 1, "thought": "t", "name": "once"}
        )
    )

    async def drive_macro() -> None:
        replier = asyncio.create_task(
            answer_the_game(macro_channel, macro_sent, len(statements))()
        )
        await macro_tools["run_macro"].ainvoke(
            {
                "step": 1,
                "thought": "t",
                "name": "once",
                "arguments": {"aim": TARGET},
            }
        )
        replier.cancel()

    asyncio.run(drive_macro())

    assert len(actions(direct_sent)) == 1, spec.name
    # action statement 하나가 batch 하나다. 짝과 함께 적었으면 batch 도 둘이다.
    assert len(actions(macro_sent)) == len(statements), spec.name
    direct_batch = actions(direct_sent)[0]
    macro_batch = actions(macro_sent)[index]

    assert [one["method"] for one in macro_batch] == [
        one["method"] for one in direct_batch
    ], spec.name
    assert [one["params"] for one in macro_batch] == [
        one["params"] for one in direct_batch
    ], spec.name


@pytest.mark.parametrize("spec", list(TOOLS), ids=lambda one: one.name)
def test_every_macro_tool_name_is_a_real_tool(spec: ToolSpec) -> None:
    """이름이 바뀌면 행동 비교가 돌기 전에 여기서 걸린다. 그쪽이 읽기 쉬운 실패다."""
    _, _, tools, _ = make()

    assert spec.name in tools


def test_the_macro_table_names_exactly_the_tools_a_macro_may_call() -> None:
    """빠지는 둘은 `click_button` 과 `reset_game` 이다.

    `action_tools.py` 가 tool 을 하나 더 내놓으면 이 테스트가 그것을 macro 에 열지
    말지 결정하게 만든다. 조용히 빠져 있는 것과 일부러 뺀 것은 다르다.
    """
    _, _, tools, _ = make()
    acting = {
        name
        for name in tools
        if name
        not in {
            "observe_scene",
            "inspect_object",
            "capture_screen",
            "compact_context",
            "search_knowledge",
            "record_knowledge",
            "update_knowledge",
            "forget_knowledge",
            "link_knowledge",
            "unlink_knowledge",
            "expand_knowledge",
            "include_screen_selector",
            "exclude_screen_selector",
            "list_scene_capabilities",
            "record_capability_verdict",
            "record_new_capability",
            "wait_for_operator",
            "report_step",
            "report_issue",
            "finish_run",
            "reply_to_operator",
            "write_macro",
            "edit_macro",
            "read_macro",
            "register_macro",
            "run_macro",
            "resume_macro",
            # phase cycle 의 tool 둘. 게임을 움직이지 않고 런이 어느 phase 에 있는지만
            # 옮긴다 — `decide_next_action` 은 `full` 에서만 나온다.
            "skip_memory_update",
            "decide_next_action",
        }
    }

    assert acting - {one.name for one in TOOLS} == {"click_button", "reset_game"}
