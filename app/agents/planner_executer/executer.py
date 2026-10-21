"""The executer: plays one step's macros with Jev until it stops.

Each turn:

1. stop at `max_turns_per_step` or `step_budget_seconds`;
2. read `done()` on the current screen. When it holds, ask Jev whether the step's goal has really
   been reached. Agreed — stop, `done`. Not agreed — note a disagreement and go on;
   `max_done_disagreements` of them stop play;
3. ask Jev which action to run now: the actions' docstrings are the options, with `none_fits`;
4. `none_fits` — wait for the next reading, and stop once nothing has fit for
   `no_action_grace_seconds`. Otherwise run the action with the macro runner; `max_failures_in_a_row`
   failed actions stop play;
5. record the turn and send it as a `LOG`.

Jev 에 묻는 두 질문은 macro 의 `ask`·`decide` 와 같은 모양이다: state 는 keyword 없는 `ask`·`decide`
가 보내는 것(`build_context_state`: scene 과 화면의 live object 전부)이고, 선택지 끝에는
`none_fits` 가 붙고, `ask` 는 `ASK_TRUE_AT` 이상이면 참이다. 질문을 만드는 것은 이 모듈이고, 보내는
것은 macro runner 의 host 가 쓰는 `app/qa/jev.py` 의 `JevClient` 다. Jev 가 답하지 못하면 그 step 의
play 를 그 오류로 멈춘다 — 대신 아무거나 고르면 무엇을 잰 것인지 알 수 없게 된다.
"""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.macro_host import ASK_FALSE_MEANS, ASK_TRUE_MEANS, ChannelMacroHost
from app.agents.planner_executer.macros import PROBE_FLAG, StepMacros
from app.agents.planner_executer.prompts import screen
from app.agents.qa.macro.binding import MacroMemories, MacroScope
from app.agents.qa.macro.context import build_context_state
from app.agents.qa.macro.grammar import ASK_TRUE_AT, NONE_FITS, NONE_FITS_DESCRIPTION
from app.agents.qa.macro.model import MacroDefinition
from app.agents.qa.macro.runner import run_macro
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import LogCategory, LogPayload, MessageType
from app.qa.jev import JevClient, JevError

GOAL_QUESTION = "Has this step's goal been reached?"
PICK_QUESTION = "Which of these should the player do now to progress this step?"

# How long the executer waits for a reading when nothing fits, before it asks again.
IDLE_LOOK_SECONDS = 1.0
# Readings come in bursts and are applied one frame at a time. A decision taken between two of
# them decides on the past, so each turn first lets the burst land: it waits until no reading has
# landed for `SETTLE_QUIET_SECONDS`, at most `SETTLE_ROUNDS` times.
SETTLE_QUIET_SECONDS = 0.15
SETTLE_ROUNDS = 8

# A failure reason as kept in the turn record.
MAX_REASON_CHARS = 400
# 같은 씬에서 아무것도 보내지 못하고 이만큼 실패한 action 은 그 씬을 떠날 때까지 선택지에서 뺀다(ARTEL-1189).
# 보낸 것이 없으면 게임이 그대로이니 다시 골라도 같은 자리에서 실패한다. 한 번은 봐준다 — 첫 reading 이
# 아직 안 온 `SELECTOR_NOT_FOUND` 처럼 곧 풀리는 실패가 있다.
SENDLESS_FAILURES_BEFORE_DROP = 2
# 입력을 보냈는데도 실패한 것까지 합쳐 같은 씬에서 이만큼 실패한 action 도 뺀다. 성공한 다른 action 이 사이에
# 끼면 연속 실패 수가 0 으로 돌아가므로, 보내고 실패한 action 도 같은 자리에서 수십 번 돌았다
# (`close_combine_zone` 이 시간 초과를 40번 넘게, 로컬 try 169).
FAILURES_BEFORE_DROP = 3
# Largest turn record sent as one `LOG`. Orchestration's socket closes on frames over 64 KiB.
MAX_TURN_LOG_BYTES = 48_000


class TurnLogPayload(LogPayload):
    """One executer turn as a `LOG` with a structured body. `kind` picks turns out of the logs."""

    kind: str = "turn"
    turn: dict[str, Any] = Field(default_factory=dict)


@dataclass
class StepPlay:
    """How play of one step went, for the evaluator."""

    stopped: str
    turns: list[dict] = field(default_factory=list)
    start_screen: str = ""
    end_screen: str = ""
    seconds: float = 0.0


class JevStopped(Exception):
    """Jev did not answer; the step's play stops with this."""


class Executer:
    """Plays one scenario's steps, one at a time, over the run's channel."""

    def __init__(
        self,
        channel: QaRunChannel,
        spec: PlannerExecuterArchSpec,
        jev: JevClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._channel = channel
        self._spec = spec
        self._jev = jev
        self._clock = clock

    async def play(self, step: int, action: str, macros: StepMacros) -> StepPlay:
        """Play step `step` (`action` is its text) with `macros` until it stops."""
        channel, spec, clock = self._channel, self._spec, self._clock
        await self._settle()
        play = StepPlay("turn limit", start_screen=screen(channel.scene.pulse))
        started = clock()
        none_since: float | None = None
        failures = disagreements = 0
        # action 이름 → 지금 씬에서 아무것도 못 보내고 실패한 횟수와, 보냈든 안 보냈든 실패한 횟수. 씬이 바뀌면 비운다.
        sendless: dict[str, int] = {}
        failed: dict[str, int] = {}
        sendless_scene = self._scene()
        try:
            for turn in range(spec.max_turns_per_step):
                await self._settle()
                now = clock()
                if now - started > spec.step_budget_seconds:
                    play.stopped = f"step budget {spec.step_budget_seconds:g}s"
                    break
                record: dict[str, Any] = {"step": step, "turn": turn, "scene": self._scene()}
                play.turns.append(record)
                record["done"], done_error = await self._done(macros.probe, step)
                if done_error:
                    record["done_error"] = done_error
                if record["done"]:
                    probability = await self._ask_goal(action)
                    record["goal_ask"] = round(probability, 3)
                    if probability >= ASK_TRUE_AT:
                        await self._send(record)
                        play.stopped = "done"
                        break
                    disagreements += 1
                    if disagreements >= spec.max_done_disagreements:
                        await self._send(record)
                        play.stopped = (
                            f"done() and Jev disagree: done() held {disagreements} times and Jev "
                            "did not see the goal reached"
                        )
                        break
                if record["scene"] != sendless_scene:
                    sendless, failed, sendless_scene = {}, {}, record["scene"]
                dropped = sorted(
                    {name for name, count in sendless.items() if count >= SENDLESS_FAILURES_BEFORE_DROP}
                    | {name for name, count in failed.items() if count >= FAILURES_BEFORE_DROP}
                )
                options = {name: macros.description(name) for name in macros.actions if name not in dropped}
                if dropped:
                    record["dropped"] = dropped
                if not options:
                    await self._send(record)
                    play.stopped = f"every action kept failing here: {', '.join(dropped)}"
                    break
                record["question"] = f"{PICK_QUESTION} The step: {action}"
                record["options"] = list(options)
                record["picked"] = picked = await self._pick(record["question"], options)
                if picked == NONE_FITS:
                    await self._send(record)
                    none_since = none_since or now
                    remaining = spec.no_action_grace_seconds - (now - none_since)
                    if remaining <= 0:
                        play.stopped = f"no action fit for {spec.no_action_grace_seconds:g}s"
                        break
                    await channel.wait_for_reading(min(remaining, IDLE_LOOK_SECONDS))
                    continue
                none_since = None
                outcome, reason, sent = await self._run(macros.actions[picked], step)
                record.update(outcome=outcome, reason=reason, sent=sent)
                await self._send(record)
                failures = failures + 1 if outcome == "failed" else 0
                if outcome == "failed":
                    failed[picked] = failed.get(picked, 0) + 1
                if outcome == "failed" and not sent:
                    # 성공한 다른 action 이 사이에 끼면 `failures` 는 0 으로 돌아간다. 그래서 이 action 이
                    # 같은 자리에서 수십 번 실패해도 위 상한에 안 걸렸다(로컬 try 166). 이름으로 따로 센다.
                    sendless[picked] = sendless.get(picked, 0) + 1
                if failures >= spec.max_failures_in_a_row:
                    play.stopped = f"{failures} actions failed in a row (last: {picked}: {reason})"
                    break
                if not sent:
                    # 보낸 것이 없으면 화면도 그대로다. 다음 reading 을 기다려 같은 화면에 바로 다시
                    # 묻지 않는다. 보낸 것이 있으면 `act_and_look` 이 이미 기다렸다.
                    await channel.wait_for_reading(IDLE_LOOK_SECONDS)
        except JevStopped as error:
            play.stopped = f"JEV-ERROR: {error}"
            if play.turns:
                play.turns[-1]["jev_error"] = str(error)
                await self._send(play.turns[-1])
        play.end_screen = screen(channel.scene.pulse)
        play.seconds = round(clock() - started, 1)
        return play

    async def _done(self, probe: MacroDefinition, step: int) -> tuple[bool, str | None]:
        """`done()` on the current screen, through the probe entry (`macros.PROBE`)."""
        host = ChannelMacroHost(self._channel, self._jev, step)
        result = await run_macro(host, probe, {}, step)
        if result.failure is not None:
            return False, f"{result.failure.code}: {result.failure.reason}"[:MAX_REASON_CHARS]
        return any(flagged.message == PROBE_FLAG for flagged in result.flags), None

    def _state(self) -> dict[str, Any]:
        """What a keyword-less macro `ask`/`decide` sends Jev: the scene and every live object."""
        state, _clipped = build_context_state(MacroMemories(scene=self._channel.scene), MacroScope(), ())
        return state

    async def _ask_goal(self, action: str) -> float:
        try:
            return await self._jev.probability(
                f"{GOAL_QUESTION} The step: {action}",
                self._state(),
                true_means=ASK_TRUE_MEANS,
                false_means=ASK_FALSE_MEANS,
            )
        except JevError as error:
            raise JevStopped(str(error)) from error

    async def _pick(self, question: str, options: dict[str, str]) -> str:
        try:
            return await self._jev.choose(
                question,
                {**options, NONE_FITS: NONE_FITS_DESCRIPTION},
                self._state(),
            )
        except JevError as error:
            raise JevStopped(str(error)) from error

    async def _run(self, definition: MacroDefinition, step: int) -> tuple[str, str | None, list[dict]]:
        """Run one action. `(outcome, reason, batches sent)`; outcome is passed, paused or failed."""
        host = ChannelMacroHost(self._channel, self._jev, step)
        try:
            result = await run_macro(host, definition, {}, step)
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - noted against the action, play goes on
            return "failed", f"{type(error).__name__}: {str(error)[:200]}", host.sent
        if result.failure is not None:
            reason = f"{result.failure.code} at {result.stopped_at}: {result.failure.reason}"
            return "failed", reason[:MAX_REASON_CHARS], host.sent
        refused = [sent["error"] for sent in host.sent if not sent["ok"] and sent["error"]]
        if refused:
            return "failed", ("the game refused an input: " + "; ".join(refused))[:MAX_REASON_CHARS], host.sent
        if host.paused is not None:
            outcome = "failed" if host.paused.startswith("`decide`") else "paused"
            return outcome, host.paused[:MAX_REASON_CHARS], host.sent
        return "passed", None, host.sent

    def _scene(self) -> str | None:
        return self._channel.scene.pulse.scene

    async def _settle(self) -> None:
        pulse = self._channel.scene.pulse
        for _ in range(SETTLE_ROUNDS):
            seen = pulse.readings
            await asyncio.sleep(SETTLE_QUIET_SECONDS)
            if pulse.readings == seen:
                return

    async def _send(self, record: dict) -> None:
        await self._channel.emit(MessageType.LOG, _turn_log(record))


def _turn_log(record: dict) -> TurnLogPayload:
    """The turn as a `LOG`: a readable line, and the record itself."""
    parts = [f"turn {record['turn']}", f"done={record.get('done')}"]
    if "goal_ask" in record:
        parts.append(f"goal ask {record['goal_ask']}")
    if "picked" in record:
        parts.append(f"picked {record['picked']}")
    if "outcome" in record:
        parts.append(record["outcome"] + (f": {record['reason']}" if record.get("reason") else ""))
    body = json.loads(json.dumps(record, default=str))
    if len(json.dumps(body, ensure_ascii=False).encode()) > MAX_TURN_LOG_BYTES:
        body = {key: record.get(key) for key in ("step", "turn", "done", "picked", "outcome")}
    return TurnLogPayload(
        category=LogCategory.OBSERVATION, message=", ".join(parts), step=record.get("step"), turn=body
    )
