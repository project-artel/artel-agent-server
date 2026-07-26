"""The agent's reasoning has to reach the timeline on its own.

Left to a tool the model chooses to call, it never appeared: in a real run the
log held actions and verdicts but not one line of why. These pin the automatic
capture so that cannot regress silently.
"""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.qa.runner import QaRunner
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType


def make_channel() -> tuple[QaRunChannel, list[dict]]:
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    return QaRunChannel(qa_try_id=7, send=send), sent


def logs(sent: list[dict]) -> list[str]:
    return [
        frame["payload"]["message"]
        for frame in sent
        if frame["type"] == MessageType.LOG.value
    ]


def test_model_turn_becomes_a_timeline_log() -> None:
    async def run() -> None:
        channel, sent = make_channel()
        runner = QaRunner()

        await runner._log_reasoning(
            channel,
            {"model": {"messages": [AIMessage(content="시작 버튼을 눌러 튜토리얼로 들어간다.")]}},
        )

        assert logs(sent) == ["시작 버튼을 눌러 튜토리얼로 들어간다."]

    asyncio.run(run())


def test_tool_and_user_messages_are_not_logged() -> None:
    """Tool results already have their own frames; repeating them buries the reasoning."""

    async def run() -> None:
        channel, sent = make_channel()
        runner = QaRunner()

        await runner._log_reasoning(
            channel,
            {
                "tools": {
                    "messages": [
                        ToolMessage(content="scene: Lobby", tool_call_id="1"),
                        HumanMessage(content="시작해"),
                    ]
                }
            },
        )

        assert logs(sent) == []

    asyncio.run(run())


def test_a_turn_that_only_calls_a_tool_logs_nothing() -> None:
    """No text means nothing was reasoned aloud — an empty log line is noise."""

    async def run() -> None:
        channel, sent = make_channel()
        runner = QaRunner()

        await runner._log_reasoning(
            channel,
            {
                "model": {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {"name": "observe_scene", "args": {}, "id": "1"}
                            ],
                        )
                    ]
                }
            },
        )

        assert logs(sent) == []

    asyncio.run(run())


def test_block_style_content_is_flattened() -> None:
    """Some providers return content as blocks rather than a plain string."""

    async def run() -> None:
        channel, sent = make_channel()
        runner = QaRunner()

        await runner._log_reasoning(
            channel,
            {
                "model": {
                    "messages": [
                        AIMessage(
                            content=[
                                {"type": "text", "text": "화면이 아직 로딩 중이다."},
                                {"type": "tool_use", "name": "observe_scene"},
                            ]
                        )
                    ]
                }
            },
        )

        assert logs(sent) == ["화면이 아직 로딩 중이다."]

    asyncio.run(run())
