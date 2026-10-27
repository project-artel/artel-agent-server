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

Jev 에 묻는 두 질문은 macro 의 `ask`·`decide` 와 같은 모양이다: state 는 `jev_state.assemble` 이 만든 것
(feature, action 이 읽는 object 와 static 을 먼저 싣고 남는 자리에 나머지)이고, 선택지 끝에는
`none_fits` 가 붙고, `ask` 는 `ASK_TRUE_AT` 이상이면 참이다. 질문을 만드는 것은 이 모듈이고, 보내는
것은 macro runner 의 host 가 쓰는 `app/qa/jev.py` 의 `JevClient` 다. Jev 가 답하지 못하면 그 step 의
play 를 그 오류로 멈춘다 — 대신 아무거나 고르면 무엇을 잰 것인지 알 수 없게 된다.
"""

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.jev_state import (
    JevStateFilter,
    Reads,
    assemble,
    changed_values,
    definition_sources,
    reads_of,
    unfiltered,
    watched_values,
)
from app.agents.planner_executer.macro_host import ASK_FALSE_MEANS, ASK_TRUE_MEANS, ChannelMacroHost
from app.agents.planner_executer.macros import FEATURE_PREFIX, PROBE_FLAG, StepMacros
from app.agents.planner_executer.prompts import screen
from app.agents.qa.macro.binding import MacroMemories
from app.agents.qa.macro.grammar import ASK_TRUE_AT, NONE_FITS
from app.agents.qa.macro.model import MacroDefinition
from app.agents.qa.macro.errors import INPUT_BLOCKED, MacroFailure
from app.agents.qa.macro.runner import evaluate_value, run_macro
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import LogCategory, LogPayload, MessageType
from app.qa.jev import JevClient, JevError

GOAL_QUESTION = "Has this step's goal been reached?"
# 판단 순서를 적는다(JEV-Star 의 macro instructions 처럼). 순서가 없을 때 Jev 는 공격할 수 있는 턴에 턴 종료를,
# 시전한 뒤에는 기다림을 골랐다(로컬 try 184 재현).
PICK_QUESTION = (
    "Which of these should the player do now to progress this step? Judge in this order: 1) clear what "
    "blocks input (an open dialogue, a tutorial prompt, a confirmation); 2) an action that makes progress "
    "now; 3) wait only when no other option can be done. Read `recent_picks`: do not repeat an action that "
    "just failed, or whose `changed` shows it moved none of the values the actions read — an action that "
    "succeeds every time but changes nothing toward the goal is not progress."
)
# `none_fits` 가 이 executer 에서 뜻하는 것: 이번 턴은 아무것도 하지 않는다.
WAIT_DESCRIPTION = "WAIT — do nothing this turn; only when no other option can be done now"
# state 에 싣는 최근 선택 수.
RECENT_PICKS = 3
# 최근 선택 하나에 싣는, action 앞뒤로 바뀐 값의 수.
MAX_CHANGED_VALUES = 8

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
# play 하나에서 한 action 을 고를 수 있는 횟수. 실패 없이 "통과" 하면서 진전이 없는 action 은 위의 어느 상한에도
# 안 걸린다 — 로컬 FREE 런에서 `move_on_map` 이 215번, `toggle_combine_panel` 이 200번 돌며 play 의 시간
# 상한(8분)을 다 썼다(try 167·170). 정상 반복은 대사 넘기기가 많게는 40번, 나머지는 23번 이하였다.
MAX_PICKS_PER_ACTION = 30
# 입력을 보내고 "통과" 했는데 화면 요약(`screen`)이 그대로인 일이 같은 씬에서 연달아 이만큼이면 그 action 도 뺀다.
# 로컬 try 172 에서 `move_on_map`(오른쪽 방향키)이 잠긴 다음 칸 앞에서 화면을 바꾸지 못한 채 30번씩 두 번 돌았다 —
# 통과한 action 60번 중 58번이 그랬고, 다른 action 은 거의 매번 화면을 바꿨다. 바뀌면 다시 0 부터 센다.
# 누름이 하나도 닿지 않은 것(`reached` 가 모두 "nothing")도 같이 센다. 전투처럼 애니메이션이 늘 도는 화면에서는
# 화면 요약이 매번 바뀌어 위 기준이 듣지 않는다. 로컬 try 180 에서 카드의 글자 자식을 끄는 developer macro 가
# 누름이 매번 빈 자리에 떨어진 채 601번 통과했다.
UNCHANGED_BEFORE_DROP = 3
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

    def reached_goal(self) -> bool:
        """Whether play got to the step's goal by its own reading: `done()` held on some turn.

        The engine's claim, not the verdict. The Evaluator records it beside its own answer so
        the two can be compared.
        """
        return self.stopped == "done" or any(turn.get("done") for turn in self.turns)


@dataclass
class FailureMemory:
    """같은 씬에서 action 마다 몇 번 실패했는지. 씬이 바뀌면 비운다(ARTEL-1189).

    FREE 의 씬 계획 하나는 여러 case 가 같은 action 을 나눠 쓴다. play 마다 새로 세면 case 가 넘어갈 때마다
    같은 실패를 두세 번 되풀이한다 — 로컬 try 171 에서 `drag_shoot_card_to_zone` 이 case 마다 두 번씩
    `SELECTOR_AMBIGUOUS` 로 실패했다. 계획 하나의 case 들에 같은 기억을 넘긴다.
    """

    scene: str | None = None
    sendless: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    # action 이름 → 보내고 통과했는데 화면이 그대로였던 연속 횟수.
    unchanged: dict[str, int] = field(default_factory=dict)

    def at(self, scene: str | None) -> None:
        if scene != self.scene:
            self.scene, self.sendless, self.failed, self.unchanged = scene, {}, {}, {}


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
        self._state_filter = JevStateFilter(jev) if spec.jev_state == "members" else None

    async def play(
        self, step: int, action: str, macros: StepMacros, failure_memory: FailureMemory | None = None
    ) -> StepPlay:
        """Play step `step` (`action` is its text) with `macros` until it stops."""
        channel, spec, clock = self._channel, self._spec, self._clock
        await self._settle()
        play = StepPlay("turn limit", start_screen=screen(channel.scene.pulse))
        started = clock()
        none_since: float | None = None
        failures = disagreements = 0
        # action 이름 → 지금 씬에서 아무것도 못 보내고 실패한 횟수와, 보냈든 안 보냈든 실패한 횟수. 씬이 바뀌면 비운다.
        # 넘겨받으면 같은 계획의 앞 case 가 센 것을 이어 쓴다.
        memory = failure_memory if failure_memory is not None else FailureMemory()
        memory.at(self._scene())
        # action 이름 → 이 play 에서 고른 횟수. 씬이 바뀌어도 비우지 않는다.
        picks: dict[str, int] = {}
        # 최근 선택과 결과. Jev 가 같은 실패를 되풀이하지 않게 state 에 싣는다.
        recent: list[dict[str, str]] = []
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
                    probability = await self._ask_goal(action, macros, step)
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
                memory.at(record["scene"])
                dropped = sorted(
                    {name for name, count in memory.sendless.items() if count >= SENDLESS_FAILURES_BEFORE_DROP}
                    | {name for name, count in memory.failed.items() if count >= FAILURES_BEFORE_DROP}
                    | {name for name, count in picks.items() if count >= MAX_PICKS_PER_ACTION}
                    | {name for name, count in memory.unchanged.items() if count >= UNCHANGED_BEFORE_DROP}
                )
                remaining = [name for name in macros.actions if name not in dropped]
                if dropped:
                    record["dropped"] = dropped
                if not remaining:
                    await self._send(record)
                    play.stopped = (
                        f"every action kept failing, left the screen as it was, or ran out of picks here: {', '.join(dropped)}"
                    )
                    break
                infeasible = [name for name in remaining if not await self._can(macros, name, step)]
                if infeasible:
                    record["infeasible"] = infeasible
                options = {name: macros.label(name) for name in remaining if name not in infeasible}
                record["question"] = f"{PICK_QUESTION} The step: {action}"
                record["options"] = list(options)
                reads = self._reads(macros, options)
                if options:
                    record["picked"] = picked = await self._pick(record, action, macros, options, recent, reads)
                else:
                    # 할 수 있는 것이 없다. 물을 것 없이 기다린다.
                    record["picked"] = picked = NONE_FITS
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
                picks[picked] = picks.get(picked, 0) + 1
                before = screen(channel.scene.pulse)
                watched = self._watched(macros, reads, step)
                outcome, reason, sent = await self._run(macros.actions[picked], step)
                # action 이 읽는 값 가운데 무엇이 바뀌었나. Jev 는 이것으로 "성공했지만 아무것도 못 바꾼" action 을 안다
                # (try 193 에서 턴 종료는 매번 성공했고, 시전 없이 7번 골라졌다).
                changed = changed_values(watched, self._watched(macros, reads, step), MAX_CHANGED_VALUES)
                record["changed"] = changed
                record.update(outcome=outcome, reason=reason, sent=sent)
                # 가로막힘은 무엇이 막았는지까지 남긴다. 다음 턴의 Jev 가 그것을 치울 action 을 고르게 한다.
                blocker = next((batch["blocked"] for batch in reversed(sent) if batch.get("blocked")), None)
                said = f"blocked by {blocker or 'something on screen'}" if outcome == "blocked" else outcome
                recent = [
                    *recent,
                    {"action": macros.label(picked), "outcome": said, "changed": changed or "nothing the actions read"},
                ][-RECENT_PICKS:]
                if outcome == "passed" and not sent:
                    # 아무 입력도 보내지 않고 통과했다 — "대사가 열려 있으면 Space" 같은 macro 가 대사가 없을 때다. 그대로
                    # 두면 같은 action 을 계속 고르며 시간만 간다(로컬 try 191 의 `dismiss_dialogue` 30번, try 184 의
                    # `close_settings` 30번). 화면이 그대로인 통과와 같이 센다.
                    record["no_input"] = True
                    memory.unchanged[picked] = memory.unchanged.get(picked, 0) + 1
                if outcome == "passed" and sent:
                    presses = [batch["reached"] for batch in sent if batch.get("reached") is not None]
                    if presses and all(reached == "nothing" for reached in presses):
                        record["missed"] = True
                    if screen(channel.scene.pulse) == before:
                        record["unchanged"] = True
                    if record.get("missed") or record.get("unchanged"):
                        memory.unchanged[picked] = memory.unchanged.get(picked, 0) + 1
                    else:
                        memory.unchanged[picked] = 0
                await self._send(record)
                # 가로막힘은 실패로 세지 않고, 연속 실패도 끊지 않는다 — 막은 것을 치운 뒤 같은 action 을 다시 고를 수 있어야 한다.
                if outcome != "blocked":
                    failures = failures + 1 if outcome == "failed" else 0
                if outcome == "failed":
                    memory.failed[picked] = memory.failed.get(picked, 0) + 1
                # 게임이 하나도 받지 않은 실패도 보내지 않은 것과 같다. SDK 는 실패한 action 뒤의 나머지를 돌리지 않으므로,
                # 조준이 거절된 click 은 아무것도 누르지 않는다(로컬 try 176 의 `skip_tutorial`).
                if outcome == "failed" and not any(batch.get("ok") for batch in sent):
                    # 성공한 다른 action 이 사이에 끼면 `failures` 는 0 으로 돌아간다. 그래서 이 action 이
                    # 같은 자리에서 수십 번 실패해도 위 상한에 안 걸렸다(로컬 try 166). 이름으로 따로 센다.
                    memory.sendless[picked] = memory.sendless.get(picked, 0) + 1
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

    def _reads(self, macros: StepMacros, actions: Iterable[str] | None = None) -> Reads:
        """계획(판단 macro 포함)과 `actions` 의 macro 몸통이 읽는 이름. `actions` 가 없으면 모든 action 이다."""
        names = macros.actions if actions is None else actions
        sources = [macros.source]
        for name in names:
            sources.extend(definition_sources(macros.actions[name]))
        return reads_of(sources)

    def _state(self, macros: StepMacros, step: int) -> dict[str, Any]:
        """거르지 않은 state: action 이 읽는 값, static, feature 가 먼저다(`jev_state.assemble`)."""
        memories = MacroMemories(scene=self._channel.scene)
        reads = self._reads(macros)
        features = self._features(macros, step) if macros.features else None
        state, _note = assemble(memories, unfiltered(memories, reads), reads, features)
        return state

    def _watched(self, macros: StepMacros, reads: Reads, step: int) -> dict[str, Any]:
        watched = watched_values(MacroMemories(scene=self._channel.scene), reads)
        watched["scene"] = self._scene()
        if macros.features:
            watched.update({f"feature:{name}": value for name, value in self._features(macros, step).items()})
        return watched

    async def _ask_goal(self, action: str, macros: StepMacros, step: int) -> float:
        try:
            return await self._jev.probability(
                f"{GOAL_QUESTION} The step: {action}",
                self._state(macros, step),
                true_means=ASK_TRUE_MEANS,
                false_means=ASK_FALSE_MEANS,
            )
        except JevError as error:
            raise JevStopped(str(error)) from error

    async def _can(self, macros: StepMacros, action: str, step: int) -> bool:
        """`can_<action>` 이 참인지. 없으면 참이다. 읽다가 실패하면 할 수 있는 것으로 둔다 — 근거 없이 빼지 않는다."""
        probe = macros.feasible.get(action)
        if probe is None:
            return True
        holds, error = await self._done(probe, step)
        return holds or error is not None

    async def _pick(
        self,
        record: dict,
        action: str,
        macros: StepMacros,
        options: dict[str, str],
        recent: list[dict[str, Any]],
        reads: Reads,
    ) -> str:
        memories = MacroMemories(scene=self._channel.scene)
        features = None
        if macros.features:
            # planner 가 쓴 판단용 값. object 목록을 대신하지 않고 앞에 보탠다(JEV-Star 의 구조화된 관측).
            record["features"] = features = self._features(macros, record["step"])
        if self._state_filter is None:
            objects, filtered = unfiltered(memories, reads), None
        else:
            objects, filtered = await self._state_filter.objects(memories, action, macros, options, reads)
        state, note = assemble(memories, objects, reads, features)
        if filtered is not None:
            note["filter"] = filtered
        # Jev 가 받은 것을 그대로 남긴다. 요약만 남기던 때는 무엇이 빠졌는지 감사가 볼 수 없었다.
        record["jev_state"] = note
        record["state"] = state
        state = {**state, "recent_picks": recent}
        try:
            answer = await self._jev.choose_scored(
                record["question"],
                {**options, NONE_FITS: WAIT_DESCRIPTION},
                state,
            )
        except JevError as error:
            raise JevStopped(str(error)) from error
        if answer.probabilities:
            record["scores"] = {name: round(value, 3) for name, value in answer.probabilities.items()}
        if answer.confidence is not None:
            record["confidence"] = round(answer.confidence, 3)
        return answer.choice

    def _features(self, macros: StepMacros, step: int) -> dict[str, Any]:
        """`feature_*` 를 지금 화면으로 계산한다. 못 읽은 값은 그 이유의 앞부분을 적는다 — 빼면 Jev 가 없는 값을 거짓으로 읽는다."""
        host = ChannelMacroHost(self._channel, self._jev, step)
        values: dict[str, Any] = {}
        for name in macros.features:
            key = name.removeprefix(FEATURE_PREFIX)
            try:
                values[key] = evaluate_value(host, macros.probe, name)
            except (MacroFailure, ValueError) as error:
                values[key] = f"unreadable: {str(error)[:80]}"
        return values

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
            if result.failure.code == INPUT_BLOCKED:
                # 게임이 그 사이 띄운 것이 막았다. action 의 잘못이 아니다.
                return "blocked", reason[:MAX_REASON_CHARS], host.sent
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
