"""엔진이 내린 결정을 `LOG` 한 줄로 남긴다(ARTEL-1186).

turn 은 executer 가 `TurnLogPayload` 로 남기지만, 그 바깥의 결정 — planner 가 무엇을 계획했고 무엇이
거절됐는지, 어느 씬으로 가려다 닿았는지, 라운드와 리셋 — 은 남지 않았다. stage 런 157 은 씬 하나에서
16개를 계획하고 play 를 0번 했는데 왜인지 읽을 곳이 없었다. `llm_usage` 에도 본문이 없다.

줄마다 `kind` 로 결정의 종류를, `decision` 에 그 내용을 구조로 싣는다. `message` 는 그것만 읽어도 무엇을
왜 했는지 알 수 있게 쓴다.
"""

import json
from typing import Any

from pydantic import Field

from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory, LogPayload, MessageType

# Orchestration 의 소켓은 64 KiB 를 넘는 frame 에서 닫힌다. `MAX_TURN_LOG_BYTES` 와 같은 여유를 둔다.
MAX_DECISION_LOG_BYTES = 48_000
# planner 가 쓴 macro source 와 parser 의 거절 사유. 결정을 되짚는 데는 앞부분이면 된다.
MAX_SOURCE_CHARS = 6_000
MAX_REFUSAL_CHARS = 2_000
# `message` 에 넣는 짧은 인용.
MAX_QUOTE_CHARS = 300


class DecisionLogPayload(LogPayload):
    """엔진의 결정 하나. `kind` 가 종류를 가른다(`scene_plan`, `travel`, `free_round` …)."""

    kind: str
    decision: dict[str, Any] = Field(default_factory=dict)


def clip(text: str | None, limit: int) -> str:
    """`limit` 글자까지. 잘랐으면 몇 글자를 버렸는지 끝에 적는다."""
    text = text or ""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… (+{len(text) - limit} chars)"


async def note_decision(
    channel: QaRunChannel, kind: str, message: str, step: int | None = None, **decision: Any
) -> None:
    """결정 하나를 `LOG` 로 보낸다. 커서 frame 한도를 넘으면 `source` 부터 뺀다."""
    body = json.loads(json.dumps(decision, default=str))
    if _size(body) > MAX_DECISION_LOG_BYTES:
        body.pop("source", None)
    if _size(body) > MAX_DECISION_LOG_BYTES:
        body = {"dropped": f"the decision body was over {MAX_DECISION_LOG_BYTES} bytes"}
    await channel.emit(
        MessageType.LOG,
        DecisionLogPayload(category=LogCategory.THOUGHT, message=message, step=step, kind=kind, decision=body),
    )


def _size(body: dict) -> int:
    return len(json.dumps(body, ensure_ascii=False).encode())
