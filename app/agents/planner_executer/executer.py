"""The executer: plays one step's macros with Jev until it stops.

Each turn:

1. stop at `max_turns_per_step` or `step_budget_seconds`;
2. read `done()` on the current screen. When it holds, ask Jev whether the step's goal has really
   been reached. Agreed — stop, `done`. Not agreed — note a disagreement and go on;
   `max_done_disagreements` of them stop play;
3. ask Jev which action to run now: the actions' docstrings are the options, with `none_fits`;
4. `none_fits` — wait for the next reading, and stop once nothing has fit for
   `no_action_grace_seconds`. Otherwise run the action with the macro runner; `max_failures_in_a_row`
   failed actions stop play, and so do `max_idle_turns_in_a_row` actions that ran without sending the
   game an input (ARTEL-1288);
5. before those stops, when one action has failed or sent nothing `repair_after` times in a row and
   the play has a `repair` (the editor, `app/agents/editor/`), call it with that action's turn records
   and go on with the macros it returns — up to `max_repairs_per_step` times a step;
6. record the turn and send it as a `LOG`.

Jev 에 묻는 두 질문은 macro 의 `ask`·`decide` 와 같은 모양이다: state 는 keyword 없는 `ask`·`decide`
가 보내는 것(`build_context_state`: scene 과 화면의 live object 전부)이고, 선택지 끝에는
`none_fits` 가 붙고, `ask` 는 `ASK_TRUE_AT` 이상이면 참이다. 질문을 만드는 것은 이 모듈이고, 보내는
것은 macro runner 의 host 가 쓰는 `app/qa/jev.py` 의 `JevClient` 다. Jev 가 답하지 못하면 그 step 의
play 를 그 오류로 멈춘다 — 대신 아무거나 고르면 무엇을 잰 것인지 알 수 없게 된다.
"""

import asyncio
import json
import time
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

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

# What the executer calls to have a stuck action's macros rewritten: `(macros, action, its turn
# records)` → the rewrite, or `None` when it was refused to the end (`app.agents.editor.Repair`).
Repair = Callable[[StepMacros, str, list[dict]], Awaitable[StepMacros | None]]

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
# Largest turn record sent as one `LOG`. Orchestration's socket closes on frames over 64 KiB.
MAX_TURN_LOG_BYTES = 48_000


class TurnLogPayload(LogPayload):
    """One executer turn as a `LOG` with a structured body. `kind` picks turns out of the logs."""

    kind: str = "turn"
    turn: dict[str, Any] = Field(default_factory=dict)


class ActionFlag(BaseModel):
    """macro가 남긴 위치·메시지·관측값을 turn과 editor에 그대로 전달한다."""

    place: str
    message: str
    observed: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ActionRun:
    outcome: str
    reason: str | None
    sent: list[dict]
    flags: list[ActionFlag] = field(default_factory=list)


@dataclass
class StepPlay:
    """How play of one step went, for the evaluator."""

    stopped: str
    turns: list[dict] = field(default_factory=list)
    start_screen: str = ""
    end_screen: str = ""
    seconds: float = 0.0
    # SDK 의 frame 번호(`pulse` 의 마지막 reading 이 말한 것)와 벽시계. play 가 시작할 때와 멈출 때의 값이다.
    # 영상에서 이 step 의 구간을 자르는 기준이 된다(ARTEL-1267) — 판정은 play 가 끝난 뒤 모델이 답한 시각이라
    # 게임 frame 이 아니고, 구간은 여기서 끝난다. reading 이 아직 없으면 frame 은 `None` 이다.
    start_frame: int | None = None
    end_frame: int | None = None
    started_at: str = ""
    ended_at: str = ""

    def played(self) -> dict:
        """step `STATUS` 의 `played`: 영상 구간을 자를 기준. `docs/evaluator-analysis-frames.md`."""
        return {
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "seconds": self.seconds,
            "stopped": self.stopped,
        }

    def reached_goal(self) -> bool:
        """Whether play got to the step's goal by its own reading: `done()` held on some turn.

        The engine's claim, not the verdict. The Evaluator records it beside its own answer so
        the two can be compared.
        """
        return self.stopped == "done" or any(turn.get("done") for turn in self.turns)


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

    async def play(
        self, step: int, action: str, macros: StepMacros, repair: Repair | None = None
    ) -> StepPlay:
        """Play step `step` (`action` is its text) with `macros` until it stops.

        `repair` is the editor for this step, or `None` when the play has none (a FREE case's play:
        its macros are a scene plan shared by several cases, which the editor does not rewrite).
        """
        channel, spec, clock = self._channel, self._spec, self._clock
        await self._settle()
        play = StepPlay(
            "turn limit",
            start_screen=screen(channel.scene.pulse),
            start_frame=channel.scene.pulse.frame,
            started_at=_now(),
        )
        started = clock()
        none_since: float | None = None
        failures = disagreements = idle_turns = repairs = 0
        # The action the last turns ran, and how many times in a row it failed or sent nothing.
        stuck: tuple[str | None, int] = (None, 0)
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
                options = {name: macros.description(name) for name in macros.actions}
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
                action_run = await self._run(macros.actions[picked], step)
                outcome, reason, sent = action_run.outcome, action_run.reason, action_run.sent
                record.update(
                    outcome=outcome, reason=reason, sent=sent,
                    flags=[flagged.model_dump(mode="json") for flagged in action_run.flags],
                )
                failures = failures + 1 if outcome == "failed" else 0
                idle = outcome == "passed" and not sent
                idle_turns = idle_turns + 1 if idle else 0
                stuck = (picked, stuck[1] + 1 if stuck[0] == picked else 1) if outcome == "failed" or idle else (None, 0)
                if stuck[1] >= spec.repair_after and repair is not None and repairs < spec.max_repairs_per_step:
                    # 멈춤 규칙보다 먼저다: 그 action 의 turn 기록을 들고 editor 를 부르고, 답이 오면 그것으로 잇는다.
                    repairs += 1
                    evidence = [turn for turn in play.turns if turn.get("picked") == picked][-stuck[1] :]
                    fixed = await repair(macros, picked, evidence)
                    record["repair"] = {"action": picked, "attempt": repairs, "replaced": fixed is not None}
                    await self._send(record)
                    if fixed is not None:
                        macros = fixed
                        failures = idle_turns = 0
                        stuck = (None, 0)
                        continue
                else:
                    await self._send(record)
                if failures >= spec.max_failures_in_a_row:
                    play.stopped = f"{failures} actions failed in a row (last: {picked}: {reason})"
                    break
                if idle_turns >= spec.max_idle_turns_in_a_row:
                    play.stopped = (
                        f"no progress: {idle_turns} actions in a row ran without sending the game an input "
                        f"(last: {picked})"
                    )
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
        play.end_frame = channel.scene.pulse.frame
        play.ended_at = _now()
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

    async def _run(self, definition: MacroDefinition, step: int) -> ActionRun:
        """실패나 pause 전까지 기록된 flags도 실행 결과와 함께 보존한다."""
        host = ChannelMacroHost(self._channel, self._jev, step)
        try:
            result = await run_macro(host, definition, {}, step)
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - noted against the action, play goes on
            return ActionRun("failed", f"{type(error).__name__}: {str(error)[:200]}", host.sent)
        flags = [
            ActionFlag(place=str(flagged.place), message=flagged.message, observed=flagged.observed)
            for flagged in result.flags
        ]
        if result.failure is not None:
            reason = f"{result.failure.code} at {result.stopped_at}: {result.failure.reason}"
            return ActionRun("failed", reason[:MAX_REASON_CHARS], host.sent, flags)
        refused = [sent["error"] for sent in host.sent if not sent["ok"] and sent["error"]]
        if refused:
            return ActionRun("failed", ("the game refused an input: " + "; ".join(refused))[:MAX_REASON_CHARS], host.sent, flags)
        if host.paused is not None:
            outcome = "failed" if host.paused.startswith("`decide`") else "paused"
            return ActionRun(outcome, host.paused[:MAX_REASON_CHARS], host.sent, flags)
        return ActionRun("passed", None, host.sent, flags)

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
    if "repair" in record:
        parts.append("macros " + ("rewritten" if record["repair"]["replaced"] else "rewrite refused"))
    body = json.loads(json.dumps(record, default=str))
    if len(json.dumps(body, ensure_ascii=False).encode()) > MAX_TURN_LOG_BYTES:
        body = {key: record.get(key) for key in ("step", "turn", "done", "picked", "outcome")}
    return TurnLogPayload(
        category=LogCategory.OBSERVATION, message=", ".join(parts), step=record.get("step"), turn=body
    )


def _now() -> str:
    """UTC ISO 8601, 초 단위. orchestration 의 envelope `timestamp` 와 같은 눈금이다."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
