"""The `MacroHost` the executer runs the planner's macros with.

The macro runner (`app/agents/qa/macro/runner.py`) is used as it is: it reads a parsed macro and
asks its host to act, to read, to wait and to ask Jev. This host does what the tool loop's
`macro_tools._Host` does, over the run's channel:

| `MacroHost`      | here                                                                          |
|------------------|-------------------------------------------------------------------------------|
| `run`            | `QaRunChannel.act_and_look`, the call the tool loop's `ToolContext.act` makes   |
| `memories`       | the channel's `SceneMemory`                                                     |
| `wait_for_pulse` | `QaRunChannel.wait_for_reading`                                                 |
| `checkpoint`     | does not wait: the macro stops there and the turn ends                          |
| `decide`/`ask`   | `JevClient` from `app/qa/jev.py`, the client the tool loop's host uses          |

operator 의 말은 runner 에 넘기지 않는다. 이 engine 에는 답할 사람이 없고, 넘기면 macro 가
`OPERATOR_INTERRUPTED` 로 멈춘다. 받은 말은 비우고 turn 기록에 남긴다.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.agents.qa.macro.binding import MacroMemories
from app.agents.qa.macro.runner import (
    MacroAskRequest,
    MacroDecideRequest,
    MacroJevUnavailable,
    MacroPulseWait,
    MacroRunResult,
)
from app.agents.qa.tools.tool_context import _is_press, _press_landing
from app.qa.acting import ActionOutcome, ScreenChange
from app.qa.channel import QaRunChannel
from app.qa.envelope import JsonRpcAction
from app.qa.jev import JevClient, JevError

# `ask` 의 참·거짓이 뜻하는 것. tool loop 의 `macro_tools._Host.ask` 와 같은 글이다.
ASK_TRUE_MEANS = "The answer to the question is yes."
ASK_FALSE_MEANS = "The answer to the question is no."


@dataclass
class ChannelMacroHost:
    """`run_macro` 가 바깥에 대고 하는 일을 run 의 channel 과 Jev client 에 잇는다."""

    channel: QaRunChannel
    jev: JevClient
    step: int
    # 보낸 batch 마다 {summary, ok, error, reached}.
    sent: list[dict[str, Any]] = field(default_factory=list)
    # `checkpoint` 나 `none_fits` 로 멈춘 이유.
    paused: str | None = None
    operator_messages: list[str] = field(default_factory=list)

    async def run(self, actions: list[JsonRpcAction], summary: str, step: int) -> ActionOutcome:
        record: dict[str, Any] = {"summary": summary, "ok": False, "error": None, "reached": None}
        self.sent.append(record)
        result, looked = await self.channel.act_and_look(actions, summary, step)
        self._drain_operator()
        if result is None:
            record["error"] = "no answer from the game"
            return ActionOutcome(text=f"{summary}: the game reported no result")
        methods = {action.id: action.method for action in actions}
        returned, failures = [], []
        for item in result.results:
            if item.id not in methods:
                continue
            if item.success:
                returned.append(item.returnValue)
            else:
                failures.append(f"{methods[item.id]}: {item.error or 'no reason given'}")
        record["ok"] = not failures
        record["error"] = "; ".join(failures) or None
        record["reached"] = next(
            (value["reached"] or "nothing" for value in returned if _is_press(value)), None
        )
        if looked:
            screen = ScreenChange.moved
        elif self.channel.scene.pulse.seen:
            screen = ScreenChange.still
        else:
            screen = ScreenChange.unknown
        return ActionOutcome(
            text=f"{summary}: " + (f"FAILED — {record['error']}" if failures else "ok"),
            screen=screen,
            landings=tuple(_press_landing(value) for value in returned if _is_press(value)),
        )

    def memories(self) -> MacroMemories:
        return MacroMemories(scene=self.channel.scene)

    async def wait_for_pulse(self, timeout: float) -> MacroPulseWait:
        loop = asyncio.get_running_loop()
        started = loop.time()
        pulses = await self.channel.wait_for_reading(timeout)
        self._drain_operator()
        return MacroPulseWait(pulses, loop.time() - started)

    async def checkpoint(self, result: MacroRunResult) -> bool:
        if result.paused_decision is not None:
            self.paused = f"`decide` found no case that fits: {result.paused_reason}"
        else:
            self.paused = f"stopped at a checkpoint: {result.paused_reason}"
        return False

    async def decide(self, request: MacroDecideRequest) -> str:
        options = {option.name: option.description for option in request.options}
        try:
            return await self.jev.choose(request.question, options, request.state)
        except JevError as error:
            raise MacroJevUnavailable(str(error)) from error

    async def ask(self, request: MacroAskRequest) -> float:
        try:
            return await self.jev.probability(
                request.question, request.state, true_means=ASK_TRUE_MEANS, false_means=ASK_FALSE_MEANS
            )
        except JevError as error:
            raise MacroJevUnavailable(str(error)) from error

    def _drain_operator(self) -> None:
        self.operator_messages.extend(self.channel.drain_operator_messages())
