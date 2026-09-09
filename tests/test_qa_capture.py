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

from app.agents.qa.arch import ScreenCaptureMode, default_resolved_arch
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
from app.qa.envelope import LogCategory, MessageType

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


# --- what the model is told ---


def test_only_the_every_call_arm_is_told_the_screen_is_already_there() -> None:
    """The description is the model's half of this change, and nothing else pins it.

    `arch_fingerprint` hashes tool names and argument schemas, not descriptions, so
    the two arms would hash apart even if this text never changed.
    """

    def description_for(arch) -> str:
        channel, state, _tools, _sent = make()
        tools = {tool.name: tool for tool in build_tools(channel, state, arch)}
        return tools["capture_screen"].description

    on_demand = description_for(default_resolved_arch())
    every_call = description_for(every_call_arch())

    assert "already in front of you" not in on_demand
    assert "already in front of you" in every_call
    assert every_call.startswith(on_demand)


# --- taking the picture without being asked ---


def every_call_arch(vision: bool = True):
    return default_resolved_arch().model_copy(
        update={"vision": vision, "screen_capture": ScreenCaptureMode.every_call}
    )


def capture_answer(capture_id: str = "auto-1") -> list[dict]:
    return [
        {
            "id": 1,
            "success": True,
            "returnValue": {
                "captureId": capture_id,
                "url": f"https://storage.test/{capture_id}.png",
                "mimeType": "image/png",
            },
        }
    ]


async def one_call(middleware, request_messages):
    """Drive `awrap_model_call` once and return what the model would have been sent."""
    seen: dict = {}

    class Request:
        messages = request_messages

        def override(self, messages):
            seen["messages"] = messages
            return self

    async def handler(request):
        return request

    await middleware.awrap_model_call(Request(), handler)
    return seen.get("messages", request_messages)


def test_every_call_takes_the_picture_itself() -> None:
    """The whole point of the arm: no tool call, and the image is still there."""

    async def run() -> None:
        channel, state, _tools, sent = make()
        middleware = QaCaptureVisionMiddleware(state, channel, every_call_arch())

        answer(channel, sent, capture_answer())
        with storage_answers(httpx.Response(200, content=PNG_BYTES)):
            messages = await one_call(middleware, [HumanMessage(content="plan")])

        dispatched = actions(sent)
        assert len(dispatched) == 1
        assert dispatched[0]["payload"]["actions"][0]["method"] == "capture_screen"
        assert messages[-1].content[1]["type"] == "image_url"

        # The tool's ration is untouched: this capture is not one the model spent.
        assert state.captures_attempted == 0
        # And no note, so a later count can tell this apart from a tool capture.
        assert logs(sent) == []

    asyncio.run(run())


def test_the_picture_never_enters_the_conversation() -> None:
    """The cache boundary reads the stored prompt back, so nothing may edit it.

    A picture stored per turn has to be disposed of on the next one, and every way
    of disposing of it edits a message already sent. Keeping it out of the stored
    messages is what makes the prefix append-only.
    """

    async def run() -> None:
        channel, state, _tools, sent = make()
        middleware = QaCaptureVisionMiddleware(state, channel, every_call_arch())

        answer(channel, sent, capture_answer())
        with storage_answers(httpx.Response(200, content=PNG_BYTES)):
            assert await middleware.abefore_model({}, None) is None
        # `abefore_model` adds nothing, and it did not spend a round trip either.
        assert actions(sent) == []

    asyncio.run(run())


def test_only_one_picture_rides_on_a_request() -> None:
    """`MAX_IMAGES_IN_REQUEST` does not apply — nothing older is ever resent."""

    async def run() -> None:
        channel, state, _tools, sent = make()
        middleware = QaCaptureVisionMiddleware(state, channel, every_call_arch())

        history = [HumanMessage(content="plan")]
        for capture_id in ("auto-1", "auto-2", "auto-3"):
            answer(channel, sent, capture_answer(capture_id))
            with storage_answers(httpx.Response(200, content=PNG_BYTES)):
                messages = await one_call(middleware, history)
            images = [m for m in messages if _has_image(m)]
            assert len(images) == 1
            # The request is the history plus one picture, and the history itself
            # never gained one.
            assert messages[:-1] == history
            assert not any(_has_image(m) for m in history)

    asyncio.run(run())


def test_a_close_up_the_model_asked_for_wins_over_a_new_capture() -> None:
    """It is fresher, already paid for, and is what the model asked to look at."""

    async def run() -> None:
        channel, state, tools, sent = make()

        answer(channel, sent, [capture_result(targetId=42)])
        await tools["capture_screen"].ainvoke(
            {"step": 1, "thought": "버튼이 가려졌는지 본다", "target_id": 42}
        )
        middleware = QaCaptureVisionMiddleware(state, channel, every_call_arch())

        before = len(actions(sent))
        with storage_answers(httpx.Response(200, content=PNG_BYTES)):
            messages = await one_call(middleware, [HumanMessage(content="plan")])

        # No second round trip, and the close-up is what rode along.
        assert len(actions(sent)) == before
        assert "element 42" in messages[-1].content[0]["text"]

    asyncio.run(run())


def test_the_automatic_capture_carries_no_step_so_it_can_be_counted_apart() -> None:
    """`step` is what separates the two in `qa_log`, not the timeline note.

    A tool capture that fails writes no note, so subtracting notes overcounts the
    automatic ones. The tool always sends a `step`; this dispatch never does.
    """

    async def run() -> None:
        channel, state, _tools, sent = make()
        middleware = QaCaptureVisionMiddleware(state, channel, every_call_arch())

        answer(channel, sent, capture_answer())
        with storage_answers(httpx.Response(200, content=PNG_BYTES)):
            await one_call(middleware, [HumanMessage(content="plan")])

        assert actions(sent)[0]["payload"]["step"] is None

    asyncio.run(run())


def test_a_game_that_does_not_answer_leaves_the_turn_without_a_picture() -> None:
    """Never fatal. The turn goes on reading the screen text.

    The timeout is injected rather than left at `AUTO_CAPTURE_TIMEOUT_SECONDS`: the
    automatic capture passes its own value to `dispatch_actions`, which overrides
    the channel's, so the harness's 50 ms would never be consulted and this one
    test would take ten seconds.
    """

    async def run() -> None:
        channel, state, _tools, sent = make()
        middleware = QaCaptureVisionMiddleware(
            state, channel, every_call_arch(), auto_capture_timeout=0.05
        )

        history = [HumanMessage(content="plan")]
        messages = await one_call(middleware, history)

        assert messages == history
        assert len(actions(sent)) == 1

    asyncio.run(run())


def test_a_refused_capture_leaves_the_turn_without_a_picture() -> None:
    async def run() -> None:
        channel, state, _tools, sent = make()
        middleware = QaCaptureVisionMiddleware(state, channel, every_call_arch())

        history = [HumanMessage(content="plan")]
        answer(channel, sent, [{"id": 1, "success": False, "error": "Unsupported method"}])
        assert await one_call(middleware, history) == history

    asyncio.run(run())


def test_on_demand_never_takes_a_picture_by_itself() -> None:
    async def run() -> None:
        channel, state, _tools, sent = make()
        middleware = QaCaptureVisionMiddleware(state, channel, default_resolved_arch())

        assert await middleware.abefore_model({}, None) is None
        assert actions(sent) == []

    asyncio.run(run())


def _has_image(message) -> bool:
    content = getattr(message, "content", None)
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "image_url" for b in content
    )


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
