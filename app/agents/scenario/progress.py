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
# 워크플로 C(문장 쓰기)·E(수정)의 노드 경계 보고 — 오케 AuthoringStage.WRITING 그대로.
WRITING = "writing"

# ── 워크플로 노드 (ARTEL-952) ─────────────────────────────────────────────────
#
# `THINKING` 은 루프 시절 값이다 — 모델 호출마다 울려서 "몇 바퀴 돌았나" 를 세는 용도였다.
# 워크플로에는 바퀴가 없고 **노드가 있고, 각 노드가 끝나는 시점을 이쪽이 안다.**
#
# 실측(run 87 trace, 2026-10-06)이 왜 노드가 필요한지 보여 준다. 턴 하나에 진행 줄이 둘뿐이고
# 그 사이가 이렇게 벌어졌다:
#
#     ▶ 턴을 보낸다        17:02:03
#     (50.3초 침묵)        ← B 묶기·순서
#     나눈다·메운다·검수·저장  17:02:53~54  (0.45초)
#     (54.2초 침묵)        ← C 문장 쓰기, 묶음 하나
#     나눈다·메운다·검수·저장  17:03:48     (0.34초)
#     ◀ 답을 냈다          **없음** — 여기서 죽었다
#
# 104초 동안 두 줄이었고, 끝에서 죽은 것과 구분이 안 됐다.
GROUPING = "grouping"        # B 시작 — 케이스를 묶고 순서를 잡는다
GROUPED = "grouped"          # B 끝 — 묶음 몇 개가 나왔는지 수와 함께
BRIDGING = "bridging"        # B 미니 루프 — 걷기 검증에서 어긋나 다시 묶는다
SAVING = "saving"            # D — 제출·검수·저장
MODIFYING = "modifying"      # E — 기존 시나리오를 고친다


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
