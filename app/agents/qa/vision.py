"""Getting a captured screen in front of the model.

Three separate problems, kept apart because they fail differently:

1. Fetching. The image lives in storage behind a short-lived URL — the bytes never
   cross the QA WebSocket, because everything relayed there is written to the QA log
   and republished over SSE.
2. Placement. Images cannot ride on a tool result, so they arrive as their own turn.
3. Cost. Every image already in the transcript would otherwise be resent on every
   later request.
"""

import base64
import logging

import httpx
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

# Images older than this are replaced with a line of text saying one was there.
# Without a cap the transcript resends every screenshot on every turn, so the cost
# of a run grows with the square of the number of captures.
MAX_IMAGES_IN_REQUEST = 2

# A run that keeps capturing instead of deciding is a run that will hit the
# deadline with nothing reported. The number lives in `app/agents/qa/arch.py`
# with the run's other structural knobs — a run can be asked to use a different
# one, and the arch fingerprint has to see it — and is re-exported here under the
# name its callers and tests already use.
from app.agents.qa.arch import MAX_CAPTURES_PER_RUN  # noqa: E402 - re-export

# Down here with it because the constant above forces the whole block below the
# module's own definitions; these are ordinary imports, not re-exports.
from app.agents.qa.arch import ScreenCaptureMode  # noqa: E402
from app.agents.qa.tools.state import (  # noqa: E402
    PendingCapture,
    capture_from_action_result,
)
from app.qa.envelope import JsonRpcAction  # noqa: E402

DOWNLOAD_TIMEOUT_SECONDS = 15.0

# How long an automatic capture waits for the game, in `every_call` runs only.
#
# Well under `ACTION_TIMEOUT_SECONDS` (30.0), which is what a tool call may wait.
# A tool capture happens when the model decides to look; an automatic one happens
# on every model call, so 30 seconds of silence per call would make a slow game
# indistinguishable from a wedged run.
#
# Not shorter than this either. One normal capture is `WaitForEndOfFrame`, an
# encode, a presign POST and an S3 PUT, and three game slots sharing a machine
# still finish inside ten seconds. Cutting it further would start timing out
# captures that were about to arrive, and a capture that times out and lands late
# is the input to the `correlationId` fallback in `QaRunChannel._action_waiter_for`
# — the one place a late answer can be read as the next action's.
AUTO_CAPTURE_TIMEOUT_SECONDS = 10.0

# Marks the messages this module owns, so the trimming pass can find them without
# guessing from content shape.
CAPTURE_MESSAGE_KEY = "artel_capture_id"

_ALLOWED_MIME_TYPES = frozenset({"image/jpeg", "image/png"})

# 파일이 스스로 말하는 자기 타입. 캡처를 실어 오는 프레임이 전부 mime 을 말해 주지는
# 않는다 — `SCREEN_SELECTOR_PROPOSAL` 은 주소만 싣는다(ARTEL-655) — 그때 확장자나 기본값을
# 믿는 대신 바이트를 본다. 데이터 URI 에 틀린 타입을 적으면 provider 가 그림을 통째로
# 거절하고, 그 거절은 "이미지가 없다" 가 아니라 호출 실패로 온다.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


class CaptureFetchError(Exception):
    """The image could not be fetched. The run continues without it."""


async def download_capture(url: str) -> bytes:
    """The image's bytes.

    Fetched here rather than handed to the provider as a URL: the link expires,
    storage may not be publicly readable, and a failure on our side is one we can
    explain to the agent instead of one that surfaces as a provider error.
    """
    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPStatusError as error:
        # An expired URL is the likely one, and it reads as an ordinary 403 from
        # storage rather than anything that names expiry.
        raise CaptureFetchError(
            f"storage answered {error.response.status_code} — the link may have expired"
        ) from error
    except httpx.HTTPError as error:
        raise CaptureFetchError(f"could not reach storage: {error}") from error

    return response.content


def image_mime_of(raw: bytes) -> str:
    """바이트가 말하는 이미지 타입. JPEG 도 PNG 도 아니면 거절한다."""
    for magic, mime_type in _IMAGE_MAGIC:
        if raw.startswith(magic):
            return mime_type
    raise CaptureFetchError("the bytes are not a JPEG or a PNG")


async def fetch_capture(url: str, mime_type: str) -> str:
    """The image as a base64 data payload, for a caller that was told its type."""
    if mime_type not in _ALLOWED_MIME_TYPES:
        raise CaptureFetchError(f"unsupported capture type {mime_type!r}")
    return base64.b64encode(await download_capture(url)).decode("ascii")


def build_capture_message(capture_id: str, encoded: str, mime_type: str, caption: str):
    """The image as its own turn, tagged so it can be found again later."""
    return HumanMessage(
        content=[
            {"type": "text", "text": caption},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
            },
        ],
        additional_kwargs={CAPTURE_MESSAGE_KEY: capture_id},
    )


def _is_capture_message(message) -> bool:
    return bool((getattr(message, "additional_kwargs", None) or {}).get(CAPTURE_MESSAGE_KEY))


def _without_image(message):
    """The same turn with the picture replaced by a note that there was one."""
    caption = next(
        (
            block.get("text", "")
            for block in message.content
            if isinstance(block, dict) and block.get("type") == "text"
        ),
        "",
    )
    return HumanMessage(
        content=f"{caption} (image dropped from this request to save cost — capture it again if you need another look)",
        additional_kwargs=dict(getattr(message, "additional_kwargs", None) or {}),
    )


def trim_images(messages: list, keep: int = MAX_IMAGES_IN_REQUEST) -> list:
    """Keep the pictures on the last `keep` capture turns; text-only for the rest.

    The agent judges against what it is looking at now. Older screenshots have
    already been reasoned about, and their conclusions are in the transcript.
    """
    capture_positions = [index for index, message in enumerate(messages) if _is_capture_message(message)]
    if len(capture_positions) <= keep:
        return messages

    stale = set(capture_positions[:-keep]) if keep > 0 else set(capture_positions)
    return [
        _without_image(message) if index in stale else message
        for index, message in enumerate(messages)
    ]


class QaCaptureVisionMiddleware(AgentMiddleware):
    """Puts pending captures in front of the model, and keeps the bill bounded.

    Images are injected as their own `HumanMessage` rather than as the capture
    tool's result. OpenAI's chat/completions API rejects image blocks on a `tool`
    role message, and every model here reaches its provider through that API —
    `build_chat_model` points `ChatOpenAI` at OpenRouter for the whole catalog,
    Anthropic slugs included. Injecting from `before_model` also puts the image
    after all of that turn's tool results, which is the ordering Anthropic
    requires.

    In an `every_call` run this hook also *takes* the capture, rather than only
    placing one the model asked for. That is safe to do here for a reason worth
    writing down: LangChain gives every middleware overriding `before_model` its
    own graph node, and those nodes are chained ahead of the single `model` node
    (`langchain/agents/factory.py`). `QaCompactionMiddleware` calls its summarizer
    inside its own hook rather than through the graph, so this hook never wraps
    that call. One automatic capture per turn of the QA agent's own model, and
    none around the summarizer.
    """

    def __init__(
        self,
        state,
        channel=None,
        arch=None,
        max_images: int = MAX_IMAGES_IN_REQUEST,
    ) -> None:
        super().__init__()
        self._state = state
        self._channel = channel
        self._arch = arch
        self._max_images = max_images

    def _captures_every_call(self) -> bool:
        return (
            self._channel is not None
            and getattr(self._arch, "screen_capture", None) is ScreenCaptureMode.every_call
        )

    async def _auto_capture(self) -> PendingCapture | None:
        """Ask the game for the screen, for a run that reads it on every call.

        Never raises and never ends the run. Every way this can fail — the game is
        busy, the SDK does not know the action, the answer is late — leaves the
        turn with no new picture, and `trim_images` still has the last two, so the
        model is looking at a screen one turn old rather than at nothing.

        `state.captures_attempted` is deliberately not touched. That counter is the
        model's tool ration and `max_captures_per_run` builds the tool's refusal
        message out of it; spending it here would ration a budget the model never
        asked to spend.

        No `channel.note` either. One line per model call would bury the timeline a
        person reads, and the ACTION row Orchestration writes for this dispatch is
        already the durable record — a note here is also what would make an
        automatic capture indistinguishable from a tool one when they are counted
        afterwards.
        """
        result = await self._channel.dispatch_actions(
            [JsonRpcAction(id=1, method="capture_screen", params=[])],
            "Capturing the screen",
            timeout=AUTO_CAPTURE_TIMEOUT_SECONDS,
        )
        if result is None or not result.results:
            logger.warning("[QA] the game did not answer the automatic capture")
            return None

        capture = capture_from_action_result(result.results[0], "the screen")
        if capture is None:
            item = result.results[0]
            logger.warning(
                "[QA] automatic capture produced no image: %s",
                item.error or "no reason given",
            )
        return capture

    async def abefore_model(self, state, runtime) -> dict | None:
        # Drained first, and the automatic capture only fills a queue this leaves
        # empty. Anything pending here was queued by `capture_screen` during the
        # turn that just ended, because the previous call drained everything older.
        # Capturing anyway would put two near-identical screens in one request —
        # filling both of `MAX_IMAGES_IN_REQUEST`'s slots with the same moment — and
        # spend a second round trip to the game for it.
        pending = self._state.take_pending_captures()
        if not pending and self._captures_every_call():
            captured = await self._auto_capture()
            pending = [captured] if captured is not None else []
        if not pending:
            return None

        messages = []
        for capture in pending:
            try:
                encoded = await fetch_capture(capture.url, capture.mime_type)
            except CaptureFetchError as error:
                # Not fatal. The tool result already told the agent a capture was
                # taken, so say plainly that the picture is not coming and let it
                # decide whether to look again or judge from the scene text.
                logger.warning("[QA] capture %s could not be fetched: %s", capture.capture_id, error)
                messages.append(
                    HumanMessage(
                        content=(
                            f"The screenshot you asked for could not be loaded ({error}). "
                            "Capture again, or judge from the scene text."
                        )
                    )
                )
                continue

            messages.append(
                build_capture_message(
                    capture.capture_id, encoded, capture.mime_type, capture.caption
                )
            )

        return {"messages": messages} if messages else None

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(messages=trim_images(request.messages, self._max_images)))
