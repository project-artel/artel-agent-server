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
# deadline with nothing reported.
MAX_CAPTURES_PER_RUN = 12

DOWNLOAD_TIMEOUT_SECONDS = 15.0

# Marks the messages this module owns, so the trimming pass can find them without
# guessing from content shape.
CAPTURE_MESSAGE_KEY = "artel_capture_id"

_ALLOWED_MIME_TYPES = frozenset({"image/jpeg", "image/png"})


class CaptureFetchError(Exception):
    """The image could not be fetched. The run continues without it."""


async def fetch_capture(url: str, mime_type: str) -> str:
    """The image as a base64 data payload.

    Fetched here rather than handed to the provider as a URL: the link expires,
    storage may not be publicly readable, and a failure on our side is one we can
    explain to the agent instead of one that surfaces as a provider error.
    """
    if mime_type not in _ALLOWED_MIME_TYPES:
        raise CaptureFetchError(f"unsupported capture type {mime_type!r}")

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

    return base64.b64encode(response.content).decode("ascii")


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
    """

    def __init__(self, state, max_images: int = MAX_IMAGES_IN_REQUEST) -> None:
        super().__init__()
        self._state = state
        self._max_images = max_images

    async def abefore_model(self, state, runtime) -> dict | None:
        pending = self._state.take_pending_captures()
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
