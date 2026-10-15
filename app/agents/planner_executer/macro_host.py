"""executer 가 func 의 `run` macro 를 돌리는 `MacroHost`, 그리고 그 결과를 func 의 결과로 읽는 법.

두 QA 구조 — tool loop agent 가 macro 로 쌓는 workflow 와 이 planner → executer → evaluator —
를 비교할 때 재는 것이 구조여야 하므로, 게임에 입력을 보내고 Jev 에 묻는 층을 같게 둔다. 그래서
func 의 `run` 은 macro package 의 `run_macro` 가 돌리고, 이 host 는 tool loop 의
`macro_tools._Host` 와 같은 일을 이 engine 의 `g` 위에서 한다.

| `MacroHost`      | 여기서                                                                     |
|------------------|----------------------------------------------------------------------------|
| `run`            | `GameHandle._act` → `QaRunChannel.act_and_look`. tool loop 의 `ctx.act` 와 같은 길 |
| `memories`       | `g` 가 읽는 channel 의 `SceneMemory` 그대로                                    |
| `wait_for_pulse` | `QaRunChannel.wait_for_reading`. operator 의 말은 trace 로 보낸다               |
| `checkpoint`     | 기다리지 않고 그만둔다: func 의 turn 이 거기서 끝난다                            |
| `decide`/`ask`   | `app/qa/jev.py` 의 `JevClient`. tool loop 의 host 와 같은 호출, 같은 글       |

**`checkpoint` 에서 사람을 기다리지 않는다.** tool loop 에서는 agent 가 화면을 보고
`resume_macro` 로 잇는다. 여기에는 그 자리에 설 사람이 없고, executer 의 turn 이 같은 일을 한다
— func 하나가 결정 하나이고, executer 는 turn 마다 상태를 다시 읽고 다음 func 를 고른다. 그래서
host 는 `False` 로 답해 macro 를 거기서 멈추고, executer 는 그 turn 을 실패 없이 끝난 것
(`paused`)으로 센다. `decide` 가 `none_fits` 를 받아 멈춘 것은 그 func 의 `case` 가 지금 상태를
덮지 못한다는 뜻이라 실패로 센다.
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

from app.agents.planner_executer.g import GameHandle
from app.agents.qa.macro.binding import MacroMemories
from app.agents.qa.macro.model import MacroDefinition
from app.agents.qa.macro.runner import (
    MacroAskRequest,
    MacroDecideRequest,
    MacroJevUnavailable,
    MacroPulseWait,
    MacroRunResult,
    run_macro,
)
from app.agents.qa.tools.macro_tools import one_line_result
from app.qa.acting import ActionOutcome
from app.qa.envelope import JsonRpcAction
from app.qa.jev import JevError

# `ask` 의 참·거짓이 뜻하는 것. tool loop 의 `macro_tools._Host.ask` 와 같은 글이다.
ASK_TRUE_MEANS = "The answer to the question is yes."
ASK_FALSE_MEANS = "The answer to the question is no."

# func 하나를 돌리는 `TOOL` frame 이름. tool loop 의 macro 실행도 이 이름으로 남는다.
RUN_TOOL = "run_macro"
MAX_TOOL_RESULT_CHARS = 2000


class MacroJev(Protocol):
    """`decide`·`ask` 를 묻는 client: run 에서는 `JevClient`."""

    async def choose(self, instructions: str, options: dict[str, str], state: dict) -> str: ...

    async def probability(
        self, instructions: str, state: dict, true_means: str, false_means: str
    ) -> float: ...


@dataclass(frozen=True)
class FuncOutcome:
    """func 의 macro 한 번이 executer 에게 남긴 것."""

    kind: Literal["passed", "paused", "failed"]
    reason: str = ""


class GameMacroHost:
    """`run_macro` 가 바깥에 대고 하는 일을 `g` 와 Jev client 에 잇는다."""

    def __init__(self, g: GameHandle, jev: MacroJev | None) -> None:
        self._g = g
        self._jev = jev
        # `checkpoint` 나 `none_fits` 로 멈춘 자리: (무엇으로, 이유).
        self.paused: tuple[Literal["checkpoint", "none_fits"], str] | None = None

    async def run(self, actions: list[JsonRpcAction], summary: str, step: int) -> ActionOutcome:
        return await self._g._act(actions, summary)

    def memories(self) -> MacroMemories:
        return MacroMemories(scene=self._g._channel.scene)

    async def wait_for_pulse(self, timeout: float) -> MacroPulseWait:
        loop = asyncio.get_running_loop()
        started = loop.time()
        pulses = await self._g._channel.wait_for_reading(timeout)
        self._g._drain_operator()
        return MacroPulseWait(pulses, loop.time() - started)

    async def checkpoint(self, result: MacroRunResult) -> bool:
        kind = "none_fits" if result.paused_decision is not None else "checkpoint"
        self.paused = (kind, result.paused_reason)
        return False

    async def decide(self, request: MacroDecideRequest) -> str:
        options = {option.name: option.description for option in request.options}
        try:
            return await self._client().choose(request.question, options, request.state)
        except JevError as error:
            raise MacroJevUnavailable(str(error)) from error

    async def ask(self, request: MacroAskRequest) -> float:
        try:
            return await self._client().probability(
                request.question, request.state, true_means=ASK_TRUE_MEANS, false_means=ASK_FALSE_MEANS
            )
        except JevError as error:
            raise MacroJevUnavailable(str(error)) from error

    def _client(self) -> MacroJev:
        if self._jev is None:
            raise MacroJevUnavailable("this run has no Jev client")
        return self._jev


async def run_func(
    g: GameHandle, definition: MacroDefinition, jev: MacroJev | None, step: int
) -> FuncOutcome:
    """func 하나의 macro 를 돌리고, 그 결과를 executer 가 읽는 모양으로 낸다.

    timeline 에는 tool loop 가 `run_macro` 를 부를 때처럼 `TOOL`·`TOOL_RESULT` 한 쌍이 남고, 그
    사이의 action 은 statement 마다 `ACTION` frame 으로 나간다.
    """
    channel = g._channel
    host = GameMacroHost(g, jev)
    sent_from = len(g.actions)
    call_id = uuid.uuid4().hex[:12]
    await channel.tool_call(RUN_TOOL, call_id, {"name": definition.name, "source": definition.source}, step)
    content = f"{definition.name}: did not finish"
    try:
        result = await run_macro(host, definition, {}, step)
        for line in (*result.waits, *result.decisions):
            g.log(line)
        refused = [sent["error"] for sent in g.actions[sent_from:] if not sent["ok"] and sent["error"]]
        outcome = outcome_of(result, host.paused, refused)
        content = one_line_result(result, g.scene or "") + (
            f" {outcome.kind}: {outcome.reason}" if outcome.reason else ""
        )
        g.log(content)
        return outcome
    finally:
        await channel.tool_result(RUN_TOOL, call_id, content[:MAX_TOOL_RESULT_CHARS], step)


def outcome_of(
    result: MacroRunResult,
    paused: tuple[str, str] | None,
    refused_actions: list[str],
) -> FuncOutcome:
    """`MacroRunResult` 를 func 의 결과 하나로. macro 가 멈춘 이유가 먼저고, 그다음 `none_fits`,
    게임이 거절한 action, checkpoint 순이다."""
    if result.failure is not None:
        return FuncOutcome(
            "failed", f"{result.failure.code} at {result.stopped_at}: {result.failure.reason}"
        )
    if paused is not None and paused[0] == "none_fits":
        return FuncOutcome("failed", f"`decide` found no case that fits the state ({paused[1]})")
    if refused_actions:
        return FuncOutcome("failed", "the game refused an input: " + "; ".join(refused_actions))
    if paused is not None:
        return FuncOutcome("paused", f"stopped at a checkpoint: {paused[1]}")
    return FuncOutcome("passed")
