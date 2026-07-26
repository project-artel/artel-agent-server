"""The bridge between the agent's tools and the WebSocket.

A tool call looks synchronous — ask for the scene, get the scene. The transport
is not: the request goes out on the socket and the answer arrives later as a
separate inbound message. This module holds the futures that make one look like
the other, so `app/agents/qa/tools.py` can simply `await`.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.qa.envelope import (
    ActionPayload,
    ActionResultPayload,
    ChatPayload,
    GameState,
    JsonRpcAction,
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


class QaCancelled(Exception):
    """Raised inside the agent loop when the operator ends the run."""


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
        self.scene = SceneMemory()
        self.cancelled = False
        # Operator messages that arrived since the agent last looked. Delivered
        # by appending to the next tool result rather than interrupting the graph:
        # simpler, and it guarantees the words reach the next decision.
        self._operator_messages: list[str] = []

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
        before = self.scene.updates
        await self.dispatch_actions(
            [JsonRpcAction(id=1, method="scan_scene", params=[])], message, step
        )
        return self.scene.updates > before

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
        before = self.scene.updates
        batch = actions + [
            JsonRpcAction(id=len(actions) + 1, method="scan_scene", params=[])
        ]
        result = await self.dispatch_actions(batch, message, step)
        return result, self.scene.updates > before

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

    def on_chat(self, raw: dict) -> None:
        payload = ChatPayload.model_validate(raw.get("payload") or {})
        self._operator_messages.append(payload.message)

    def on_cancel(self) -> None:
        self.cancelled = True
        if self._action_waiter is not None and not self._action_waiter.done():
            self._action_waiter.cancel()

    # --- operator ------------------------------------------------------------

    def drain_operator_messages(self) -> list[str]:
        messages = self._operator_messages
        self._operator_messages = []
        return messages

    def _raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise QaCancelled()


def with_operator_messages(result: str, messages: list[str]) -> str:
    """Append what the operator said to a tool result.

    The words have to land inside the loop, and the tool result is the only place
    the model is guaranteed to read before its next decision.
    """
    if not messages:
        return result
    lines = "\n".join(f"  - {message}" for message in messages)
    return f"{result}\n\nThe operator said, and it applies from now on:\n{lines}"
