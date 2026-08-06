"""The bridge between the agent's tools and the WebSocket.

A tool call looks synchronous — ask for the scene, get the scene. The transport
is not: the request goes out on the socket and the answer arrives later as a
separate inbound message. This module holds the futures that make one look like
the other, so `app/agents/qa/tools.py` can simply `await`.
"""

import asyncio
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
    LogCategory,
    LogPayload,
    MessageType,
    outbound_envelope,
)
from app.qa.scene import SceneMemory

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


class QaCancelled(Exception):
    """Raised inside the agent loop when the operator ends the run."""


@dataclass(frozen=True)
class KnowledgeSearchFailed:
    """Orchestration answered the search with an ERROR frame.

    Distinct from an empty result, which is an ordinary answer, and from no answer
    at all. Only this one means the search itself could not run — a bad filter, an
    embedding model mismatch, a database that would not answer.
    """

    reason: str


class QaRunChannel:
    """Sends frames for one QA session and resolves what comes back."""

    def __init__(
        self,
        qa_try_id: int,
        send: Callable[[dict], Awaitable[None]],
        action_timeout: float = ACTION_TIMEOUT_SECONDS,
    ) -> None:
        self._qa_try_id = qa_try_id
        self._send = send
        self._action_timeout = action_timeout
        self._sequence = 0
        self._action_waiter: asyncio.Future[ActionResultPayload] | None = None
        self._pending_action_id: str | None = None
        # A second waiter rather than a shared one: a knowledge search and a game
        # action are answered by different frames and must not be able to resolve
        # each other's request.
        self._knowledge_waiter: (
            asyncio.Future[KnowledgeSearchResultPayload | KnowledgeSearchFailed] | None
        ) = None
        self._pending_knowledge_id: str | None = None
        self._expand_waiter: (
            asyncio.Future[KnowledgeExpandResultPayload | KnowledgeSearchFailed] | None
        ) = None
        self._pending_expand_id: str | None = None
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
        self, query: str, tag: str | None, limit: int
    ) -> KnowledgeSearchResultPayload | KnowledgeSearchFailed | None:
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
        self._raise_if_cancelled()
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[KnowledgeSearchResultPayload | KnowledgeSearchFailed] = (
            loop.create_future()
        )
        self._knowledge_waiter = waiter

        frame = self._frame(
            MessageType.KNOWLEDGE_SEARCH,
            KnowledgeSearchPayload(query=query, tag=tag, limit=limit),
        )
        self._pending_knowledge_id = frame["messageId"]
        await self._send(frame)
        try:
            return await asyncio.wait_for(waiter, timeout=KNOWLEDGE_SEARCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return None
        finally:
            self._knowledge_waiter = None
            self._pending_knowledge_id = None

    async def expand_knowledge(
        self, knowledge_id: str, depth: int, include_similar: bool
    ) -> KnowledgeExpandResultPayload | KnowledgeSearchFailed | None:
        """Walk the knowledge graph out from one entry. `None` means no answer came.

        Same three outcomes as `search_knowledge`, and the same argument for
        returning rather than raising.

        This gets its OWN waiter rather than sharing the search's, for the reason
        the action waiter is separate from that one: two frames in flight whose
        replies can resolve each other's futures is a bug that shows up as an
        expansion answering a search with the wrong content, and nothing about
        the payload would make it obvious. The timeout is the search's, because
        the work on the far side is the same shape — a couple of indexed queries.
        """
        self._raise_if_cancelled()
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[KnowledgeExpandResultPayload | KnowledgeSearchFailed] = (
            loop.create_future()
        )
        self._expand_waiter = waiter

        frame = self._frame(
            MessageType.KNOWLEDGE_EXPAND,
            KnowledgeExpandPayload(
                knowledge_id=knowledge_id, depth=depth, include_similar=include_similar
            ),
        )
        self._pending_expand_id = frame["messageId"]
        await self._send(frame)
        try:
            return await asyncio.wait_for(waiter, timeout=KNOWLEDGE_SEARCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return None
        finally:
            self._expand_waiter = None
            self._pending_expand_id = None

    async def write_knowledge(self, message_type: MessageType, payload) -> None:
        """Write to the project's knowledge base. Nothing comes back, by design.

        The deliberate asymmetry with `search_knowledge` above: that one parks on
        a future until Orchestration answers, this one has no waiter and no
        correlation to match, because Orchestration's `routeKnowledgeMutation`
        answers a mutation with nothing at all. A success is silent and a
        rejection becomes an ORCHE_INTERNAL error row on the run's timeline,
        published to the operator's stream rather than sent back down this socket.
        A tool that waited here would therefore wait out its whole timeout on
        every call, including the ones that worked.

        What that costs is honesty about the result, and the tools pay it rather
        than hide it: this side can report that the frame went out, never that it
        landed. See `app/agents/qa/knowledge.py`.
        """
        self._raise_if_cancelled()
        await self._send(self._frame(message_type, payload))

    # --- inbound --------------------------------------------------------------

    def on_game_state(self, raw: dict) -> None:
        # No future to resolve: a scene answers the `scan_scene` action it rode
        # in with, and the ACTION_RESULT is what releases the waiting tool. Scenes
        # the game volunteers land here too, and are folded into memory the same
        # way — the watermark means the next render still reports their changes.
        self.scene.apply(GameState.model_validate(raw.get("payload") or {}))

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
        if not self._answers_pending_search(raw):
            return
        self._resolve_knowledge(
            KnowledgeSearchResultPayload.model_validate(raw.get("payload") or {})
        )

    def on_knowledge_expand_result(self, raw: dict) -> None:
        if not self._answers_pending_expand(raw):
            return
        self._resolve_expand(
            KnowledgeExpandResultPayload.model_validate(raw.get("payload") or {})
        )

    def on_error(self, raw: dict) -> bool:
        """An inbound ERROR. True when it was the answer to something we asked.

        Orchestration replies to a failed search with an ERROR carrying the
        request's correlation id, so this is how a search that could not run
        reaches the tool waiting on it. An uncorrelated ERROR is somebody
        reporting a problem rather than answering, and there is nothing to
        release — the caller decides what to do with that.

        The payload is read field by field rather than through `ErrorPayload`:
        Orchestration's failure frame carries only `message`, and that model
        requires a `code`. Validating here would drop exactly the frame that
        exists to unblock a waiting tool.
        """
        payload = raw.get("payload") or {}
        reason = payload.get("message") if isinstance(payload, dict) else None
        failure = KnowledgeSearchFailed(reason=str(reason or "no reason given"))
        if self._answers_pending_search(raw):
            self._resolve_knowledge(failure)
            return True
        # An expansion that could not run is answered the same way, so its waiter
        # has to be released here too — otherwise the tool sits out the full
        # timeout on a failure Orchestration already reported.
        if self._answers_pending_expand(raw):
            self._resolve_expand(failure)
            return True
        return False

    def _answers_pending_search(self, raw: dict) -> bool:
        """Whether this frame is the reply to the search currently in flight.

        Orchestration echoes the request's messageId as the reply's correlation,
        so a mismatch means the frame answers something we no longer wait on —
        a search abandoned at its timeout, most likely, whose late answer must
        not resolve the next one.
        """
        if self._pending_knowledge_id is None:
            return False
        return raw.get("correlationId") == self._pending_knowledge_id

    def _answers_pending_expand(self, raw: dict) -> bool:
        """Whether this frame is the reply to the expansion currently in flight."""
        if self._pending_expand_id is None:
            return False
        return raw.get("correlationId") == self._pending_expand_id

    def _resolve_knowledge(
        self, answer: KnowledgeSearchResultPayload | KnowledgeSearchFailed
    ) -> None:
        if self._knowledge_waiter is not None and not self._knowledge_waiter.done():
            self._knowledge_waiter.set_result(answer)

    def _resolve_expand(
        self, answer: KnowledgeExpandResultPayload | KnowledgeSearchFailed
    ) -> None:
        if self._expand_waiter is not None and not self._expand_waiter.done():
            self._expand_waiter.set_result(answer)

    def on_chat(self, raw: dict) -> None:
        payload = ChatPayload.model_validate(raw.get("payload") or {})
        self._operator_messages.append(payload.message)
        self.operator_instructions.append(payload.message)
        self._operator_arrived.set()

    def on_cancel(self) -> None:
        self.cancelled = True
        if self._action_waiter is not None and not self._action_waiter.done():
            self._action_waiter.cancel()
        # Same for a search in flight: left alone it would hold the run open for
        # the rest of its timeout after the operator already ended it.
        if self._knowledge_waiter is not None and not self._knowledge_waiter.done():
            self._knowledge_waiter.cancel()
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
