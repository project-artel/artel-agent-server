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
from dataclasses import dataclass

import httpx
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from app.agents.qa.arch import ScreenCaptureMode
from app.agents.qa.tools.state import PendingCapture, capture_from_action_result
from app.qa.envelope import JsonRpcAction

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
#
# The waiting this arm adds to a model call is 25 seconds, not 10: the fetch that
# follows a successful capture is bounded separately by `DOWNLOAD_TIMEOUT_SECONDS`
# (15.0). Under `on_demand` that 15 was paid only on the turns the model asked to
# look; here it is on every turn.
AUTO_CAPTURE_TIMEOUT_SECONDS = 10.0

# `by_role` 이 실패 화면을 몇 turn 들고 있나.
#
# 실패는 그 turn 에 판정되지 않는 일이 많다 — agent 는 다시 눌러 보고, 다른 길을 찾아보고,
# 그러고 나서 실패로 적는다. 그 사이 화면이 사라지면 판정할 때 근거가 없다. 반대로 오래
# 들고 있으면 지나간 실패가 계속 넉 장을 채운다. 여섯은 그 사이에서 고른 값이고, 파일럿이
# 다른 수를 말하면 바꾼다.
FAILURE_CAPTURE_TURNS = 6

# Marks the messages this module owns, so the trimming pass can find them without
# guessing from content shape.
CAPTURE_MESSAGE_KEY = "artel_capture_id"

# Marks a message that rides on one request and is never stored, so the cache
# boundary can be written before it rather than after
# (`_CachingChatBedrockConverse._with_cache_point`). A boundary written after it
# would put a picture that changes every turn inside the cached prefix, which is
# the same lost read this arm exists to avoid.
TRANSIENT_MESSAGE_KEY = "artel_transient"

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


@dataclass(frozen=True)
class HeldCapture:
    """한 번 내려받아 둔 그림. 역할이 바뀌어도 다시 받지 않는다.

    URL 이 아니라 인코딩된 바이트를 들고 있는 이유가 둘이다. 같은 그림이 여러 turn 실리므로
    turn 마다 다시 받으면 그 왕복이 turn 수만큼 늘고, 다운로드 주소는 30분짜리라 긴 런의
    후반에는 만료된다.
    """

    capture_id: str
    mime_type: str
    encoded: str
    # capture 가 스스로 말하는 것. `target_id` 로 잘라 온 그림이면 어느 요소인지, 잘려
    # 나갔으면 그 사실이 여기 있다. 역할 caption 이 그것을 덮으면 모델은 자기가 요청한
    # close-up 을 전체 화면으로 읽는다.
    caption: str


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
        auto_capture_timeout: float = AUTO_CAPTURE_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__()
        self._state = state
        self._channel = channel
        self._arch = arch
        self._max_images = max_images
        # Injectable for the same reason `QaRunChannel.action_timeout` is: a test
        # that waited out the real one would pay ten seconds to prove a timeout.
        self._auto_capture_timeout = auto_capture_timeout
        # 역할 → 그 역할이 지금 들고 있는 그림. `by_role` 에서만 채워진다.
        self._held: dict[str, HeldCapture] = {}
        # 역할을 옮길 때가 됐는지 판단하는 근거. 지난 turn 에 본 값이다.
        self._last_action_frame = None
        self._last_scene = None
        self._last_failures = 0
        self._failure_age = 0

    def _captures_by_role(self) -> bool:
        return getattr(self._arch, "screen_capture", None) is ScreenCaptureMode.by_role

    def _captures_every_call(self) -> bool:
        """Read from the arch alone, so a missing channel raises instead of hiding.

        Keying this on `self._channel is not None` as well would turn a wiring
        mistake into a run recorded as `every_call` that never captures — the
        wrong-bucket failure `resolve_arch` refuses `every_call` without vision to
        prevent, put back one layer down. `_auto_capture` is where the `None`
        shows up, and an `AttributeError` there is the right kind of loud.
        """
        mode = getattr(self._arch, "screen_capture", None)
        return mode in (ScreenCaptureMode.every_call, ScreenCaptureMode.by_role)

    async def _auto_capture(self) -> PendingCapture | None:
        """Ask the game for the screen, for a run that reads it on every call.

        Returns `None` for every failure the game can produce — it is busy, its SDK
        does not know the action, the answer is late — and the turn then goes on
        with no new picture. `trim_images` still holds the last two, so the model
        is looking at a screen one turn old rather than at nothing.

        `QaCancelled` from `dispatch_actions` is deliberately NOT caught. The
        operator ending the run has to stop the run, and every tool re-raises it
        for that reason; catching it here would swallow a cancel on every model
        call.

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
            timeout=self._auto_capture_timeout,
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
        """Put the captures the model asked for into the conversation.

        `on_demand` only. In `every_call` the picture never enters the stored
        conversation at all — `awrap_model_call` below puts it in the request and
        nowhere else, and the reason is the prompt cache. Draining here would leave
        this hook competing with that one for the same queue.
        """
        if self._captures_every_call():
            return None
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
        """Trim images for `on_demand`; hand the automatic modes their own, only here.

        ## Why the picture does not go into the conversation

        The cache boundary is written at the end of the prompt
        (`_CachingChatBedrockConverse._with_cache_point`), so the next turn reads
        this whole prompt back — but only while every byte of it is the same again.

        Storing a picture per turn cannot keep that promise, whichever way the old
        ones are disposed of. `trim_images` rewrites them into text and a deletion
        would remove them outright; both edit a message that has already been sent,
        so the prefix diverges and the read is lost. Measured on a real run: the
        `on_demand` arm read 97.3% of its input from cache and the `every_call` arm
        1.3%, and the bill went from $0.87 to $14.22 while input tokens rose only
        1.25x (ARTEL-868).

        So the conversation stays text and append-only, and the pictures are added
        to this one request and thrown away. `by_role` makes that matter more, not
        less: its roles move every turn — this turn's `current` is the next turn's
        `pre_action` — and a stored transcript would have to be rewritten to follow
        them. Nothing is stored, so nothing is rewritten.

        `MAX_IMAGES_IN_REQUEST` does not apply to either automatic mode. It bounds
        how many pictures a *stored* transcript resends, and these store none.
        """
        if not self._captures_every_call():
            return await handler(
                request.override(messages=trim_images(request.messages, self._max_images))
            )

        messages = await self._screen_messages()
        return await handler(
            request.override(messages=[*request.messages, *messages] if messages else request.messages)
        )

    async def _screen_messages(self) -> list:
        """The pictures this request carries, oldest role first.

        `every_call` carries one. `by_role` carries two to four — see
        `_shift_roles` for what decides which.
        """
        current = await self._current_screen()
        if not self._captures_by_role():
            if current is None:
                return []
            return [self._message_for(current.caption, current)]

        self._shift_roles(current)
        # 오래된 것부터 싣고 `current` 를 맨 뒤에 둔다. 마지막에 읽은 것이 지금 화면이어야
        # 모델이 무엇을 기준으로 판단할지 헷갈리지 않는다.
        order = (
            ("checkpoint", "the screen when this scene began"),
            ("failure", "the screen at the step that failed"),
            ("pre_action", "the screen just before your last action"),
            ("current", "the screen right now"),
        )
        return [
            self._message_for(self._role_caption(caption, held), held)
            for role, caption in order
            if (held := self._held.get(role)) is not None
        ]

    @staticmethod
    def _role_caption(role_caption: str, held: "HeldCapture") -> str:
        """역할을 말하되 capture 자신이 말하는 것을 지우지 않는다.

        전체 화면이면 역할만으로 충분하다. `target_id` 로 잘라 온 그림은 어느 요소인지가
        그 그림의 절반이므로 함께 싣는다.
        """
        if held.caption.startswith("This is the screen"):
            return role_caption
        return f"{role_caption} — {held.caption}"

    def _shift_roles(self, current: "HeldCapture | None") -> None:
        """Move the roles on, from what the run has done since the last call.

        Mechanical on purpose. Letting the model say which picture matters would
        make the arm two changes — the pictures it gets and the judgement it makes
        about them — and only one of those is what this axis is testing.
        """
        state, channel = self._state, self._channel

        # `pre_action` — 지난 turn 의 `current` 는 그 뒤에 action 이 나갔을 때만 "행위 직전"
        # 이 된다. `last_action_frame` 은 게임을 실제로 건드린 action 에서만 움직인다.
        frame = getattr(state, "last_action_frame", None)
        if frame is not None and frame != self._last_action_frame:
            previous = self._held.get("current")
            if previous is not None:
                self._held["pre_action"] = previous
            self._last_action_frame = frame

        # `checkpoint` — scene 이 바뀐 순간의 화면. 그 scene 에 있는 동안 그대로 둔다.
        scene = self._scene_name(channel)
        if scene is not None and scene != self._last_scene:
            if current is not None:
                self._held["checkpoint"] = current
            self._last_scene = scene

        # `failure` — 실패 판정이 새로 적힌 turn 의 화면. 몇 turn 뒤에 내린다.
        failures = sum(1 for result in getattr(state, "step_results", []) if not result.passed)
        if failures > self._last_failures:
            if current is not None:
                self._held["failure"] = current
            self._failure_age = 0
            self._last_failures = failures
        elif "failure" in self._held:
            self._failure_age += 1
            if self._failure_age > FAILURE_CAPTURE_TURNS:
                del self._held["failure"]

        if current is not None:
            self._held["current"] = current

    @staticmethod
    def _scene_name(channel) -> str | None:
        """씬 이름. `pulse` 만 오는 게임에서는 `SceneMemory.scene` 이 끝까지 `None` 이다."""
        scene = getattr(channel, "scene", None)
        if scene is None:
            return None
        return getattr(scene, "scene", None) or getattr(getattr(scene, "pulse", None), "scene", None)

    def _message_for(self, caption: str, held: "HeldCapture"):
        message = build_capture_message(held.capture_id, held.encoded, held.mime_type, caption)
        message.additional_kwargs[TRANSIENT_MESSAGE_KEY] = True
        return message

    async def _current_screen(self) -> "HeldCapture | None":
        """The screen as it is now, downloaded and encoded, or `None` if it is not.

        A capture the tool queued this turn wins over taking a new one: it is at
        least as fresh, the round trip is already paid, and when it is a `target_id`
        close-up it is what the model asked to look at.
        """
        pending = self._state.take_pending_captures()
        capture = pending[-1] if pending else await self._auto_capture()
        if capture is None:
            return None
        try:
            encoded = await fetch_capture(capture.url, capture.mime_type)
        except CaptureFetchError as error:
            # Not fatal, and not worth telling the model about either: it did not
            # ask for this picture, so a line explaining its absence would be noise
            # on a turn that reads the screen text instead.
            logger.warning(
                "[QA] capture %s could not be fetched: %s", capture.capture_id, error
            )
            return None
        return HeldCapture(
            capture_id=capture.capture_id,
            mime_type=capture.mime_type,
            encoded=encoded,
            caption=capture.caption,
        )
