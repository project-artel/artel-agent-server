"""Looking at the screen: the tool, the placement, and the cost.

The failure this guards against is not an exception — it is a run that quietly
never sees a picture, or one whose bill grows with the square of the number of
screenshots because every old image is resent on every turn.
"""

import asyncio
import base64
from contextlib import contextmanager

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.qa.arch import default_resolved_arch
from app.agents.qa.tools import PendingCapture, QaRunState, build_tools
from app.agents.qa.vision import (
    MAX_CAPTURES_PER_RUN,
    CaptureFetchError,
    QaCaptureVisionMiddleware,
    build_capture_message,
    fetch_capture,
    trim_images,
)
from app.llm.models import LLMModel, get_model_spec
from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory, MessageType, ScreenCreatedPayload

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake image body"


@contextmanager
def storage_answers(response: httpx.Response):
    """Answer the image fetch without a network.

    Swapped at the client rather than at `fetch_capture` so the real request,
    status handling and error mapping are the code under test.
    """
    transport = httpx.MockTransport(lambda request: response)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda **kwargs: original(transport=transport, **kwargs)
    try:
        yield
    finally:
        httpx.AsyncClient = original


def make(total_steps: int = 1, timeout: float = 0.05, supports_vision: bool = True):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=timeout, write_timeout=timeout)
    state = QaRunState(total_steps=total_steps)
    # Vision is a property of the resolved structure now, not of the model alone:
    # `arch.vision` is what `build_tools` reads, and what the arch fingerprint sees.
    arch = default_resolved_arch().model_copy(update={"vision": supports_vision})
    tools = {tool.name: tool for tool in build_tools(channel, state, arch)}
    return channel, state, tools, sent


def actions(sent: list[dict]) -> list[dict]:
    return [frame for frame in sent if frame["type"] == MessageType.ACTION.value]


def logs(sent: list[dict]) -> list[dict]:
    return [frame for frame in sent if frame["type"] == MessageType.LOG.value]


def answer(channel: QaRunChannel, sent: list[dict], results: list[dict]):
    """Reply the way the game does, quoting the ACTION this answers."""
    already = len(actions(sent))

    async def reply() -> None:
        for _ in range(50):
            if len(actions(sent)) > already:
                break
            await asyncio.sleep(0)
        channel.on_action_result(
            {
                "correlationId": actions(sent)[-1]["messageId"],
                "payload": {"results": results},
            }
        )

    return asyncio.create_task(reply())


def capture_result(**overrides) -> dict:
    return {
        "id": 1,
        "success": True,
        "returnValue": {
            "captureId": "capture-1",
            "url": "https://storage.test/qa-captures/7/capture-1.jpg",
            "expiresAt": "2026-07-28T14:00:00Z",
            "mimeType": "image/jpeg",
            "width": 1024,
            "height": 576,
            "clipped": False,
            **overrides,
        },
    }


# --- the tool ---


def test_capturing_sends_capture_screen_and_queues_the_image() -> None:
    async def run() -> None:
        channel, state, tools, sent = make()

        answer(channel, sent, [capture_result()])
        result = await tools["capture_screen"].ainvoke(
            {"step": 1, "thought": "레이아웃이 깨졌는지 눈으로 본다"}
        )

        assert actions(sent)[0]["payload"]["actions"] == [
            {"id": 1, "jsonrpc": "2.0", "method": "capture_screen", "params": []}
        ]
        assert "image follows" in result

        pending = state.take_pending_captures()
        assert len(pending) == 1
        assert pending[0].url.endswith("capture-1.jpg")
        assert pending[0].mime_type == "image/jpeg"

    asyncio.run(run())


def test_capturing_an_element_passes_its_id_through() -> None:
    async def run() -> None:
        channel, state, tools, sent = make()

        answer(channel, sent, [capture_result(targetId=42)])
        await tools["capture_screen"].ainvoke(
            {"step": 1, "thought": "버튼이 가려졌는지 본다", "target_id": 42}
        )

        assert actions(sent)[0]["payload"]["actions"][0]["params"] == [42]
        assert "element 42" in state.take_pending_captures()[0].caption

    asyncio.run(run())


def test_a_clipped_capture_says_so_in_the_caption() -> None:
    """Otherwise the agent reads a partial image as the whole element."""

    async def run() -> None:
        channel, state, tools, sent = make()

        answer(channel, sent, [capture_result(clipped=True, targetId=42)])
        await tools["capture_screen"].ainvoke(
            {"step": 1, "thought": "버튼을 본다", "target_id": 42}
        )

        assert "off the edge" in state.take_pending_captures()[0].caption

    asyncio.run(run())


def test_capturing_puts_the_url_on_the_timeline() -> None:
    """So a reviewer can open exactly what the agent looked at."""

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, [capture_result()])
        await tools["capture_screen"].ainvoke({"step": 3, "thought": "화면을 본다"})

        observations = [
            frame
            for frame in logs(sent)
            if frame["payload"]["category"] == LogCategory.OBSERVATION.value
        ]
        assert len(observations) == 1
        assert "capture-1.jpg" in observations[0]["payload"]["message"]
        assert observations[0]["payload"]["step"] == 3

    asyncio.run(run())


def test_a_refused_capture_keeps_the_run_going() -> None:
    """The reason AND what to do instead.

    A game whose SDK predates this action answers "Unsupported method" to every
    capture. Told only that, the agent failed the step and then the whole run —
    over a screenshot it could have done without. Seen in a real run.
    """

    async def run() -> None:
        channel, state, tools, sent = make()

        answer(
            channel,
            sent,
            [{"id": 1, "success": False, "error": "Unsupported method: capture_screen"}],
        )
        result = await tools["capture_screen"].ainvoke({"step": 1, "thought": "화면을 본다"})

        assert "Unsupported method" in result
        assert "scene text" in result
        assert state.take_pending_captures() == []

    asyncio.run(run())


def test_a_failed_capture_still_spends_the_run_budget() -> None:
    """Otherwise a game that refuses every capture never reaches the cap."""

    async def run() -> None:
        channel, state, tools, sent = make()

        for _ in range(2):
            answer(channel, sent, [{"id": 1, "success": False, "error": "Unsupported method"}])
            await tools["capture_screen"].ainvoke({"step": 1, "thought": "화면을 본다"})

        assert state.captures_attempted == 2

    asyncio.run(run())


def test_a_result_without_a_return_value_keeps_the_run_going() -> None:
    """An older SDK answers success with nothing to read."""

    async def run() -> None:
        channel, state, tools, sent = make()

        answer(channel, sent, [{"id": 1, "success": True}])
        result = await tools["capture_screen"].ainvoke({"step": 1, "thought": "화면을 본다"})

        assert "no image to read" in result
        assert state.take_pending_captures() == []

    asyncio.run(run())


def test_a_silent_game_keeps_the_run_going() -> None:
    async def run() -> None:
        _, state, tools, _ = make()

        result = await tools["capture_screen"].ainvoke({"step": 1, "thought": "화면을 본다"})

        assert "did not answer" in result
        assert state.take_pending_captures() == []

    asyncio.run(run())


def test_the_per_run_cap_is_refused_with_its_reason() -> None:
    """A run that keeps looking instead of deciding reaches the deadline empty."""

    async def run() -> None:
        channel, state, tools, sent = make()
        state.captures_attempted = MAX_CAPTURES_PER_RUN

        result = await tools["capture_screen"].ainvoke({"step": 1, "thought": "화면을 본다"})

        assert str(MAX_CAPTURES_PER_RUN) in result
        assert "scene text" in result
        # Refused before the game was ever asked.
        assert actions(sent) == []

    asyncio.run(run())


def test_a_run_that_cannot_see_is_not_offered_the_tool() -> None:
    _, _, tools, _ = make(supports_vision=False)

    assert "capture_screen" not in tools
    assert "observe_scene" in tools


def test_every_catalogued_model_is_marked_as_seeing() -> None:
    """Matches `architecture.input_modalities` in the OpenRouter catalog.

    Gemma 4 was carried as text-only on the strength of a ticket description; the
    catalog says `image,text,video`, and the wrong flag silently took the capture
    tool away from it. The flags are claims about a live catalog, so they are
    written down here rather than left to whoever adds the next model.
    """
    for model in LLMModel:
        assert get_model_spec(model).supports_vision is True, model


# --- the new-screen capture (ARTEL-595) ---
#
# 도구가 아니라 pulse 를 다루는 쪽이 낸다. orchestration 이 처음 보는 screen 을 만들었다고
# 알려 오면(`SCREEN_CREATED`) 그 자리에서 한 번 찍고, 주소를 `SCREEN_CAPTURE` 로 돌려준다.

NEW_SCREEN = ScreenCreatedPayload(screenId="12", sceneName="TitleScene")


def screen_created(message_id: str = "screen-frame-1", **payload) -> dict:
    return {
        "type": MessageType.SCREEN_CREATED.value,
        "messageId": message_id,
        "payload": {"screenId": "12", "sceneName": "TitleScene", **payload},
    }


def screen_captures(sent: list[dict]) -> list[dict]:
    return [frame for frame in sent if frame["type"] == MessageType.SCREEN_CAPTURE.value]


async def capture_a_new_screen(channel, sent, results) -> None:
    """Let the game answer the capture that a new screen triggers."""
    answer(channel, sent, results)
    await channel.capture_new_screen(NEW_SCREEN, "screen-frame-1")


def test_a_new_screen_is_captured_and_the_url_rides_back() -> None:
    """The whole point: the screen row gets a picture of itself as it was born."""

    async def run() -> None:
        channel, _, _, sent = make()

        await capture_a_new_screen(channel, sent, [capture_result()])

        assert actions(sent)[0]["payload"]["actions"] == [
            {"id": 1, "jsonrpc": "2.0", "method": "capture_screen", "params": []}
        ]
        [frame] = screen_captures(sent)
        # Correlated to the frame that asked, so Orchestration can bind it even if
        # the payload's own screenId were to go missing on the way.
        assert frame["correlationId"] == "screen-frame-1"
        assert frame["payload"]["screenId"] == "12"
        assert frame["payload"]["captureId"] == "capture-1"
        assert frame["payload"]["url"].endswith("capture-1.jpg")
        # Orchestration's router drops a frame whose payload.message is blank.
        assert frame["payload"]["message"]

    asyncio.run(run())


def test_the_new_screen_capture_does_not_spend_the_agent_budget() -> None:
    """A tool call would make the map's pictures depend on the run's mood."""

    async def run() -> None:
        channel, state, tools, sent = make()

        await capture_a_new_screen(channel, sent, [capture_result()])

        assert screen_captures(sent)
        assert state.captures_attempted == 0
        # And the image is not pushed at the model: this is the map's picture, not
        # evidence the agent asked to see.
        assert state.take_pending_captures() == []
        assert "capture_screen" in tools

    asyncio.run(run())


def test_a_refused_new_screen_capture_leaves_the_screen_alone() -> None:
    """A screen row without a picture beats a map without the screen."""

    async def run() -> None:
        channel, _, _, sent = make()

        await capture_a_new_screen(
            channel,
            sent,
            [{"id": 1, "success": False, "error": "Unsupported method: capture_screen"}],
        )

        assert screen_captures(sent) == []
        # Said on the timeline rather than swallowed, so the missing picture has a
        # reason someone can read.
        assert any(
            "could not be captured" in frame["payload"]["message"] for frame in logs(sent)
        )

    asyncio.run(run())


def test_a_game_without_the_action_is_only_asked_once() -> None:
    """Otherwise every new screen buys the same refusal for the rest of the run."""

    async def run() -> None:
        channel, _, _, sent = make()

        await capture_a_new_screen(
            channel, sent, [{"id": 1, "success": False, "error": "Unsupported method"}]
        )
        # 두 번째 screen 에는 답을 준비하지 않는다. 물으러 가지 않는 것이 이 테스트의 주장이다.
        await channel.capture_new_screen(NEW_SCREEN, "screen-frame-2")
        channel.on_screen_created(screen_created(message_id="screen-frame-3"))

        assert len(actions(sent)) == 1
        assert screen_captures(sent) == []
        assert channel._capture_tasks == set()

    asyncio.run(run())


def test_a_silent_game_does_not_end_the_run() -> None:
    async def run() -> None:
        channel, _, _, sent = make()

        await channel.capture_new_screen(NEW_SCREEN, "screen-frame-1")

        assert screen_captures(sent) == []
        assert any("did not answer" in frame["payload"]["message"] for frame in logs(sent))

    asyncio.run(run())


def test_a_capture_with_no_id_to_bind_is_not_reported() -> None:
    """Half a binding is worse than none: Orchestration would store an image it
    cannot match back to an object."""

    async def run() -> None:
        channel, _, _, sent = make()

        await capture_a_new_screen(channel, sent, [capture_result(captureId=None)])

        assert screen_captures(sent) == []

    asyncio.run(run())


def test_the_screen_frame_does_not_hold_up_the_socket() -> None:
    """`on_screen_created` 는 소켓을 읽는 쪽의 동기 콜백이다.

    capture 왕복을 거기서 기다리면 그동안 pulse 도 action 결과도 채널에 못 들어온다. 곧바로
    돌려주고 백그라운드에서 찍는다.
    """

    async def run() -> None:
        channel, _, _, sent = make()

        answer(channel, sent, [capture_result()])
        channel.on_screen_created(screen_created())
        # 돌아온 시점에는 아직 아무것도 안 나갔다.
        assert sent == []

        for task in list(channel._capture_tasks):
            await task
        assert len(screen_captures(sent)) == 1

    asyncio.run(run())


def test_closing_the_channel_stops_a_capture_in_flight() -> None:
    """A frame sent for a finished try is rejected, and that rejection kills the
    socket — which fails the whole run."""

    async def run() -> None:
        channel, _, _, sent = make()

        channel.on_screen_created(screen_created())
        await asyncio.sleep(0)
        channel.close()
        await asyncio.sleep(0)

        assert screen_captures(sent) == []

    asyncio.run(run())


# --- fetching ---


def test_fetching_returns_the_image_as_base64() -> None:
    async def run() -> None:
        with storage_answers(httpx.Response(200, content=PNG_BYTES)):
            encoded = await fetch_capture("https://storage.test/a.png", "image/png")

        assert base64.b64decode(encoded) == PNG_BYTES

    asyncio.run(run())


def test_an_expired_link_is_reported_as_such() -> None:
    async def run() -> None:
        with storage_answers(httpx.Response(403)):
            with pytest.raises(CaptureFetchError) as error:
                await fetch_capture("https://storage.test/a.png", "image/png")

        assert "403" in str(error.value)
        assert "expired" in str(error.value)

    asyncio.run(run())


def test_a_type_that_is_not_an_image_is_refused_before_the_request() -> None:
    async def run() -> None:
        with pytest.raises(CaptureFetchError):
            await fetch_capture("https://storage.test/a.pdf", "application/pdf")

    asyncio.run(run())


# --- placement ---


def test_the_image_arrives_as_its_own_human_turn_after_the_tool_results() -> None:
    """Not as the tool's result.

    OpenAI's chat/completions API rejects image blocks on a `tool` role message,
    and every model here reaches its provider through that API. Injecting from
    `before_model` also puts the image after that turn's ToolMessages, which is
    the ordering Anthropic requires.
    """

    async def run() -> None:
        state = QaRunState(total_steps=1)
        state.add_pending_capture(
            PendingCapture(
                capture_id="capture-1",
                url="https://storage.test/a.png",
                mime_type="image/png",
                caption="This is the screen right now.",
            )
        )

        with storage_answers(httpx.Response(200, content=PNG_BYTES)):
            update = await QaCaptureVisionMiddleware(state).abefore_model({}, None)

        message = update["messages"][0]
        assert isinstance(message, HumanMessage)
        assert message.content[0]["type"] == "text"
        assert message.content[1]["type"] == "image_url"
        assert message.content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    asyncio.run(run())


def test_nothing_is_injected_when_nothing_was_captured() -> None:
    async def run() -> None:
        middleware = QaCaptureVisionMiddleware(QaRunState(total_steps=1))
        assert await middleware.abefore_model({}, None) is None

    asyncio.run(run())


def test_an_unreachable_image_becomes_a_line_the_agent_can_act_on() -> None:
    async def run() -> None:
        state = QaRunState(total_steps=1)
        state.add_pending_capture(
            PendingCapture(
                capture_id="capture-1",
                url="https://storage.test/a.png",
                mime_type="image/png",
                caption="This is the screen right now.",
            )
        )

        with storage_answers(httpx.Response(403)):
            update = await QaCaptureVisionMiddleware(state).abefore_model({}, None)

        # The run continues; the reason reaches the model rather than the logs only.
        assert "could not be loaded" in update["messages"][0].content

    asyncio.run(run())


# --- cost ---


def image_message(capture_id: str):
    return build_capture_message(capture_id, "AAAA", "image/png", f"capture {capture_id}")


def test_only_the_most_recent_images_keep_their_pictures() -> None:
    messages = [
        HumanMessage(content="start"),
        image_message("a"),
        AIMessage(content="looked"),
        image_message("b"),
        image_message("c"),
    ]

    trimmed = trim_images(messages, keep=2)

    # The oldest keeps its place in the transcript but loses the payload, so the
    # request stops growing with every screenshot ever taken.
    assert isinstance(trimmed[1].content, str)
    assert "image dropped" in trimmed[1].content
    assert trimmed[3].content[1]["type"] == "image_url"
    assert trimmed[4].content[1]["type"] == "image_url"


def test_trimming_leaves_a_transcript_under_the_cap_alone() -> None:
    messages = [HumanMessage(content="start"), image_message("a")]

    assert trim_images(messages, keep=2) == messages


def test_trimming_does_not_touch_ordinary_messages() -> None:
    messages = [
        HumanMessage(content="start"),
        ToolMessage(content="scene", tool_call_id="1"),
        image_message("a"),
        image_message("b"),
        image_message("c"),
    ]

    trimmed = trim_images(messages, keep=1)

    assert trimmed[0] is messages[0]
    assert trimmed[1] is messages[1]
    assert trimmed[4].content[1]["type"] == "image_url"
