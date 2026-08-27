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

    request = SimpleNamespace(messages=[HumanMessage(content="상점을 열어 본다.")])

    with caplog.at_level(logging.INFO):
        asyncio.run(_log_token_usage.awrap_model_call(request, handler))

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


# --- tool calls ---------------------------------------------------------------
#
# 로그가 에이전트의 산문으로만 채워지던 것을 고친 자리다(ARTEL-609). tool 이름과 인자는
# 러너가 이미 손에 쥐고 있었지만 stdout 로만 나갔고, 타임라인에는 각 tool 이 스스로 남기는
# `thought` 한 줄뿐이었다 — "씬 캡처 했습니다" 같은 문장. 여기서 내면 tool 28개가 한 번에
# 덮이고, 새 tool 이 생겨도 저절로 따라온다.


def frames_of(sent: list[dict], message_type: MessageType) -> list[dict]:
    return [frame for frame in sent if frame["type"] == message_type.value]


def test_a_tool_call_becomes_a_tool_frame() -> None:
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
                                {
                                    "name": "search_knowledge",
                                    "args": {"step": 2, "query": "보스전 진입 조건"},
                                    "id": "call_abc123",
                                }
                            ],
                        )
                    ]
                }
            },
        )

        payload = frames_of(sent, MessageType.TOOL)[0]["payload"]
        assert payload["tool"] == "search_knowledge"
        assert payload["tool_call_id"] == "call_abc123"
        assert payload["args"] == {"step": 2, "query": "보스전 진입 조건"}
        # 화면이 로그를 스텝 구간에 나누는 값. 인자에서 읽는다.
        assert payload["step"] == 2
        # Orchestration 라우터는 표시용 message 가 비면 프레임을 통째로 거절한다.
        assert payload["message"] == "search_knowledge"

    asyncio.run(run())


def test_a_tool_result_carries_its_call_as_correlation() -> None:
    """화면이 호출과 답을 한 행으로 묶는 근거. 짝이 어긋나면 남의 결과가 붙는다."""

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
                                {"name": "observe_scene", "args": {"step": 1}, "id": "call_1"}
                            ],
                        )
                    ]
                }
            },
        )
        await runner._log_reasoning(
            channel,
            {
                "tools": {
                    "messages": [
                        ToolMessage(
                            content="scene: Lobby", name="observe_scene", tool_call_id="call_1"
                        )
                    ]
                }
            },
        )

        call = frames_of(sent, MessageType.TOOL)[0]
        result = frames_of(sent, MessageType.TOOL_RESULT)[0]
        assert result["correlationId"] == call["messageId"]
        assert result["payload"]["tool"] == "observe_scene"
        assert result["payload"]["content"] == "scene: Lobby"

    asyncio.run(run())


def test_a_result_whose_call_was_never_seen_still_lands() -> None:
    """컴팩션 뒤에 남은 꼬리가 그렇다. 지어낸 correlation 을 다는 것보다 짝 없이 두는 편이 낫다."""

    async def run() -> None:
        channel, sent = make_channel()
        runner = QaRunner()

        await runner._log_reasoning(
            channel,
            {
                "tools": {
                    "messages": [
                        ToolMessage(content="ok", name="report_step", tool_call_id="orphan")
                    ]
                }
            },
        )

        result = frames_of(sent, MessageType.TOOL_RESULT)[0]
        assert result["correlationId"] is None

    asyncio.run(run())


def test_a_call_missing_its_name_or_id_is_not_logged() -> None:
    """짝지을 수도, 무엇이 불렸는지 말할 수도 없는 줄만 하나 늘린다."""

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
                                {"name": "", "args": {}, "id": "call_1"},
                                {"name": "observe_scene", "args": {}, "id": ""},
                            ],
                        )
                    ]
                }
            },
        )

        assert frames_of(sent, MessageType.TOOL) == []

    asyncio.run(run())


def test_a_long_tool_result_is_clipped() -> None:
    """`observe_scene` 은 씬 렌더를 통째로 돌려준다. 이 프레임은 SSE 로도 발행된다."""

    async def run() -> None:
        channel, sent = make_channel()
        runner = QaRunner()

        await runner._log_reasoning(
            channel,
            {
                "tools": {
                    "messages": [
                        ToolMessage(
                            content="ㅁ" * 50_000, name="observe_scene", tool_call_id="call_1"
                        )
                    ]
                }
            },
        )

        content = frames_of(sent, MessageType.TOOL_RESULT)[0]["payload"]["content"]
        assert len(content) < 50_000
        assert "more characters" in content

    asyncio.run(run())
