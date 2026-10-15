"""`g`: what a step module's Python reads of the game. It sends nothing.

A step module's Python (`when`, `done`, `lost`, `check`) reads the game through `g`. Inputs come
from a func's `run`, which is a macro: the macro runner sends each action statement through
`GameHandle._act`, the same channel call (`act_and_look`) the tool loop's macros use, so the two
engines share the layer that touches the game.

Two things only code can do, and why this layer exists:

* `wait_until` re-checks on every `pulse`, so `check` reacts on the reading that shows the
  condition rather than a model turn later;
* what the game went through during a step is recorded reading by reading, so a step's `check`
  can ask about a state that has already passed.

The channel is used as it is. It tells nobody when a reading lands, so `watch` waits for each
one with `QaRunChannel.wait_for_reading` and records what it shows.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agents.qa.tools.tool_context import _is_press, _press_landing
from app.qa.acting import ActionOutcome, ScreenChange
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import JsonRpcAction
from app.qa.pulse import PulseMemory

logger = logging.getLogger(__name__)

# How long `fresh` waits for a reading taken after the last action. The SDK reads every 0.1s and
# delivers every 1s, so a little over one delivery. The SDK sends nothing when nothing moved, so
# running out is normal and means "the screen did not change", not "the game is gone".
FRESH_READING_SECONDS = 1.5

# Ceiling on any single wait a function asks for.
MAX_WAIT_SECONDS = 60.0

# `settle` returns once no reading has landed for this long, after at most this many rounds.
SETTLE_QUIET_SECONDS = 0.15
SETTLE_ROUNDS = 8

# How long `watch` waits for one reading before looking again.
WATCH_SECONDS = 5.0

# The scene Unity files `DontDestroyOnLoad` objects under.
PERSISTENT_SCENE = "DontDestroyOnLoad"

# Bounds on `state_view`, which the planner reads once per call, not per input.
MAX_STATICS_IN_STATE = 40
MAX_OBJECTS_IN_STATE = 80
# Members shown per object. A member the planner cannot see does not exist for it, and a step was
# given up on because the count it needed was among the folded ones (PoC try 110).
MAX_MEMBERS_PER_OBJECT = 400
MAX_OFF_IN_STATE = 60
MAX_VALUE_CHARS = 80

class WaitTimedOut(Exception):
    """A `wait_until` condition never held. An error in the function unless caught."""


class ExpectationFailed(Exception):
    """An `expect` did not hold. The step's verdict is FAILED."""


@dataclass(frozen=True)
class Obj:
    """One object as the latest readings left it."""

    key: str
    scene: str | None
    selector: str | None
    path: str | None
    id: int | None
    tag: str | None
    text: str | None
    rect: dict[str, Any] | None
    live: bool
    # `Component.member` -> value. The component is the short type name.
    values: dict[str, Any] = field(default_factory=dict)

    @property
    def address(self) -> str:
        """The selector, else the instance id: what names the object in a trace line."""
        if self.selector:
            return self.selector
        if self.id is not None:
            return f"#{self.id}"
        return self.path or self.key

    def value(self, member: str, default: Any = None) -> Any:
        """A member's value, by `member` alone or by `Component.member`."""
        if member in self.values:
            return self.values[member]
        for name, value in self.values.items():
            if name.rsplit(".", 1)[-1] == member:
                return value
        return default


class GameHandle:
    """What a step module is given as `g`. Everything it may touch is a public member here."""

    def __init__(self, channel: QaRunChannel) -> None:
        self._channel = channel
        # Unity frame at which the last input batch finished. A reading shows an input's result
        # only when it was taken after this.
        self._last_action_frame: int | None = None
        self._step: int | None = None
        # What happened, in order: the step's record of play.
        self.trace: list[str] = []
        # The message of the last `expect` that held.
        self.last_expect: str | None = None
        # What the screen went through since the step began, reading by reading. `check` runs
        # after play, so a state play passed through — a reward shown for a second before the
        # map — is gone by then; this is where `check` can still ask about it.
        self._visited: list[str] = []
        self._seen_texts: set[str] = set()
        self._seen_objects: set[str] = set()
        # `pulse.readings` when `_on_reading` last ran. Whatever reads the record first catches up
        # with readings `watch` has not had a turn to take yet.
        self._recorded = 0
        # The game's own values at the start of each step, by step number. A "went up" claim
        # needs the value it went up from, and a planner left to recall it counts like a person:
        # the first stage is `StagePosition` 0, and a check that took it as 1 expected 2 (PoC
        # try 79).
        self.step_starts: dict[int, dict] = {}
        # Every input batch sent, in order, for the turn record.
        self.actions: list[dict] = []

    @property
    def _pulse(self) -> PulseMemory:
        return self._channel.scene.pulse

    # --- reading the game -----------------------------------------------------------------------

    @property
    def scene(self) -> str | None:
        return self._pulse.scene

    @property
    def frame(self) -> int | None:
        return self._pulse.frame

    def objects(
        self, match: str | None = None, tag: str | None = None, include_off: bool = False
    ) -> list[Obj]:
        """Objects the readings hold, optionally filtered.

        `match` is a case-insensitive substring of the selector or path. Switched-off objects are
        left out unless `include_off`.
        """
        needle = (match or "").strip().lower()
        found = []
        for key, held in sorted(self._pulse.held.items()):
            if not held.live and not include_off:
                continue
            if tag is not None and held.tag != tag:
                continue
            if needle and needle not in (held.selector or "").lower() and needle not in (
                held.path or ""
            ).lower():
                continue
            values = {}
            for member in held.members.values():
                component = (member.on or "").split(".")[-1]
                values[f"{component}.{member.member}"] = member.value
            found.append(
                Obj(
                    key=key,
                    scene=held.scene,
                    selector=held.selector,
                    path=held.path,
                    id=held.id,
                    tag=held.tag,
                    text=held.text,
                    rect=held.rect,
                    live=held.live,
                    values=values,
                )
            )
        return found

    def find(self, match: str) -> Obj | None:
        """The first live object matching `match`, or `None`."""
        hits = self.objects(match)
        return hits[0] if hits else None

    def exists(self, match: str) -> bool:
        return self.find(match) is not None

    def value(self, match: str, member: str, default: Any = None) -> Any:
        """A member's value on the first object matching `match`."""
        obj = self.find(match)
        return default if obj is None else obj.value(member, default)

    def text(self, match: str, default: str | None = None) -> str | None:
        obj = self.find(match)
        return default if obj is None or obj.text is None else obj.text

    def texts(self) -> list[str]:
        """Every text currently on screen."""
        return [obj.text for obj in self.objects() if obj.text]

    def static(self, name: str, default: Any = None) -> Any:
        """A static value, by `member` or `Declaring.member`."""
        for entry in self._pulse.statics.values():
            declaring = (entry.declaring or "").split(".")[-1]
            if name in (entry.member, f"{declaring}.{entry.member}"):
                return entry.value
        return default

    def state_view(self) -> str:
        """The readings as data, spelled the way a function reads them.

        `PulseMemory.render` is written for a model that reads it turn by turn: it leads with
        what changed and folds values away. A planner writing code needs every current value,
        under the exact name `g.value`/`g.static` takes.
        """
        pulse = self._pulse
        lines = [f"scene {pulse.scene!r} · reading {pulse.reading} · frame {pulse.frame}"]
        statics = sorted(pulse.statics.values(), key=lambda e: (e.declaring or "", e.member or ""))
        if statics:
            lines.append('statics — g.static("Type.member"):')
            for entry in statics[:MAX_STATICS_IN_STATE]:
                declaring = (entry.declaring or "").split(".")[-1]
                lines.append(f"  {declaring}.{entry.member} = {_show(entry.value)}")
        live = self.objects()
        lines.append(
            f'objects ({len(live)} live) — g.value("<part of selector>", "Type.member"), '
            "g.text(...):"
        )
        for obj in live[:MAX_OBJECTS_IN_STATE]:
            head = obj.selector or obj.key
            if obj.scene == PERSISTENT_SCENE:
                head += "  [persistent: in every scene]"
            if obj.text:
                head += f"  text={_show(obj.text)}"
            if obj.tag:
                head += f"  tag={obj.tag}"
            lines.append(f"  {head}")
            shown = [(name, value) for name, value in sorted(obj.values.items()) if not _noise(value)]
            for name, value in shown[:MAX_MEMBERS_PER_OBJECT]:
                lines.append(f"      {name} = {_show(value)}")
            if len(shown) > MAX_MEMBERS_PER_OBJECT:
                rest = ", ".join(name for name, _ in shown[MAX_MEMBERS_PER_OBJECT:])
                lines.append(
                    f"      (+{len(shown) - MAX_MEMBERS_PER_OBJECT} more members, values not "
                    f"shown: {rest})"
                )
        if len(live) > MAX_OBJECTS_IN_STATE:
            lines.append(f"  (+{len(live) - MAX_OBJECTS_IN_STATE} more live objects)")
        off = [obj for obj in self.objects(include_off=True) if not obj.live]
        if off:
            lines.append(
                f"switched off now ({len(off)}) — exist, not active; "
                "g.objects(match, include_off=True):"
            )
            for obj in off[:MAX_OFF_IN_STATE]:
                members = ", ".join(sorted(obj.values)[:6])
                lines.append(f"  {obj.selector or obj.key}" + (f"  [{members}]" if members else ""))
        return "\n".join(lines)

    def snapshot(self) -> dict:
        """Scalar values now: the scene, every static, every object member and text."""
        pulse = self._pulse
        statics = {}
        for entry in pulse.statics.values():
            if _scalar(entry.value):
                statics[f"{(entry.declaring or '').split('.')[-1]}.{entry.member}"] = entry.value
        members = {}
        texts = {}
        for obj in self.objects():
            for name, value in obj.values.items():
                if _scalar(value):
                    members[f"{obj.selector or obj.key}|{name}"] = value
            if obj.text is not None:
                texts[obj.selector or obj.key] = obj.text
        return {"scene": pulse.scene, "statics": statics, "members": members, "texts": texts}

    # --- what the game went through -------------------------------------------------------------

    async def watch(self) -> None:
        """Record every reading as it lands, until cancelled. The runner keeps this running."""
        while True:
            await self._channel.wait_for_reading(WATCH_SECONDS)
            self._catch_up()
            self._drain_operator()

    def _catch_up(self) -> None:
        if self._pulse.readings != self._recorded:
            self._recorded = self._pulse.readings
            self._on_reading()

    def _on_reading(self) -> None:
        scene = self.scene
        if scene and (not self._visited or self._visited[-1] != scene):
            self._visited.append(scene)
        for key, held in self._pulse.held.items():
            if held.live:
                self._seen_objects.add(held.selector or held.path or key)
                if held.text:
                    self._seen_texts.add(held.text)

    def _drain_operator(self) -> None:
        """Operator messages end every channel wait until taken. This engine has no one to answer
        them, so they go to the trace and stop holding the waits open."""
        for message in self._channel.drain_operator_messages():
            self._log(f"operator said: {message}")

    def at_step_start(
        self, step: int, name: str, match: str | None = None, default: Any = None
    ) -> Any:
        """A value as the game reported it when step `step` began.

        `name` is "Type.member". Without `match` it is looked up among statics, then among object
        members; with `match`, on the first object whose selector contains it. `name` "text" with
        `match` is the text that object showed, as `g.text` reads it now.
        """
        snap = self.step_starts.get(step)
        if not snap:
            return default
        if name == "text" and match is not None:
            needle = match.lower()
            return next(
                (text for selector, text in snap["texts"].items() if needle in selector.lower()),
                default,
            )
        if match is None and name in snap["statics"]:
            return snap["statics"][name]
        needle = (match or "").lower()
        for key, value in snap["members"].items():
            selector, member = key.split("|", 1)
            if member == name and needle in selector.lower():
                return value
        return default

    def scenes_visited(self) -> list[str]:
        """Scenes the game was on since this step began, in order, current last."""
        self._catch_up()
        return list(self._visited) or ([self.scene] if self.scene else [])

    def seen_object(self, fragment: str) -> bool:
        """Whether an object whose selector contains `fragment` was live at any reading since this
        step began."""
        self._catch_up()
        needle = fragment.lower()
        current = {obj.selector or obj.key for obj in self.objects()}
        return any(needle in name.lower() for name in self._seen_objects | current)

    def seen_text(self, fragment: str) -> bool:
        """Whether any text containing `fragment` was on screen since this step began."""
        self._catch_up()
        needle = fragment.lower()
        return any(needle in text.lower() for text in self._seen_texts | set(self.texts()))

    def begin_step(self, number: int) -> None:
        """Mark the step the next inputs belong to and take its starting values."""
        self._catch_up()
        self._step = number
        self.last_expect = None
        self.step_starts[number] = self.snapshot()
        self._visited = [self.scene] if self.scene else []
        self._seen_texts = set(self.texts())
        self._seen_objects = {obj.selector or obj.key for obj in self.objects()}
        self._log(f"step {number}: start")

    def end_step(self) -> None:
        self._step = None

    # --- waiting --------------------------------------------------------------------------------

    def _raise_if_cancelled(self) -> None:
        if self._channel.cancelled:
            raise QaCancelled()

    async def _await_reading(self, after: int, timeout: float, frame: int | None = None) -> bool:
        """Wait until a reading newer than `after` lands, up to `timeout` seconds.

        With `frame`, a reading counts only once it was taken after that frame: the first batch to
        arrive after an input can hold readings taken before it (ARTEL-621).
        """

        def arrived() -> bool:
            if self._pulse.readings <= after:
                return False
            return frame is None or self._pulse.frame is None or self._pulse.frame > frame

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            self._raise_if_cancelled()
            if arrived():
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await self._channel.wait_for_reading(remaining)
            self._drain_operator()

    async def fresh(self, timeout: float = FRESH_READING_SECONDS) -> bool:
        """Wait for a reading taken after the last input. False if none came.

        False is normal: the SDK sends nothing when nothing moved.
        """
        pulse = self._pulse
        if self._last_action_frame is None:
            if pulse.seen:
                return True
            return await self._await_reading(pulse.readings, timeout)
        if pulse.frame is not None and pulse.frame > self._last_action_frame:
            return True
        return await self._await_reading(pulse.readings, timeout, self._last_action_frame)

    async def next_reading(self, timeout: float) -> bool:
        """Wait for any reading newer than the one held, up to `timeout` seconds."""
        return await self._await_reading(self._pulse.readings, timeout)

    async def settle(self) -> None:
        """Let readings already on their way land before deciding on them.

        Readings come in bursts and are applied one frame at a time. A decision taken between two
        of them decides on the past: a PoC run took ten turns on one burst, each a reading older
        than the game (try 113).
        """
        for _ in range(SETTLE_ROUNDS):
            seen = self._pulse.readings
            await asyncio.sleep(SETTLE_QUIET_SECONDS)
            if self._pulse.readings == seen:
                return

    async def wait_until(
        self, condition: Callable[[], bool], timeout: float = 10.0, what: str = "condition"
    ) -> None:
        """Return as soon as `condition()` holds; raise `WaitTimedOut` if it never does.

        Checked first against a reading taken after the last input, then on every reading that
        lands. A reading only lands when something moved, so a condition about something that
        does not move is answered by the first check alone.
        """
        await self.fresh()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(max(timeout, 0.0), MAX_WAIT_SECONDS)
        while True:
            self._raise_if_cancelled()
            if self._holds(condition):
                self._log(f"wait_until {what}: held")
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                self._log(f"wait_until {what}: timed out after {timeout:g}s")
                raise WaitTimedOut(f"{what} did not hold within {timeout:g}s")
            await self._await_reading(self._pulse.readings, remaining)
            await self.settle()

    async def expect(
        self, condition: Callable[[], bool], message: str, timeout: float = 3.0
    ) -> None:
        """Assert what the step expects. Raises `ExpectationFailed` with `message`.

        Waits up to `timeout` for it, because an expected result often lands a reading or two
        after the input that causes it.
        """
        try:
            await self.wait_until(condition, timeout, what=f"expect: {message}")
        except WaitTimedOut:
            raise ExpectationFailed(message) from None
        self.last_expect = message

    def _holds(self, condition: Callable[[], bool]) -> bool:
        # A condition that raises (a None where a number was expected) has not held yet; the next
        # reading may fill it in.
        try:
            return bool(condition())
        except Exception as error:  # noqa: BLE001 - the step module's own predicate
            self._log(f"condition raised {type(error).__name__}: {error}")
            return False

    # --- acting ---------------------------------------------------------------------------------

    async def _act(self, actions: list[JsonRpcAction], summary: str) -> ActionOutcome:
        """macro 의 action statement 하나를 보낸다. `MacroHost.run` 이 부른다(`macro_host.py`).

        tool loop 의 `ToolContext.act` 와 같은 길이다: `QaRunChannel.act_and_look` 으로 보내고 그
        뒤의 `pulse` 를 기다린 다음, 누름이 닿은 곳(`landings`)과 화면이 움직였는지(`screen`)를
        데이터로 돌려준다. 두 구조를 비교하려면 게임에 입력을 보내는 층이 같아야 하므로, tool loop
        의 macro 가 하지 않는 보정(다른 점에 다시 누르기, 다시 보내기)은 여기서도 하지 않는다.

        실패한 action 도 raise 하지 않는다. runner 는 돌아온 값을 보고 멈출지 정하고, 실패는
        `actions` 기록에 남아 executer 가 turn 이 끝난 뒤 func 의 error 로 센다.

        operator 의 말은 trace 로 보내고 runner 에는 넘기지 않는다. 이 engine 에는 답할 사람이 없고
        (`_drain_operator`), 넘기면 macro 가 `OPERATOR_INTERRUPTED` 로 멈춘다.
        """
        sent = {"summary": summary, "frame": self.frame, "ok": False, "error": None, "reached": None}
        self.actions.append(sent)
        result, looked = await self._channel.act_and_look(actions, summary, self._step)
        self._drain_operator()
        if result is None:
            sent["error"] = "no answer from the game"
            self._log(f"{summary}: no answer from the game")
            return ActionOutcome(
                text=f"{summary}: the game reported no result", screen=ScreenChange.unknown
            )
        if result.frame is not None:
            self._last_action_frame = result.frame
        methods = {action.id: action.method for action in actions}
        returned: list[Any] = []
        failures: list[str] = []
        for item in result.results:
            if item.id not in methods:
                continue
            if not item.success:
                failures.append(f"{methods[item.id]}: {item.error or 'no reason given'}")
                continue
            returned.append(item.returnValue)
        landings = tuple(_press_landing(value) for value in returned if _is_press(value))
        sent["ok"] = not failures
        sent["error"] = "; ".join(failures) or None
        sent["reached"] = next(
            (v["reached"] or "nothing" for v in returned if isinstance(v, dict) and "reached" in v),
            None,
        )
        if looked:
            screen = ScreenChange.moved
        elif self._pulse.seen:
            screen = ScreenChange.still
        else:
            screen = ScreenChange.unknown
        text = f"{summary}: " + (f"FAILED — {sent['error']}" if failures else f"ok{_reached(returned)}")
        self._log(text)
        return ActionOutcome(text=text, screen=screen, landings=landings)

    def log(self, line: str) -> None:
        """Add a line to this run's trace, which the planner reads when a function fails."""
        self._log(line)

    def _log(self, line: str) -> None:
        stamped = f"[f{self.frame if self.frame is not None else '?'}] {line}"
        self.trace.append(stamped)
        logger.debug("[QA planner_executer] %s", stamped)


def g_functions() -> tuple[str, ...]:
    """What a step module may call or read on `g`, for the engine's fingerprint."""
    return tuple(
        sorted(
            name
            for name, value in vars(GameHandle).items()
            if not name.startswith("_") and (callable(value) or isinstance(value, property))
        )
    )


def _show(value: Any) -> str:
    """One value, compact. References and sprites keep only what names them."""
    if isinstance(value, dict):
        kept = {k: value[k] for k in ("value", "path", "name", "sprite", "state", "active") if k in value}
        value = kept or value
    text = repr(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "…"


def _scalar(value: Any) -> bool:
    return isinstance(value, (bool, int, float, str)) or value is None


def _noise(value: Any) -> bool:
    """A value that names an asset or a renderer, which no step is judged on."""
    return isinstance(value, dict) and "sprite" in value


def _reached(returned: list[Any]) -> str:
    """What a press reached, when the SDK says so (`reached`)."""
    for value in returned:
        if isinstance(value, dict) and "reached" in value:
            if value.get("pointerHeldByPerson"):
                return " (not delivered: a person holds the pointer)"
            return f" (reached {value['reached']})" if value["reached"] else " (reached nothing)"
    return ""
