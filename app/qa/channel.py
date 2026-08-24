"""The bridge between the agent's tools and the WebSocket.

A tool call looks synchronous — ask for the scene, get the scene. The transport
is not: the request goes out on the socket and the answer arrives later as a
separate inbound message. This module holds the futures that make one look like
the other, so `app/agents/qa/tools.py` can simply `await`.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.qa.envelope import (
    ActionPayload,
    ActionResultPayload,
    ChatPayload,
    GameState,
    JsonRpcAction,
    KnowledgeExpandPayload,
    KnowledgeExpandResultPayload,
    KnowledgeSearchPayload,
    KnowledgeSearchResultPayload,
    KnowledgeWriteResultPayload,
    LogCategory,
    LogPayload,
    MessageType,
    outbound_envelope,
)
from app.qa.pulse import PulseReading
from app.qa.scene import SceneMemory

logger = logging.getLogger(__name__)

# How long a tool waits for the game before giving up on one request.
#
# Returned as a value rather than raised: the server cannot tell a long load from
# a dead game, so the agent decides whether to retry, wait longer, or fail the
# step. The run's overall deadline is the real backstop.
ACTION_TIMEOUT_SECONDS = 30.0

# Ceiling on a tool's requested wait. The agent picks the number, and an
# unbounded one would park the run until the overall deadline killed it.
MAX_SCENE_WAIT_SECONDS = 30.0

# Ceiling on a wait for the operator. Longer than a scene wait because a person
# has to read the question and type, and shorter than the run's deadline so the
# run still ends by its own account rather than by being killed mid-wait.
MAX_OPERATOR_WAIT_SECONDS = 300.0

# How long a knowledge search waits before the agent is told nobody answered.
#
# Shorter than an action's wait on purpose. An action is the run making progress,
# so it is worth waiting out; a knowledge search only buys context for a judgement
# the agent can also make without it. The round trip is not free on the far side —
# Orchestration embeds the query and then queries pgvector — so this is not as
# tight as a local call, but it must never be the reason a run misses its deadline.
KNOWLEDGE_SEARCH_TIMEOUT_SECONDS = 20.0

# How long a knowledge write waits for its answer.
#
# Much shorter than a search's, and the difference is the point: a search makes
# Orchestration embed a query and hit pgvector, while a write is one database
# statement it has already finished by the time it answers. The number that
# matters is the one paid when NO answer comes — against an Orchestration that
# predates ARTEL-331 every write waits this out, and that cost is per write, per
# run. Silence is not a failure here (see `write_knowledge`), so waiting longer
# would buy nothing but a slower run.
KNOWLEDGE_WRITE_TIMEOUT_SECONDS = 5.0


class QaCancelled(Exception):
    """Raised inside the agent loop when the operator ends the run."""


@dataclass(frozen=True)
class _PendingRequest:
    """One request waiting on its answer.

    Carries the request's own type beside the future because the answer is
    checked against it (ARTEL-367). Orchestration echoes the request type in a
    write's result, and nothing was reading that echo — a mismatch would have
    put the wrong id into `knowledge_seen`, and the next correction would go to
    the wrong entry. Nothing produces a mismatch today; the point is that if
    something started to, no one would find out.
    """

    waiter: "asyncio.Future[Any]"
    request_type: str


@dataclass(frozen=True)
class KnowledgeRequestFailed:
    """Orchestration answered the request with an ERROR frame.

    Distinct from an ordinary answer — a search's empty `results`, a write that
    went through — and from no answer at all. Only this one means the request
    itself was refused or could not run: a bad filter, an embedding model
    mismatch, a database that would not answer, an entry that is not there.

    Shared by the search, the expansion and the writes (ARTEL-332). Orchestration
    reports all three failures the same way, so telling them apart here would be
    this side inventing a distinction the wire does not carry.
    """

    reason: str


class QaRunChannel:
    """Sends frames for one QA session and resolves what comes back."""

    def __init__(
        self,
        qa_try_id: int,
        send: Callable[[dict], Awaitable[None]],
        action_timeout: float = ACTION_TIMEOUT_SECONDS,
        write_timeout: float = KNOWLEDGE_WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self._qa_try_id = qa_try_id
        self._send = send
        self._action_timeout = action_timeout
        # Injectable for the same reason `action_timeout` is: a suite that waited
        # out the real one would pay it on every write it exercises.
        self._write_timeout = write_timeout
        self._sequence = 0
        self._action_waiter: asyncio.Future[ActionResultPayload] | None = None
        self._pending_action_id: str | None = None
        # Knowledge requests waiting on an answer, keyed by the messageId
        # Orchestration echoes back as `correlationId`.
        #
        # One map rather than a pair of fields per request kind (ARTEL-332). Those
        # fields existed to stop two frames in flight from resolving each other's
        # future — an expansion answering a search with the wrong content, which
        # nothing about the payload would make obvious. Keying on the correlation
        # rules that out by construction instead, and it scales: writes are the
        # third kind to need it and would have been a third pair of fields.
        #
        # The ACTION waiter above stays separate on purpose. Its timeout, its
        # cancellation and its late-answer rule are the game's, not the knowledge
        # base's, and folding it in here would spread this change across a path
        # this issue does not touch.
        self._pending: dict[str, _PendingRequest] = {}
        self.scene = SceneMemory()
        self.cancelled = False
        # Operator messages that arrived since the agent last looked. Delivered
        # by appending to the next tool result rather than interrupting the graph:
        # simpler, and it guarantees the words reach the next decision.
        self._operator_messages: list[str] = []
        # Set exactly while that list has something in it, so a tool can wait on
        # the operator instead of only picking messages up in passing.
        self._operator_arrived = asyncio.Event()
        # Everything the operator has said this run, in order — the durable record
        # beside the delivery queue above, which is emptied as soon as a tool picks
        # it up. Once delivered, an instruction exists only inside one tool result's
        # text, and compaction replaces exactly that text; "it applies from now on"
        # then stops being true halfway through a run. This list is what
        # `render_progress_ledger` restates afterwards.
        #
        # Recorded in `on_chat` rather than at any tool, because that is the single
        # funnel every operator message passes through: a tool added later cannot
        # forget to do it.
        #
        # Unbounded, and bounded in practice by the run's own deadline: an operator
        # cannot type enough in 600 seconds for the restated block to matter.
        self.operator_instructions: list[str] = []

    @property
    def qa_try_id(self) -> int:
        """Stable correlation value exposed for trace metadata."""
        return self._qa_try_id

    # --- outbound -------------------------------------------------------------

    def _frame(self, message_type: MessageType, payload, correlation_id: str | None = None) -> dict:
        self._sequence += 1
        return outbound_envelope(
            message_type,
            self._qa_try_id,
            self._sequence,
            payload,
            correlation_id=correlation_id,
        )

    async def note(self, message: str, category: LogCategory, step: int | None = None) -> None:
        """Put a line on the timeline. Nothing waits for it."""
        await self._send(
            self._frame(MessageType.LOG, LogPayload(category=category, message=message, step=step))
        )

    async def say(self, message: str, step: int | None = None) -> None:
        await self._send(self._frame(MessageType.CHAT, ChatPayload(message=message, step=step)))

    async def emit(self, message_type: MessageType, payload, correlation_id: str | None = None) -> None:
        await self._send(self._frame(message_type, payload, correlation_id))

    async def look(
        self, after_seconds: float, message: str, step: int | None = None
    ) -> bool:
        """Ask the game for the current scene. True when a fresh one arrived.

        Sent as an ACTION carrying `scan_scene`, the same path the acting tools
        use. There was a second path — a `REQUEST_GAME_STATE` frame that made
        Orchestration send the SDK a top-level `GET_GAME_STATE` — but the SDK
        treats that as a compatibility alias and names `scan_scene` as the real
        method (see ArtelManager's own error text). Two paths to one answer also
        made the timeline lie: the `GET_GAME_STATE` row logged this envelope
        rather than the frame that actually went out.
        """
        self._raise_if_cancelled()
        if after_seconds > 0:
            # Safe to sleep: the run is its own asyncio task (see
            # app/api/qa_sessions.py), so this holds up nothing but this tool.
            await asyncio.sleep(min(after_seconds, MAX_SCENE_WAIT_SECONDS))
        # Frames, not observations: `updates` restarts at 1 on a scene change,
        # so comparing it would report a transition as the game having stayed
        # silent.
        before = self.scene.frames
        await self.dispatch_actions(
            [JsonRpcAction(id=1, method="scan_scene", params=[])], message, step
        )
        return self.scene.frames > before

    async def act_and_look(
        self, actions: list[JsonRpcAction], message: str, step: int | None = None
    ) -> tuple[ActionResultPayload | None, bool]:
        """Run actions, then read the scene they produced, in one batch.

        `scan_scene` rides at the end of the same batch on purpose. Asked for
        separately it answers straight out of the SDK's message handler and can
        return the screen as it was *before* a click's cursor movement finished —
        the agent would then judge the step against a stale scene. Queued behind
        the actions it cannot run early.

        Returns the results and whether a fresh scene arrived.
        """
        before = self.scene.frames
        batch = actions + [
            JsonRpcAction(id=len(actions) + 1, method="scan_scene", params=[])
        ]
        result = await self.dispatch_actions(batch, message, step)
        return result, self.scene.frames > before

    async def dispatch_actions(
        self, actions: list[JsonRpcAction], message: str, step: int | None = None
    ) -> ActionResultPayload | None:
        """Run actions on the game. `None` means no result came back."""
        self._raise_if_cancelled()
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[ActionResultPayload] = loop.create_future()
        self._action_waiter = waiter

        frame = self._frame(
            MessageType.ACTION, ActionPayload(message=message, step=step, actions=actions)
        )
        self._pending_action_id = frame["messageId"]
        await self._send(frame)
        try:
            return await asyncio.wait_for(waiter, timeout=self._action_timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._action_waiter = None
            self._pending_action_id = None

    async def search_knowledge(
        self, query: str, tag: str | None, limit: int, step: int | None = None
    ) -> KnowledgeSearchResultPayload | KnowledgeRequestFailed | None:
        """Ask the project's knowledge base a question. `None` means no answer came.

        Three outcomes, kept apart as three types, because the agent should act
        differently on each: a payload (which may carry an empty `results`, and
        that is a real answer), a refusal, or silence. Returned rather than
        raised, for the same reason `look` returns a boolean — a knowledge lookup
        is a side errand to the verdict, and none of these is a reason to stop
        the run.

        Nothing about the game is touched here: no action goes out, so no scene
        comes back, so no caller ends up with a scene view to append.
        """
        return await self._request(
            MessageType.KNOWLEDGE_SEARCH,
            KnowledgeSearchPayload(query=query, tag=tag, limit=limit, step=step),
            KNOWLEDGE_SEARCH_TIMEOUT_SECONDS,
        )

    async def expand_knowledge(
        self, knowledge_id: str, depth: int, include_similar: bool, step: int | None = None
    ) -> KnowledgeExpandResultPayload | KnowledgeRequestFailed | None:
        """Walk the knowledge graph out from one entry. `None` means no answer came.

        Same three outcomes as `search_knowledge`, and the same argument for
        returning rather than raising. The timeout is the search's, because the
        work on the far side is the same shape — a couple of indexed queries.
        """
        return await self._request(
            MessageType.KNOWLEDGE_EXPAND,
            KnowledgeExpandPayload(
                knowledge_id=knowledge_id,
                depth=depth,
                include_similar=include_similar,
                step=step,
            ),
            KNOWLEDGE_SEARCH_TIMEOUT_SECONDS,
        )

    async def write_knowledge(
        self, message_type: MessageType, payload
    ) -> KnowledgeWriteResultPayload | KnowledgeRequestFailed | None:
        """Write to the project's knowledge base, and wait for the verdict.

        Three outcomes, as with a search, but the third one means something
        different here and getting it wrong is expensive:

        - a payload — Orchestration stored it, and for a create the payload
          carries the new entry's id
        - `KnowledgeRequestFailed` — it refused. The write did NOT happen
        - `None` — **no answer came, which is not the same as no write.**
          Orchestration performs the write and skips the reply when the run has
          no Agent session, drops frames it cannot route without answering them,
          and an Orchestration older than ARTEL-331 never answers at all. Every
          one of those looks identical from here.

        A caller that reports `None` as a failure makes the model write the same
        fact again, which is the duplicate this whole contract exists to stop.
        `app/agents/qa/tools.py` says "sent, not confirmed, do not send it again"
        for exactly that reason.
        """
        return await self._request(message_type, payload, self._write_timeout)

    async def _request(self, message_type: MessageType, payload, timeout: float) -> Any:
        """Send one frame and park until its answer arrives. `None` means none did.

        The answer's type follows the request's — callers annotate what they
        expect, because what lands in the future is whatever the matching
        `on_*` handler validated. That handler is chosen by the inbound frame's
        type, so a mismatch here would mean Orchestration answered one request
        with another request's frame.

        The entry is removed in `finally`, so a late answer to a request already
        abandoned at its timeout finds nothing to resolve and is dropped.
        """
        self._raise_if_cancelled()
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[Any] = loop.create_future()
        frame = self._frame(message_type, payload)
        message_id = frame["messageId"]
        self._pending[message_id] = _PendingRequest(waiter, str(message_type))
        try:
            await self._send(frame)
            return await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(message_id, None)

    # --- inbound --------------------------------------------------------------

    def on_game_state(self, raw: dict) -> None:
        # No future to resolve: a scene answers the `scan_scene` action it rode
        # in with, and the ACTION_RESULT is what releases the waiting tool. Scenes
        # the game volunteers land here too, and are folded into memory the same
        # way — the watermark means the next render still reports their changes.
        self.scene.apply(GameState.model_validate(raw.get("payload") or {}))

    def on_pulse(self, raw: dict) -> None:
        """판독 하나를 씬 메모리 위에 얹는다.

        `on_game_state` 와 달리 풀어 줄 future 가 없다. 판독은 무엇을 물어서 오는 것이
        아니라 게임이 도는 동안 계속 도착하는 관측이고, 도구가 그것을 기다리지 않는다.
        """
        self.scene.pulse.apply(PulseReading.model_validate(raw.get("payload") or {}))

    def on_action_result(self, raw: dict) -> None:
        correlation = raw.get("correlationId")
        # Orchestration echoes the ACTION's messageId as the result's correlation.
        # A mismatch means this answers something we are no longer waiting on.
        if (
            self._pending_action_id is not None
            and correlation is not None
            and correlation != self._pending_action_id
        ):
            return
        payload = ActionResultPayload.model_validate(raw.get("payload") or {})
        if self._action_waiter is not None and not self._action_waiter.done():
            self._action_waiter.set_result(payload)

    def on_knowledge_search_result(self, raw: dict) -> None:
        self._resolve(raw, KnowledgeSearchResultPayload.model_validate(raw.get("payload") or {}))

    def on_knowledge_expand_result(self, raw: dict) -> None:
        self._resolve(raw, KnowledgeExpandResultPayload.model_validate(raw.get("payload") or {}))

    def on_knowledge_write_result(self, raw: dict) -> None:
        """A write's answer, checked against what was asked (ARTEL-367).

        Orchestration echoes the request type in the payload. Matching it costs a
        comparison and buys the one thing correlation alone cannot give: if the two
        ever disagree, the answer is dropped instead of being believed. Believing it
        would file the wrong id under `knowledge_seen`, and every correction after
        that would land on the wrong entry — silently, because the id is real.

        Dropped rather than surfaced as a failure. A mismatch is a protocol fault on
        the far side, not something the run can act on, and turning it into a tool
        failure would make the model rewrite a fact that may well have been stored.
        The tool times out into "cannot confirm", which is what the situation is.
        """
        payload = KnowledgeWriteResultPayload.model_validate(raw.get("payload") or {})
        correlation = raw.get("correlationId")
        pending = self._pending.get(correlation) if isinstance(correlation, str) else None
        if pending is not None and payload.type and payload.type != pending.request_type:
            logger.warning(
                "[QA] a %s answer arrived for a %s request (correlation %s); dropped",
                payload.type,
                pending.request_type,
                correlation,
            )
            return
        self._resolve(raw, payload)

    def on_error(self, raw: dict) -> bool:
        """An inbound ERROR. True when it was the answer to something we asked.

        Orchestration replies to a refused request with an ERROR carrying that
        request's correlation id, so this is how a search, an expansion or a
        write that could not run reaches the tool waiting on it. An uncorrelated
        ERROR is somebody reporting a problem rather than answering, and there is
        nothing to release — the caller decides what to do with that.

        The payload is read field by field rather than through `ErrorPayload`:
        Orchestration's failure frame carries only `message`, and that model
        requires a `code`. Validating here would drop exactly the frame that
        exists to unblock a waiting tool.
        """
        payload = raw.get("payload") or {}
        reason = payload.get("message") if isinstance(payload, dict) else None
        return self._resolve(raw, KnowledgeRequestFailed(reason=str(reason or "no reason given")))

    def _resolve(self, raw: dict, answer: Any) -> bool:
        """Hand `answer` to whatever is waiting on this frame's correlation.

        False when nothing was: an answer to a request already abandoned at its
        timeout, or an unsolicited frame. Both are dropped rather than applied to
        whatever happens to be waiting now — that mix-up is what the correlation
        key exists to prevent.
        """
        correlation = raw.get("correlationId")
        pending = self._pending.get(correlation) if isinstance(correlation, str) else None
        if pending is None or pending.waiter.done():
            return False
        pending.waiter.set_result(answer)
        return True

    def on_chat(self, raw: dict) -> None:
        payload = ChatPayload.model_validate(raw.get("payload") or {})
        self._operator_messages.append(payload.message)
        self.operator_instructions.append(payload.message)
        self._operator_arrived.set()

    def on_cancel(self) -> None:
        self.cancelled = True
        if self._action_waiter is not None and not self._action_waiter.done():
            self._action_waiter.cancel()
        # Same for any knowledge request in flight: left alone it would hold the
        # run open for the rest of its timeout after the operator already ended it.
        #
        # Sweeping the map covers all three kinds. Before ARTEL-332 this cancelled
        # the search only, so an expansion the operator interrupted still sat out
        # its full 20 seconds — a bug the per-kind fields made easy to miss and the
        # map makes impossible to have.
        for pending in self._pending.values():
            if not pending.waiter.done():
                pending.waiter.cancel()
        # A tool parked on the operator has no action to cancel, so wake it and
        # let it find the cancellation itself.
        self._operator_arrived.set()

    # --- operator ------------------------------------------------------------

    def drain_operator_messages(self) -> list[str]:
        messages = self._operator_messages
        self._operator_messages = []
        self._operator_arrived.clear()
        return messages

    async def wait_for_operator(self, timeout_seconds: float) -> list[str]:
        """Park until the operator says something. Empty means nobody did.

        Returns rather than raises on a timeout, for the same reason `look` does:
        silence is a thing the agent decides about, not an error.
        """
        self._raise_if_cancelled()
        try:
            await asyncio.wait_for(
                self._operator_arrived.wait(),
                timeout=bounded_operator_wait(timeout_seconds),
            )
        except asyncio.TimeoutError:
            return []
        # Cancellation wakes this too, and it must not be read as an answer.
        self._raise_if_cancelled()
        return self.drain_operator_messages()

    def _raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise QaCancelled()


def bounded_operator_wait(timeout_seconds: float) -> float:
    """The wait the channel will actually make, which is what a tool must quote.

    Shared so the tool's "nobody answered within Ns" cannot name a number the
    channel never waited.
    """
    return min(max(timeout_seconds, 0.0), MAX_OPERATOR_WAIT_SECONDS)


def with_operator_messages(result: str, messages: list[str]) -> str:
    """Append what the operator said to a tool result.

    The words have to land inside the loop, and the tool result is the only place
    the model is guaranteed to read before its next decision.
    """
    if not messages:
        return result
    lines = "\n".join(f"  - {message}" for message in messages)
    return f"{result}\n\nThe operator said, and it applies from now on:\n{lines}"
