"""planner–executer 엔진이 지금 하는 일을 orchestration 에 알린다.

계획(planner)과 판정(evaluator)은 LLM 이 답을 쓰는 동안이라 1분 가까이 게임이 멈춘 듯 보인다. 사용자는 그동안
무엇을 하는지 알 수 없었다. 단계가 바뀔 때 `LOG` 에 `phase` 를 실어 보내면, orchestration 이 그것을 `RUN_STATUS`
로 SDK 에 넘겨 하단 줄에 "생각 중…" 을 띄운다. 같은 단계가 이어지면 보내지 않는다.
"""

from enum import StrEnum

from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory, LogPayload, MessageType


class RunPhase(StrEnum):
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    EVALUATING = "EVALUATING"


class PhaseLogPayload(LogPayload):
    phase: RunPhase


_SAYS = {
    RunPhase.PLANNING: "The planner is thinking.",
    RunPhase.EXECUTING: "The executer is playing.",
    RunPhase.EVALUATING: "The evaluator is judging.",
}


class PhaseReporter:
    """런 하나의 단계. 바뀔 때만 한 줄을 보낸다."""

    def __init__(self, channel: QaRunChannel) -> None:
        self._channel = channel
        self._phase: RunPhase | None = None

    async def enter(self, phase: RunPhase, step: int | None = None) -> None:
        if phase == self._phase:
            return
        self._phase = phase
        await self._channel.emit(
            MessageType.LOG,
            PhaseLogPayload(category=LogCategory.SYSTEM, message=_SAYS[phase], step=step, phase=phase),
        )
