"""The agent's reasoning has to reach the timeline on its own.

Left to a tool the model chooses to call, it never appeared: in a real run the
log held actions and verdicts but not one line of why. These pin the automatic
capture so that cannot regress silently.
"""

import asyncio
import logging
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.qa.runner import QaRunner, _log_token_usage
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


def test_a_thinking_block_reaches_the_timeline() -> None:
    """Anthropic puts the reasoning in a `thinking` block, not a `text` one.

    Keeping only `text` is why a whole run finished with 48 THOUGHT rows that all
    came from the tools' own `thought` argument and none from the model itself.
    """

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
                                {"type": "thinking", "thinking": "상점 버튼부터 눌러야 한다."},
                                {"type": "tool_use", "name": "click_button"},
                            ]
                        )
                    ]
                }
            },
        )

        assert logs(sent) == ["상점 버튼부터 눌러야 한다."]

    asyncio.run(run())


def test_a_reasoning_block_reaches_the_timeline() -> None:
    """Several models fronted by OpenRouter name the same block `reasoning`."""

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
                                {"type": "reasoning", "reasoning": "골드가 줄었는지 확인하자."}
                            ]
                        )
                    ]
                }
            },
        )

        assert logs(sent) == ["골드가 줄었는지 확인하자."]

    asyncio.run(run())


def test_reasoning_carried_beside_the_content_reaches_the_timeline() -> None:
    """Some providers hang it off `additional_kwargs` instead of the content."""

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
                            additional_kwargs={"reasoning_content": "먼저 화면을 봐야 한다."},
                        )
                    ]
                }
            },
        )

        assert logs(sent) == ["먼저 화면을 봐야 한다."]

    asyncio.run(run())


def test_reasoning_sent_both_ways_is_logged_once() -> None:
    """A provider that duplicates it must not double the timeline."""

    async def run() -> None:
        channel, sent = make_channel()
        runner = QaRunner()

        await runner._log_reasoning(
            channel,
            {
                "model": {
                    "messages": [
                        AIMessage(
                            content=[{"type": "thinking", "thinking": "상점을 열어 본다."}],
                            additional_kwargs={"reasoning": "상점을 열어 본다."},
                        )
                    ]
                }
            },
        )

        assert logs(sent) == ["상점을 열어 본다."]

    asyncio.run(run())


def test_token_usage_log_reports_cache_reads(caplog) -> None:
    """Cache misses are silent, so the run log has to carry the hit count.

    Drives the middleware directly: it only reads `usage_metadata` off whatever
    the handler returned, so a fake response exercises all of it.
    """
    reply = AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 5000,
            "output_tokens": 100,
            "total_tokens": 5100,
            "input_token_details": {"cache_read": 4800},
        },
    )

    async def handler(_request):
        return SimpleNamespace(result=[reply])

    with caplog.at_level(logging.INFO):
        asyncio.run(_log_token_usage.awrap_model_call(object(), handler))

    assert "cache_read': 4800" in caplog.text
    assert "input=5000" in caplog.text


def test_a_middleware_node_s_rewrite_is_not_logged_as_a_new_turn() -> None:
    """Compaction reports its whole rewritten conversation as its node update.

    Read like a model turn, every preserved AIMessage in it would be logged and put
    on the timeline a second time — the operator would watch the agent's reasoning
    repeat itself after each compaction with no clue why. Only `model` and `tools`
    produce turns that actually happened.
    """

    async def run() -> None:
        channel, sent = make_channel()
        runner = QaRunner()

        await runner._log_reasoning(
            channel,
            {
                "QaCompactionMiddleware.before_model": {
                    "messages": [
                        HumanMessage(content="요약"),
                        AIMessage(content="상점을 열어 본다."),
                    ]
                }
            },
        )

        assert logs(sent) == []

    asyncio.run(run())
