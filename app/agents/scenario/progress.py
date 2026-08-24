"""Telling the client where an authoring turn is, while it is still running.

Orchestration sees every tool call — each one crosses the socket as its own
frame — but it cannot see the model turns between them, and those are most of the
wall clock. A turn that thinks for forty seconds and calls one tool therefore
looked exactly like one that died right after the tool: one line on screen, then
silence.

So the model turns report themselves. `on_chat_model_start` fires once per model
turn in the loop, which is precisely the thing the far side is blind to, and it
alternates with the tool frames it already sees — so the count of "thinking"
lines is also how many times the loop has gone round.

Nothing here can fail a turn: a dropped progress line costs a line on screen.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler

if TYPE_CHECKING:
    from app.sessions.channel import ScenarioChannel

logger = logging.getLogger(__name__)

# The wire value. Orchestration maps it to AuthoringStage.THINKING and drops
# stages it does not know, so adding one here does not require a deploy there.
THINKING = "thinking"


class ProgressCallback(AsyncCallbackHandler):
    """Reports each model turn on the authoring session's socket."""

    def __init__(self, channel: ScenarioChannel) -> None:
        self._channel = channel

    async def on_chat_model_start(
        self, serialized: dict[str, Any], messages: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        try:
            await self._channel.report(THINKING)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - see module docstring
            logger.debug("[scenario] could not report progress", exc_info=True)
