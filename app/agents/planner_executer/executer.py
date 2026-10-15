"""The executer: plays one step module turn by turn until the step stops.

A step module defines, at top level:

    def when_<name>(g) -> bool     # this func can be done and is still needed now
    run_<name> = '''def run_<name>() -> None: ...'''   # its inputs, as a macro
    FUNCS = {"<name>": ("<what it does, and when>", when_<name>, run_<name>), ...}
    def done(g) -> bool            # the step's goal is reached
    def lost(g)                    # optional: a reason once the goal can no longer be reached
    async def check(g)             # the verdict, run by the evaluator after play stops

Each turn: stop if `done` holds or `lost` gives a reason, collect the funcs whose `when` is true,
pick one, run it with the macro runner (`macro_host.py`), and record the turn. 하나뿐이면 그것을
돌리고, 둘 이상이면 macro 의 `match decide(...)` 와 같은 요청을 같은 길로 Jev 에 묻는다
(`_pick`): 선택지는 func 이름과 그 description 에 `none_fits` 를 더한 것, state 는 keyword 없는
`decide` 가 보내는 화면 전체다. Jev 가 `none_fits` 를 고르면 그 turn 은 적용되는 func 가 없는
것으로 보고, Jev 를 부르지 못하면 `FUNCS` 에 먼저 적힌 것을 돌린다. Python 읽기(`when`, `done`, `lost`)는
이 모듈의 `_holds`·`_reason`·`_applies` 로만 평가한다. 나중에 그 읽기를 macro 쪽으로 옮길 때 바꿀
곳이 여기다.

play 가 멈추는 때: `done`, `lost`, 어떤 func 도 적용되지 않는 상태가 `no_func_grace_seconds` 동안
이어질 때, func 의 macro 가 `max_failures_in_a_row` 번 연달아 실패할 때, turn·시간 상한. 멈춘 뒤의
판정은 evaluator 가 한다(`evaluator.py`).

`check` is loaded in a namespace of its own, so it judges from the game's readings and never from
a variable a func set: with one shared namespace it read a flag a func failed to raise and failed
a step whose reward was on screen (PoC try 82).
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from app.agents.planner_executer import sandbox
from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.g import GameHandle
from app.agents.planner_executer.macro_host import FuncOutcome, GameMacroHost, MacroJev, run_func
from app.agents.planner_executer.runs import RegisteredMacros, parse_run
from app.agents.qa.macro.binding import MacroMemories, MacroScope
from app.agents.qa.macro.context import build_context_state
from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.grammar import NONE_FITS, NONE_FITS_DESCRIPTION
from app.agents.qa.macro.model import MacroDefinition
from app.agents.qa.macro.runner import MacroDecideOption, MacroDecideRequest, MacroJevUnavailable
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import LogCategory, LogPayload, MessageType

logger = logging.getLogger(__name__)

# How long the executer waits for a reading when no func applies, before it looks again.
IDLE_LOOK_SECONDS = 1.0

# func 가 둘 이상 적용될 때 Jev 에 묻는 말. 뒤에 step 의 글이 붙는다.
PICK_QUESTION = "Which of these should the player do now to progress this step?"

# A func's error as kept in the turn record. A macro's stop reason names the line, what it
# expected and what it saw.
MAX_ERROR_CHARS = 400

# Largest turn record sent as one `LOG`. Orchestration's socket to the agent closes on frames over
# the library default of 64 KiB, so this stays well under it.
MAX_TURN_LOG_BYTES = 48_000
# Longest step module source sent as a `LOG`.
MAX_MODULE_LOG_CHARS = 6000


class TurnLogPayload(LogPayload):
    """One executer turn as a `LOG` with a structured body.

    Orchestration stores a `LOG` payload as it comes and asks only for a non-blank `message`, so a
    structured turn needs no change there. `kind` lets a reader pick turns out of the other logs.
    """

    kind: str = "turn"
    turn: dict[str, Any] = Field(default_factory=dict)


@dataclass
class Func:
    name: str
    description: str
    when: Callable[[GameHandle], Any]
    # macro source(`run_<name>`).
    run: str
    # 파싱한 `run`. `Executer._parse_runs` 가 채운다.
    macro: MacroDefinition | None = None


@dataclass
class StepModule:
    """A step module loaded: its funcs in the order written, and what decides the step."""

    source: str
    funcs: dict[str, Func]
    done: Callable[[GameHandle], Any]
    lost: Callable[[GameHandle], Any] | None
    check: Callable[[GameHandle], Any]


def load_step_module(source: str, step: int) -> StepModule:
    """Check and load `source`. Raises `sandbox.ModuleRejected` naming the problem.

    func 의 macro 는 여기서 파싱하지 않는다. `from macros import` 가 이 build 의 등록된 macro 를
    channel 로 물어야 하므로 async 이고, `Executer._parse_runs` 가 한다.
    """
    space = sandbox.load_namespace(
        source, ("check",), ("done",), ("FUNCS",), filename=f"<step-{step}>"
    )
    check = sandbox.load_namespace(source, ("check",), filename=f"<check-{step}>")["check"]
    funcs, refused = funcs_from(space.get("FUNCS"))
    if refused:
        raise sandbox.ModuleRejected("FUNCS: " + "; ".join(refused))
    if not funcs:
        raise sandbox.ModuleRejected("FUNCS declares no func")
    return StepModule(source, funcs, space["done"], space.get("lost"), check)


def funcs_from(declared: Any) -> tuple[dict[str, Func], list[str]]:
    """The funcs a `FUNCS` value declares, in order, and why any entry was refused."""
    if not isinstance(declared, dict):
        return {}, [f"FUNCS is {type(declared).__name__}, not a dict"]
    funcs: dict[str, Func] = {}
    refused = []
    for name, value in declared.items():
        if not (
            isinstance(value, tuple)
            and len(value) == 3
            and callable(value[1])
            and isinstance(value[2], str)
        ):
            refused.append(f"{name}: not (description, when, run) with `run` a macro string")
            continue
        funcs[str(name)] = Func(str(name), str(value[0]), value[1], value[2])
    return funcs, refused


async def parse_runs(funcs: dict[str, Func], registered: RegisteredMacros) -> list[str]:
    """func 마다 `run` macro 를 파싱해 `macro` 를 채운다. 거절된 것의 이유를 낸다."""
    refused = []
    for name, func in funcs.items():
        try:
            func.macro = await parse_run(name, func.run, registered)
        except MacroRejection as rejection:
            refused.append(f"`run_{name}`: {rejection.render()}")
    return refused


@dataclass
class StepPlay:
    """How play of one step went, before it is judged."""

    stopped: str
    module: StepModule | None = None
    turns: int = 0
    seconds: float = 0.0
    # 실행 전에 module 이 거절됐을 때 그 이유. 그러면 판정하지 않고 FAILED 다.
    rejected: str | None = None


class Executer:
    """Plays step modules over one `g` for one scenario."""

    def __init__(
        self,
        g: GameHandle,
        channel: QaRunChannel,
        spec: PlannerExecuterArchSpec,
        jev: MacroJev | None = None,
        registered: RegisteredMacros | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.g = g
        self._channel = channel
        self._spec = spec
        # func 의 macro 가 `decide`·`ask` 를 묻는 client. 없으면 그 statement 가 `JEV_FAILED` 로 멈춘다.
        self._jev = jev
        # `from macros import` 가 가리키는 이 build 의 등록된 macro.
        self._registered = registered or RegisteredMacros(channel)
        self._clock = clock
        # Every turn played this scenario, in order.
        self.turns: list[dict] = []

    async def play(self, step: int, source: str, action: str = "") -> StepPlay:
        """Load `source` and play step `step` with it until it stops. `action` 은 step 의 글로,
        func 를 고를 때 Jev 에 묻는 말에 붙는다."""
        try:
            module = load_step_module(source, step)
        except sandbox.ModuleRejected as error:
            return StepPlay("rejected", rejected=f"MODULE-ERROR: {error}")
        refused = await parse_runs(module.funcs, self._registered)
        if refused:
            return StepPlay("rejected", rejected="MODULE-ERROR: " + "; ".join(refused))
        await self._channel.note(
            f"Step {step} module:\n{source[:MAX_MODULE_LOG_CHARS]}", LogCategory.THOUGHT, step
        )
        g, clock, spec = self.g, self._clock, self._spec
        g.begin_step(step)
        started = clock()
        no_func_since: float | None = None
        failures_in_a_row = 0
        stopped = "turn limit"
        turn = 0
        try:
            for turn in range(spec.max_turns_per_step):
                await g.settle()
                now = clock()
                if now - started > spec.step_budget_seconds:
                    stopped = f"step budget {spec.step_budget_seconds:g}s"
                    break
                if _holds(module.done, g):
                    stopped = "done"
                    break
                reason = _reason(module.lost, g)
                if reason:
                    stopped = f"lost: {reason}"
                    break
                applicable = [name for name, func in module.funcs.items() if _applies(func, g)]
                name, decision = await self._pick(module, applicable, action)
                if name is None and decision is not None:
                    # Jev 가 `none_fits` 를 골랐다: 적용되는 func 가 없는 turn 과 같다.
                    await self._record(step, turn, applicable, None, None, decision)
                if name is None:
                    no_func_since = no_func_since or now
                    remaining = spec.no_func_grace_seconds - (now - no_func_since)
                    if remaining <= 0:
                        stopped = f"no func applied for {spec.no_func_grace_seconds:g}s"
                        break
                    await g.next_reading(min(remaining, IDLE_LOOK_SECONDS))
                    continue
                no_func_since = None
                outcome = await self._run(module.funcs[name], step)
                await self._record(step, turn, applicable, name, outcome, decision)
                failures_in_a_row = failures_in_a_row + 1 if outcome.kind == "failed" else 0
                if failures_in_a_row >= spec.max_failures_in_a_row:
                    stopped = (
                        f"{failures_in_a_row} funcs failed in a row (last: {name}: "
                        f"{outcome.reason[:MAX_ERROR_CHARS]})"
                    )
                    break
                await g.fresh()
        finally:
            g.end_step()
        return StepPlay(stopped, module, turns=turn, seconds=round(clock() - started, 1))

    async def _pick(
        self, module: StepModule, applicable: list[str], action: str
    ) -> tuple[str | None, dict | None]:
        """돌릴 func 와, Jev 에 물었으면 그 기록.

        요청은 macro runner 의 `_match` 가 `match decide(...)` 에서 만드는 것과 같은 모양이고
        (`MacroDecideRequest`, 끝에 `none_fits`), 같은 host 의 `decide` 로 `app/qa/jev.py` 의
        `JevClient` 에 간다. state 는 keyword 없는 `decide` 처럼 `build_context_state` 가 만든다.
        `JevClient.choose` 는 고른 이름만 돌려주므로 확률은 기록하지 못한다.
        """
        if len(applicable) <= 1:
            return (applicable[0] if applicable else None), None
        state, clipped = build_context_state(MacroMemories(scene=self._channel.scene), MacroScope(), ())
        request = MacroDecideRequest(
            question=f"{PICK_QUESTION} The step: {action}" if action else PICK_QUESTION,
            options=tuple(
                MacroDecideOption(name=name, description=module.funcs[name].description)
                for name in applicable
            )
            + (MacroDecideOption(name=NONE_FITS, description=NONE_FITS_DESCRIPTION),),
            state=state,
            clipped=clipped,
        )
        decision: dict[str, Any] = {"question": request.question, "options": applicable, "clipped": clipped}
        try:
            answer = await GameMacroHost(self.g, self._jev).decide(request)
        except MacroJevUnavailable as error:
            decision["error"] = str(error)[:MAX_ERROR_CHARS]
            self.g.log(f"func pick: Jev did not answer ({error}); taking {applicable[0]}")
            return applicable[0], decision
        decision["answer"] = answer
        self.g.log(f"func pick: Jev chose {answer} of {', '.join(applicable)}")
        return (None if answer == NONE_FITS else answer), decision

    async def _run(self, func: Func, step: int) -> FuncOutcome:
        assert func.macro is not None, "`parse_runs` parses every func before play"
        try:
            return await run_func(self.g, func.macro, self._jev, step)
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - noted against the func, play goes on
            return FuncOutcome("failed", f"{type(error).__name__}: {str(error)[:200]}")

    async def _record(
        self,
        step: int,
        turn: int,
        applicable: list[str],
        picked: str | None,
        outcome: FuncOutcome | None,
        decision: dict | None,
    ) -> None:
        """turn 하나를 기록하고 `LOG` 로 보낸다: 적용된 func, 고른 func, 그 결과, Jev 에 물었으면 그 답.

        Jev 가 `none_fits` 를 고른 turn 은 `picked` 가 None 이고 `outcome` 이 `none_fits` 다.
        """
        entry = {
            "step": step, "turn": turn, "frame": self.g.frame, "scene": self.g.scene,
            "applicable": applicable, "picked": picked,
            "outcome": outcome.kind if outcome is not None else NONE_FITS,
            "reason": (outcome.reason[:MAX_ERROR_CHARS] or None) if outcome is not None else None,
            "decision": decision,
        }
        self.turns.append(entry)
        await self._channel.emit(MessageType.LOG, _turn_log(entry))


def _applies(func: Func, g: GameHandle) -> bool:
    """func 의 `when`. 읽지 못하면 적용되지 않는 것으로 보고 trace 에 남긴다."""
    try:
        return bool(func.when(g))
    except Exception as error:  # noqa: BLE001 - a broken `when` is not a choice
        g.log(f"when_{func.name} raised {type(error).__name__}: {error}")
        return False


def _holds(predicate, g: GameHandle) -> bool:
    try:
        return bool(predicate(g))
    except Exception:  # noqa: BLE001 - an unreadable goal has not been reached
        return False


def _reason(lost, g: GameHandle) -> str | None:
    if lost is None:
        return None
    try:
        value = lost(g)
    except Exception:  # noqa: BLE001 - an unreadable loss has not happened
        return None
    return str(value) if value else None


def _turn_log(entry: dict) -> TurnLogPayload:
    """The turn as a `LOG`: a readable line, and the record itself."""
    others = f" (of {', '.join(entry['applicable'])})" if len(entry["applicable"]) > 1 else ""
    message = f"turn {entry['turn']}: {entry['picked'] or 'nothing'}{others} — {entry['outcome']}" + (
        f": {entry['reason']}" if entry.get("reason") else ""
    )
    body = json.loads(json.dumps(entry, default=str))
    if len(json.dumps(body, ensure_ascii=False).encode()) > MAX_TURN_LOG_BYTES:
        body = {key: entry[key] for key in ("step", "turn", "picked", "outcome")}
    return TurnLogPayload(
        category=LogCategory.OBSERVATION, message=message, step=entry.get("step"), turn=body
    )
