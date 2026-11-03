"""The executer: plays one step's macros with Jev until it stops.

Each turn:

1. stop at `max_turns_per_step` or `step_budget_seconds`;
2. read `done()` on the current screen. When it holds, ask Jev whether the step's goal has really
   been reached. Agreed — stop, `done`. Not agreed — note a disagreement and go on;
   `max_done_disagreements` of them stop play;
3. ask Jev which action to run now: the actions' docstrings are the options, with `none_fits`. The
   state Jev reads carries the last `RECENT_ACTIONS_SHOWN` action turns — what ran, whether it sent an
   input, why it stopped (ARTEL-1288): a screen that did not move looks the same every turn, and
   without this Jev picked the same idle action eleven times in a row;
4. `none_fits` — wait for the next reading, and stop once nothing has fit for
   `no_action_grace_seconds`. Otherwise run the action with the macro runner; `max_failures_in_a_row`
   failed actions stop play, and so do `max_idle_turns_in_a_row` actions that ran without sending the
   game an input (ARTEL-1288);
5. before those stops, when one action has failed or sent nothing `repair_after` times in a row and
   the play has a `repair` (the editor, `app/agents/editor/`), call it with that action's turn records
   and go on with the macros it returns — up to `max_repairs_per_step` times a step;
6. record the turn and send it as a `LOG`.

Jev 에 묻는 두 질문은 macro 의 `ask`·`decide` 와 같은 모양이다: state 는 keyword 없는 `ask`·`decide`
가 보내는 것(scene 과 화면의 live object 전부)이고 — 출처는 `state_source` 가 고른다: Orchestration 의
`STATE_SET`(`app/qa/state_set.py`, ARTEL-1297) 이나 Agent 의 `PulseMemory`(`build_context_state`) — 선택지 끝에는
`none_fits` 가 붙고, `ask` 는 `ASK_TRUE_AT` 이상이면 참이다. 질문을 만드는 것은 이 모듈이고, 보내는
것은 macro runner 의 host 가 쓰는 `app/qa/jev.py` 의 `JevClient` 다. Jev 가 답하지 못하면 그 step 의
play 를 그 오류로 멈춘다 — 대신 아무거나 고르면 무엇을 잰 것인지 알 수 없게 된다.
"""

import ast
import asyncio
import itertools
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.contract import READY_PREFIX
from app.agents.planner_executer.target import StepTarget, target_state
from app.agents.planner_executer.evidence import evidence_log, readings
from app.agents.planner_executer.macro_host import (
    ASK_FALSE_MEANS,
    ASK_TRUE_MEANS,
    ChannelMacroHost,
)
from app.agents.planner_executer.macros import (
    EFFECT_FLAG,
    READY_FLAG,
    PROBE_FLAG,
    ActionPick,
    StepCheck,
    StepMacros,
    StepObserve,
)
from app.agents.planner_executer.prompts import screen
from app.agents.qa.macro.binding import MacroMemories, MacroScope
from app.agents.qa.macro.context import build_context_state
from app.agents.qa.macro.errors import ACTION_REJECTED
from app.agents.qa.macro.grammar import ASK_TRUE_AT, NONE_FITS, NONE_FITS_DESCRIPTION
from app.agents.qa.macro.model import MacroDefinition
from app.agents.qa.macro.runner import _apply_read_member, run_macro
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import LogCategory, LogPayload, MessageType
from app.qa.jev import JevClient, JevError
from app.agents.planner_executer.facts import NO_FACTS, GameFacts
from app.qa.state_set import (
    EVERYTHING,
    EVIDENCE_SPEC,
    EXECUTER_SPEC,
    PERSISTENT_SCENE,
    StateSetUnavailable,
    StateSnapshot,
    StateView,
    declared_values,
    unmatched_paths,
    evidence_values,
    read_state,
    render_jev_state,
    state_changes,
)

# What the executer calls to read the game's state from Orchestration: `(qa_try_id, spec)` → snapshot.
StateReader = Callable[[int, dict[str, Any]], Awaitable[StateSnapshot]]

# What the executer calls to have a stuck action's macros rewritten: `(macros, action, its turn
# records)` → the rewrite, or `None` when it was refused to the end (`app.agents.editor.Repair`).
Repair = Callable[[StepMacros, str, list[dict]], Awaitable[StepMacros | None]]

GOAL_QUESTION = "Has this step's goal been reached?"

# Jev 가 action 대신 고를 수 있는 둘(ARTEL-1295 C). 서로, 그리고 `none_fits` 와 겹치지 않게 쓴다: `none_fits` 는 "지금은
# 맞는 것이 없으니 화면이 바뀌길 기다린다", `EXPAND_STATE` 는 "고르려면 보이는 것이 모자란다", `REPLAN` 은 "이 action
# 들로는 이 화면에서 나아갈 수 없다".
EXPAND_STATE = "EXPAND_STATE"
EXPAND_STATE_DESCRIPTION = (
    "Show more of the game state before choosing: the objects or values needed to pick an action are not in "
    "the state shown."
)
REPLAN = "REPLAN"
REPLAN_DESCRIPTION = (
    "Give up these actions and plan the step again: the state shows enough, but no listed action can make "
    "progress from this screen."
)
PICK_QUESTION = "Which of these should the player do now to progress this step?"

# How long the executer waits for a reading when nothing fits, before it asks again.
IDLE_LOOK_SECONDS = 1.0
# How many of the latest action turns Jev is told about when it picks the next action. Three is
# enough to show a repeat; the screen itself says the rest.
RECENT_ACTIONS_SHOWN = 3
# Readings come in bursts and are applied one frame at a time. A decision taken between two of
# them decides on the past, so each turn first lets the burst land: it waits until no reading has
# landed for `SETTLE_QUIET_SECONDS`, at most `SETTLE_ROUNDS` times.
SETTLE_QUIET_SECONDS = 0.15
SETTLE_ROUNDS = 8

# A failure reason as kept in the turn record.
MAX_REASON_CHARS = 400
# 잘린 reading 때문에 판정하지 못한 action 을 실패로 세지 않고 다시 기다리는 횟수. 이만큼 이어지면 실패로 센다 — reading 이
# 끝내 온전해지지 않는 게임에서 같은 자리를 영영 돌지 않게 한다.
MAX_UNOBSERVED_IN_A_ROW = 5
# 인자를 매 turn 고르는 action 의 선택지 이름: `<action> @ <대상 path>`.
PICK_SEPARATOR = " @ "
# action 하나가 한 turn 에 펼치는 후보 수. 풀에서 꺼낸 객체가 수백이어도 선택지가 넘치지 않게 한다.
MAX_PICK_CANDIDATES = 12
# 실패 이유에 적힌 macro 조건 글에서 `selector("...")` 를 뽑는다.
_SELECTOR_LITERAL = re.compile(r'selector\("([^"]+)"\)')
_INDEX = re.compile(r"\[\d+\]")


def unseen_selectors(text: str, pulse: Any) -> list[str]:
    """`text` 의 `selector("...")` 중 이 런에서 게임이 한 번도 보고하지 않은 것. 지금 있는 객체(켜짐·꺼짐)와 사라진 객체를 모두
    본 것으로 센다. 경로의 `[n]` 은 무시하고, 그 아래 객체가 보고됐으면 본 것이다."""
    names = list(dict.fromkeys(_SELECTOR_LITERAL.findall(text or "")))
    if not names:
        return []
    known: set[str] = set()
    for held in pulse.held.values():
        known.update(_INDEX.sub("", value).strip("/") for value in (held.selector, held.path) if value)
    for key in pulse.gone_at:
        plain = _INDEX.sub("", key).strip("/")
        known.update({plain, plain.split("/", 1)[-1]})
    return [
        name for name in names
        if not any(seen == name or seen.startswith(name + "/") for seen in known)
    ]
def address_hints(text: str, pulse: Any) -> dict[str, list[str]]:
    """`text` 의 `selector("...")` 중 이 런에서 보고된 적 없는데, 지금 켜진 객체의 경로가 그것으로 끝나는 것과 그 경로들.

    앞부분이 빠진 주소를 가리킨다(런 222: `ChatWindow/…` 를 썼는데 게임은 `Canvas/ChatWindow/…` 로 보고했다). 의심일 뿐 고치지는
    않는다 — 다른 객체를 겨누게 될 수 있다. 꺼진 객체는 견주지 않는다: 이름만 같은 다른 객체를 짚는다."""
    live = sorted({_INDEX.sub("", held.path).strip("/") for held in pulse.held.values() if held.path and held.live})
    hints: dict[str, list[str]] = {}
    for name in unseen_selectors(text, pulse):
        matches = [path for path in live if path.endswith("/" + name)]
        if matches:
            hints[name] = matches[:3]
    return hints


def effect_missed(turn: dict) -> bool:
    """입력은 갔는데 효과가 나지 않은 turn. 계약 행동은 `result`, planner 가 쓴 효과 조건은 `effect` 에 적는다.

    예전에는 Jev 의 최근 기록·Evaluator·ledger 가 `effect` 만 읽어, 계약 행동의 `effect_unmet` 을 통과로 전했다(런 225)."""
    return turn.get("result") == "effect_unmet" or turn.get("effect") == "unmet"


def ready_source(source: str, action: str) -> str:
    """`source` 에서 `ready_<action>` 과 그것이 (helper 를 거쳐) 부르는 def 의 글.

    준비 판정이 실제로 읽는 주소만 남긴다. 전에는 행동의 글 전체를 봐, 실행하지 않은 effect 의 주소 하나로도 주소 의심이 났다."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    defs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    seen: set[str] = set()
    todo = [READY_PREFIX + action]
    while todo:
        name = todo.pop()
        if name in seen or name not in defs:
            continue
        seen.add(name)
        todo += [item.func.id for item in ast.walk(defs[name]) if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)]
    return "\n\n".join(ast.get_source_segment(source, defs[name]) or "" for name in sorted(seen))


# `STATE_SET` 이 동기화되지 않았을 때(`AWAITING_WHOLE`) Jev 를 부르지 않고 기다리는 시간. 새 stream 의 첫 whole
# `pulse` 는 연결 직후나 씬이 바뀔 때 온다.
STATE_SYNC_WAIT_SECONDS = 10.0

# 대상 해석에 보내는 시작 값의 상한(글자). 근거 기록 하나의 상한보다 넓다 — 대상을 가리키는 값이 빠지면 해석이 그 step 을
# `unknown` 으로 둔다.
TARGET_BASELINE_CHARS = 24_000

# 근거 기록에 늘 적는 활성 씬의 이름. 객체 경로와 겹치지 않게 괄호로 감싼다.
ACTIVE_SCENE = "(active scene)"

# Largest turn record sent as one `LOG`. Orchestration's socket closes on frames over 64 KiB.
MAX_TURN_LOG_BYTES = 48_000


class TurnLogPayload(LogPayload):
    """One executer turn as a `LOG` with a structured body. `kind` picks turns out of the logs."""

    kind: str = "turn"
    turn: dict[str, Any] = Field(default_factory=dict)


class EvidenceLogPayload(LogPayload):
    """step 하나의 판정 근거 기록(`evidence.evidence_log`). `kind` 로 로그에서 고른다."""

    kind: str = "evidence"
    evidence: dict[str, Any] = Field(default_factory=dict)


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


@dataclass(frozen=True)
class ContractInstance:
    """계약 행동의 후보 하나(ActionInstance): 행동, 인자 바인딩, 바인딩한 객체의 런타임 id, Jev 가 읽을 설명."""

    action: str
    binding: tuple[tuple[str, str], ...]
    ids: dict[str, int | None]
    description: str


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
    # Jev 가 `REPLAN` 을 골라 멈췄다(ARTEL-1295 C). runner 가 planner 를 다시 부른다.
    replan_requested: bool = False
    # play 가 멈출 때 Jev 가 마지막으로 읽은 `STATE_SET` 의 `stateVersion`. 판정의 근거를 Orchestration 상태에 잇는다.
    end_state_version: int | None = None
    # planner 의 ```check 범위와, 그 값을 play 시작·매 turn 시작·끝에 `STATE_SET` 에서 읽은 기록. 기록 하나는
    # `{"at": "start" | "turn <n>" | "end", "state_version", "values": {이름: 값}}` 이고, 읽지 못했으면 `values` 대신
    # `error` 다. evaluator 는 화면 대신 이것을 읽는다. check 가 없거나 출처가 `PulseMemory` 면 비어 있다.
    check: StepCheck | None = None
    evidence: list[dict] = field(default_factory=list)
    # check 의 이름 중 어느 기록에서도 맞지 않은 것. 멤버는 `Type.member`, 경로는 `path <경로>` 로 적는다(묶음 3).
    evidence_unknown: list[str] = field(default_factory=list)
    # ```observe 의 경로 중 어느 기록에서도 객체에 맞지 않은 것. Jev 가 그 경로로는 아무것도 못 봤다(묶음 3). planner 복구가 읽는다.
    observe_unknown: list[str] = field(default_factory=list)
    # 실패한 action 의 조건이 가리켰지만 이 런에서 게임이 한 번도 보고하지 않은 `selector`. 조건이 틀렸을 수 있다는 근거다 —
    # 글자도 동작 근거도 없는 객체는 SDK 가 싣지 않는다(런 184: `PushArea1` 을 기다린 developer macro). planner 복구가 읽는다.
    unseen_selectors: list[str] = field(default_factory=list)
    # `repair="planner"` 일 때 executer 가 planner 에게 step 을 다시 써 달라고 멈춘 이유. `{"action", "misses",
    # "reason"}` 이고, 남은 게임 action 이 하나도 없어 멈췄으면 `action` 이 `None` 이다. runner 가 planner 를 부른다.
    repair_request: dict | None = None
    # play 시작과 끝의 전체 게임 상태를 비교한 변화(묶음 3). planner 의 check 와 상관없다. 상한에 걸려 뺀 수가 `changes_left_out`.
    changes: list[str] = field(default_factory=list)
    changes_left_out: int = 0
    # 이 step 의 고정된 대상(`target.py`). 계약이 없는 런이면 `None` 이다. 그 값은 근거 기록에 함께 읽힌다.
    target: StepTarget | None = None
    # 판정 근거의 출처. PulseMemory run은 별도의 STATE_SET evidence 대신 시작·끝 관측값을 전달한다.
    state_source: Literal["state_set", "pulse_memory"] = "state_set"

    def played(self) -> dict:
        """step `STATUS` 의 `played`: 영상 구간을 자를 기준. `docs/evaluator-analysis-frames.md`."""
        return {
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "seconds": self.seconds,
            "stopped": self.stopped,
            "end_state_version": self.end_state_version,
        }

    def reached_goal(self) -> bool:
        """Whether play got to the step's goal by its own reading: `done()` held on some turn.

        The engine's claim, not the verdict. The Evaluator records it beside its own answer so
        the two can be compared.
        """
        return self.stopped == "done" or any(turn.get("done") for turn in self.turns)


class JevStopped(Exception):
    """Jev did not answer; the step's play stops with this."""


class StateStopped(Exception):
    """The state Jev would read could not be had from `STATE_SET`; the step's play stops with this.

    `PulseMemory` 로 되돌아가지 않는다. 몰래 바꾸면 그 turn 이 어느 출처로 골랐는지 기록에서 사라진다.
    """


class Executer:
    """Plays one scenario's steps, one at a time, over the run's channel."""

    def __init__(
        self,
        channel: QaRunChannel,
        spec: PlannerExecuterArchSpec,
        jev: JevClient,
        clock: Callable[[], float] = time.monotonic,
        state_reader: StateReader = read_state,
        facts: GameFacts = NO_FACTS,
    ) -> None:
        self._channel = channel
        self._spec = spec
        # 개발자가 선언한 fact. planner 가 step 마다 고른 것만 Jev state 의 `facts` 로 싣는다.
        self._game_facts = facts if spec.facts == "on" else NO_FACTS
        # 지금 play 에서 고른 fact: `(이름, scope, source 이름)`. `play` 가 매번 정한다.
        self._fact_sources: list[tuple[str, str, str]] = []
        # 마지막 state 에서 `unknown` 이 된 fact 와 그 까닭. turn 기록에 남긴다.
        self._fact_reasons: dict[str, str] = {}
        self._jev = jev
        self._clock = clock
        self._state_reader = state_reader
        # 마지막으로 Jev 에게 보낸 state 의 `stateVersion`. turn 기록에 남겨 어느 상태로 골랐는지 잇는다.
        self._state_version: int | None = None
        # 지금 play 의 범위: planner 의 ```observe 와 `EXPAND_STATE` 로 넓힌 단계(0–2). `play` 가 매번 정한다.
        self._observe: StepObserve | None = None
        self._expansions = 0
        # 상한에 걸려 Jev 의 state 에서 빠진 객체 수. turn 기록에 남긴다.
        self._omitted = 0
        # 이 play 의 첫 근거 읽기 snapshot. 끝 읽기와 비교해 `StepPlay.changes` 를 만든다.
        self._evidence_start: StateSnapshot | None = None

    async def play(
        self,
        step: int,
        action: str,
        macros: StepMacros,
        repair: Repair | None = None,
        can_replan: bool = False,
        budget_seconds: float | None = None,
        can_ask_planner: bool = False,
        target: StepTarget | None = None,
        history: list[dict] | None = None,
    ) -> StepPlay:
        """Play step `step` (`action` is its text) with `macros` until it stops.

        `repair` is the editor for this step, or `None` when the play has none (a FREE case's play:
        its macros are a scene plan shared by several cases, which the editor does not rewrite).
        `can_replan` offers Jev `REPLAN`; the runner passes it while the step has replans left.
        `budget_seconds` is what is left of the step's time budget (a step played again after a replan or a planner
        repair does not start the budget over). `can_ask_planner` lets a stuck action stop play for the planner to
        write the step again (`repair="planner"`). `target` is the step's fixed target: when `done()` holds,
        the turn records whether it holds on that target. `history` is the step's turns before a replan: Jev's recent actions
        go on showing them, marked as before the new plan, so a failure just before the replan is not lost.
        """
        channel, spec, clock = self._channel, self._spec, self._clock
        # play 안의 `target` 은 고른 대상 객체의 이름이다. step 의 대상 계약은 이 이름으로 쥔다.
        step_target = target
        self._evidence_start = None
        self._observe = getattr(macros, "observe", None)
        self._fact_sources = [
            (name, fact.scope, fact.member)
            for name in getattr(macros, "facts", ())
            if (fact := self._game_facts.get(name)) is not None
        ]
        self._expansions = 0
        await self._settle()
        play = StepPlay(
            "turn limit",
            start_screen=screen(channel.scene.pulse),
            start_frame=channel.scene.pulse.frame,
            started_at=_now(),
            check=getattr(macros, "check", None),
            target=target,
            state_source=spec.state_source,
        )
        await self._read_evidence(play, "start")
        started = clock()
        none_since: float | None = None
        failures = disagreements = idle_turns = repairs = unobserved = 0
        # 같은 action 을 연달아 고른 횟수.
        same_in_a_row = 0
        last_picked: str | None = None
        # 방금 통과한 action 의 ```after 다음 행동. 다음 turn 의 선택지를 이것으로 좁힌다.
        follow: tuple[str, ...] = ()
        # 이 step 에서 action 마다 실패했거나 입력 없이 끝난 횟수(누적, ARTEL-1298). 연속만 세면 두 action 이 번갈아 헛돌
        # 때 아무 규칙에도 안 걸린다 — 런 159 의 step 5 는 방향키 실패 2번과 입력 없는 대화 닫기 1번을 30번 되풀이해
        # 480 초를 다 썼다.
        misses: dict[str, int] = {}
        # `max_misses_per_action` 에 닿아 이 step 의 선택지에서 뺀 action.
        dropped: list[str] = []
        # 입력 없이 끝나 잠시 뺀 action. 다른 action 이 게임에 입력을 보내면 돌려놓는다 — 그 전까지는 화면이 그대로라
        # 같은 action 은 또 할 일이 없다. 런 162·165 의 step 4 에서 닫을 대사가 없는 `dismiss_dialogue` 를 거듭 골랐고, 런 165
        # 에서는 그것이 헛돎으로 세어져 editor 가 멀쩡한 macro 를 고치다 망가뜨렸다.
        parked: set[str] = set()
        # 마지막 입력 뒤로 입력 없이 끝난 적이 있는 action. 입력이 오가지 않았는데 또 입력 없이 끝나면 그때는 헛돎으로 센다 —
        # 할 일이 없는 것이 아니라 macro 가 입력을 못 보내는 것일 수 있다(ARTEL-1288 의 경우).
        idled: set[str] = set()
        try:
            for turn in range(spec.max_turns_per_step):
                await self._settle()
                if turn:
                    await self._read_evidence(play, f"turn {turn}")
                now = clock()
                if now - started > (spec.step_budget_seconds if budget_seconds is None else budget_seconds):
                    play.stopped = f"step budget {spec.step_budget_seconds:g}s"
                    break
                # `frame` 은 `done()` 과 macro 가 읽는 `pulse` 의 마지막 frame 이다. Jev 의 state 는 `state_version` 으로,
                # 근거는 기록마다의 `state_version` 으로 출처를 잇는다(묶음 3).
                record: dict[str, Any] = {"step": step, "turn": turn, "scene": self._scene(), "frame": channel.scene.pulse.frame}
                play.turns.append(record)
                record["done"], done_error = await self._done(macros.probe, step)
                if done_error:
                    record["done_error"] = done_error
                if record["done"] and step_target is not None and step_target.status == "bound":
                    # 대상 계약: `done()` 이 참일 때의 대상 대조를 남긴다. 멈춤은 막지 않는다 — identity 는 이름으로 짐작한
                    # 해석이라 틀릴 수 있고, 틀린 identity 로 멈춤을 막으면 play 가 목표를 지나쳐 간다(런 232). 다른 대상 위의 결과는 판정
                    # 뒤 `guard` 가 통과로 남기지 않는다.
                    latest = next((entry["values"] for entry in reversed(play.evidence) if "values" in entry), {})
                    record["target"] = target_state(step_target, latest)[0]
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
                # state 를 먼저 읽는다. 상한에 걸려 빠진 것이 있는지가 선택지(`EXPAND_STATE`)를 정한다.
                state = await self._state()
                # Jev 가 무엇을 보고 골랐는지는 그 state 가 전부다. 기록에 없으면 "왜 그걸 골랐나" 를 다시 볼 길이 없다
                # (런 162 step 4: 대사가 없는데 `dismiss_dialogue` 를 다섯 번 골랐다).
                record["jev_state"] = state
                record["view"] = self._view_record()
                if self._fact_reasons:
                    record["fact_unknown"] = dict(self._fact_reasons)
                names = [name for name in [*macros.actions, *macros.contracts] if name not in dropped and name not in parked]
                if not names and parked:
                    # 남은 것이 잠시 뺀 action 뿐이면 돌려놓는다. 화면이 스스로 바뀌어(대사가 뜨는 등) 할 일이 생겼을 수 있다.
                    parked.clear()
                    await channel.wait_for_reading(IDLE_LOOK_SECONDS)
                    names = [name for name in [*macros.actions, *macros.contracts] if name not in dropped]
                if parked:
                    record["parked"] = sorted(parked)
                options = {name: macros.description(name) for name in names if name in macros.actions}
                # 준비 조건이 거짓인 행동은 고르기 전에 뺀다. 실행 직전에도 다시 읽는다.
                not_ready = await self._not_ready(list(options), macros, step)
                if not_ready:
                    record["not_ready"] = not_ready
                    options = {name: text for name, text in options.items() if name not in not_ready}
                # 인자를 매 turn 고르는 action 은 지금 켜진 후보 객체마다 선택지가 된다. 사라진 객체는 빠지고, 그 macro
                # 앞부분 `require` 가 거짓인 객체도 빠진다.
                options, unready = await self._expand_picks(options, macros, step)
                # 계약 행동: 바인딩마다 준비 조건이 참인 것만 선택지다. 거짓·확인 불가는 이유와 함께 남긴다.
                instances, unready_contracts = await self._contract_options(
                    [name for name in names if name in macros.contracts], macros, step
                )
                unready = [*unready, *unready_contracts]
                if unready:
                    record["not_ready_targets"] = unready
                    not_ready = [*not_ready, *unready]
                    # 준비 조건을 읽지 못한 후보는 거짓이 아니라 보류다. 선택지에는 없고 이유와 함께 따로 남긴다.
                    held = [item for item in unready if item.endswith("(unknown)")]
                    if held:
                        record["held_targets"] = held
                options.update({key: instance.description for key, instance in instances.items()})
                narrowed = {key: text for key, text in options.items() if key.split(PICK_SEPARATOR, 1)[0] in follow}
                if narrowed:
                    # planner 가 정한 선후다: 준비한 뒤에는 시전을 고르게 한다. 런 173 step 4 에서 Jev 가 주문을 준비한 뒤 턴을
                    # 끝내 조합이 사라지고, 시전은 조합 버튼이 없다며 실패하기를 되풀이했다.
                    options = narrowed
                    record["after"] = sorted(narrowed)
                follow = ()
                if not options and not_ready:
                    # 준비된 행동이 없다. 실패가 아니라 기다릴 일이다 — 고를 것이 없을 때와 같이 유예 동안 화면을 기다린다.
                    await self._send(record)
                    none_since = none_since or now
                    remaining = spec.no_action_grace_seconds - (now - none_since)
                    if remaining <= 0:
                        stuck = f"no action was ready for {spec.no_action_grace_seconds:g}s ({', '.join(not_ready)})"
                        # 준비 조건이 이 런에서 본 적 없는 주소를 읽는데 그것으로 끝나는 켜진 객체가 있으면 주소가 틀렸다고 의심해
                        # 남긴다. 의심만으로는 재계획을 막지 않는다: 아직 안 생긴 객체·다른 씬도 본 적 없는 주소다.
                        hints = self._address_hints(not_ready, macros)
                        if hints:
                            record["suspected_address"] = hints
                            play.unseen_selectors = sorted(set(play.unseen_selectors) | set(hints))
                            stuck += "; suspected address mismatch in the catalog: " + "; ".join(
                                f"selector({name!r}) was never reported, but {', '.join(paths)} was" for name, paths in hints.items()
                            )
                        if can_ask_planner:
                            play.repair_request = {"action": None, "misses": None, "reason": stuck}
                            play.stopped = f"REPAIR: {stuck}"
                        elif can_replan:
                            # 고른 행동들이 이 화면에 맞지 않는다. 행동 목록이 아니라 계획을 다시 쓸 일이다.
                            play.replan_requested = True
                            play.stopped = f"REPLAN: {stuck}"
                        else:
                            play.stopped = stuck
                        break
                    await channel.wait_for_reading(min(remaining, IDLE_LOOK_SECONDS))
                    continue
                if not options:
                    await self._send(record)
                    left = (
                        f"every action failed or sent no input {spec.max_misses_per_action} times in this step, or was "
                        f"picked {spec.max_same_action_in_a_row} times in a row without progress ({', '.join(dropped)})"
                    )
                    # 게임 action 이 다 빠져도 복구할 길이 남았으면 그쪽으로 간다(개선 계획 묶음 2). 전에는 `REPLAN` 예산이 남아도
                    # 선택지를 만들기 전에 멈췄다.
                    if can_ask_planner:
                        play.repair_request = {"action": None, "misses": None, "reason": f"Every action is out of this step's choices: {left}"}
                        play.stopped = f"REPAIR: no action left: {left}"
                    elif can_replan:
                        play.replan_requested = True
                        play.stopped = f"REPLAN: no action left: {left}"
                    else:
                        play.stopped = f"no action left: {left}"
                    break
                if self._narrows() and self._expansions < spec.max_expansions_per_step:
                    options[EXPAND_STATE] = EXPAND_STATE_DESCRIPTION
                if can_replan and self._planned():
                    options[REPLAN] = REPLAN_DESCRIPTION
                record["question"] = f"{PICK_QUESTION} The step: {action}" + (
                    f"\nHow to play it: {macros.guidance}" if macros.guidance else ""
                )
                record["options"] = list(options)
                earlier = [{**turn, "earlier_plan": True} for turn in history or []]
                recent = recent_action_lines(earlier + play.turns[:-1])
                if recent:
                    record["recent_actions"] = recent
                record["picked"] = picked = await self._pick(record["question"], options, recent, state)
                target = None
                instance = instances.get(picked)
                if instance is not None:
                    # 계약 행동의 후보 하나다. 세기·선후는 행동 이름으로 한다.
                    picked = instance.action
                    if instance.binding:
                        record["target"] = " + ".join(selector for _param, selector in instance.binding)
                elif PICK_SEPARATOR in picked:
                    # 고른 선택지는 action 과 대상이다. 세기·선후·효과는 action 이름으로 한다.
                    picked, target = picked.split(PICK_SEPARATOR, 1)
                    record["target"] = target
                if self._state_version is not None:
                    record["state_version"] = self._state_version
                if self._omitted:
                    record["omitted_objects"] = self._omitted
                if picked == EXPAND_STATE:
                    self._expansions += 1
                    record["expanded_to"] = self._expansions
                    await self._send(record)
                    continue
                if picked == REPLAN:
                    await self._send(record)
                    play.stopped = "REPLAN: the decision model found no listed action that can progress from this screen"
                    play.replan_requested = True
                    break
                if picked == NONE_FITS:
                    await self._send(record)
                    none_since = none_since or now
                    remaining = spec.no_action_grace_seconds - (now - none_since)
                    if remaining <= 0:
                        stuck = f"no action fit for {spec.no_action_grace_seconds:g}s"
                        if can_ask_planner:
                            # 지금 action 들로는 진행할 수 없다는 뜻이다. planner 가 다시 쓸 길이 남았으면 그쪽으로 간다(묶음 2 의
                            # 복구 표). 런 172 step 4 는 적이 화면에 있는데 Jev 가 다섯 번 고를 것이 없다고 해 멈췄다.
                            play.repair_request = {
                                "action": None, "misses": None,
                                "reason": f"The decision model found no listed action that fits the screen for {spec.no_action_grace_seconds:g}s",
                            }
                            play.stopped = f"REPAIR: {stuck}"
                        else:
                            play.stopped = stuck
                        break
                    await channel.wait_for_reading(min(remaining, IDLE_LOOK_SECONDS))
                    continue
                none_since = None
                contract_effect: tuple | None = None
                if instance is not None:
                    prepared = await self._prepare_contract(instance, macros, step, record)
                    if prepared is None:
                        await self._send(record)
                        await channel.wait_for_reading(IDLE_LOOK_SECONDS)
                        continue
                    definition, allowed, contract_effect = prepared
                    action_run = await self._run(definition, step, allowed)
                    action_run = await self._judge_contract(action_run, contract_effect, step, record)
                    effect = None
                    effect_before = None
                definition = macros.actions.get(picked) if instance is None else definition
                if instance is None and target is not None:
                    if target not in self._candidates(macros.picks[picked].names):
                        # 고른 사이에 대상이 사라졌다. 대신 다른 것을 치지 않고 이 turn 을 버리고 다시 고른다.
                        record["target_gone"] = True
                        await self._send(record)
                        await channel.wait_for_reading(IDLE_LOOK_SECONDS)
                        continue
                    definition = await macros.picks[picked].bind(target)
                    if await self._pick_ready(macros.picks[picked], target, step) == "unmet":
                        # 고른 사이에 대상의 준비 조건이 깨졌다(적이 쓰러짐). 실패로 세지 않고 다시 고른다.
                        record["not_ready_at_run"] = True
                        await self._send(record)
                        await channel.wait_for_reading(IDLE_LOOK_SECONDS)
                        continue
                if instance is None:
                    if picked in macros.readies and await self._predicate(macros.readies[picked], step, READY_FLAG) == "unmet":
                        # 고른 사이에 준비 조건이 깨졌다. 실패로 세지 않고 다시 고른다.
                        record["not_ready_at_run"] = True
                        await self._send(record)
                        await channel.wait_for_reading(IDLE_LOOK_SECONDS)
                        continue
                    effect = macros.effects.get(picked)
                    # 효과는 바뀌어야 효과다: 실행 전에 이미 참이던 조건이 실행 뒤에도 참인 것은 증거가 아니다.
                    effect_before = await self._predicate(effect, step) if effect is not None else None
                    action_run = await self._run(definition, step)
                    if picked.startswith("developer:"):
                        record["legacy"] = True
                outcome, reason, sent = action_run.outcome, action_run.reason, action_run.sent
                unobserved = unobserved + 1 if outcome == "unobserved" else 0
                if unobserved >= MAX_UNOBSERVED_IN_A_ROW:
                    outcome = "failed"
                record.update(
                    outcome=outcome, reason=reason, sent=sent,
                    flags=[flagged.model_dump(mode="json") for flagged in action_run.flags],
                )
                if outcome == "failed":
                    unseen = unseen_selectors(reason or "", channel.scene.pulse)
                    if unseen:
                        record["unseen_selectors"] = unseen
                        play.unseen_selectors = sorted(set(play.unseen_selectors) | set(unseen))
                failures = failures + 1 if outcome == "failed" else 0
                idle = outcome == "passed" and not sent
                idle_turns = idle_turns + 1 if idle else 0
                if sent:
                    parked.clear()
                    idled.clear()
                # `after` 는 앞 행동이 된 뒤에만 다음 행동을 정한다. 계약 행동의 효과가 안 났으면 뒤를 강제하지 않는다.
                if outcome == "passed" and sent and record.get("result") != "effect_unmet":
                    follow = macros.after.get(picked, ())
                stalled = idle and picked in idled
                if record.get("result") == "effect_unmet":
                    # 계약의 효과가 기한 안에 나타나지 않았다. 헛돎으로 센다.
                    stalled = True
                if outcome == "passed" and sent and effect is not None:
                    # 입력이 간 것과 효과가 난 것은 다르다(개선 계획 묶음 2). planner 가 쓴 효과 조건을 읽어 남기고, 효과가
                    # 안 난 것은 헛돎으로 센다 — 통과하고 입력도 보내 다른 규칙에 안 걸리는 반복을 일찍 잡는다.
                    await self._settle()
                    after = await self._predicate(effect, step)
                    record["effect"] = "already" if effect_before == "met" and after == "met" else after
                    if record["effect"] == "unmet":
                        stalled = True
                if idle:
                    # 할 일이 없어 아무것도 안 한 것이지 고칠 macro 가 아니다. 처음에는 헛돎으로 세지 않고 잠시 뺀다.
                    parked.add(picked)
                    idled.add(picked)
                if outcome == "failed" or stalled:
                    misses[picked] = misses.get(picked, 0) + 1
                    record["misses"] = misses[picked]
                missed = misses.get(picked, 0)
                if missed >= spec.repair_after and can_ask_planner:
                    # planner 가 step 목표와 다른 action, 값의 변화를 함께 보고 다시 쓴다(개선 계획 묶음 2). editor 는 그 action
                    # 하나만 보고 고쳐, 실패를 다른 동작으로 덮거나(런 164) 없는 경로로 다시 썼다(런 165).
                    await self._send(record)
                    why = reason if reason is not None else "it passed and sent its input, but its effect did not show"
                    play.repair_request = {"action": picked, "misses": missed, "reason": why}
                    play.stopped = f"REPAIR: `{picked}` failed {missed} times in this step (last: {why})"
                    break
                if missed >= spec.repair_after and repair is not None and repairs < spec.max_repairs_per_step:
                    # 멈춤 규칙보다 먼저다: 그 action 이 헛돈 turn 기록을 들고 editor 를 부르고, 답이 오면 그것으로 잇는다.
                    repairs += 1
                    evidence = [
                        turn for turn in play.turns
                        if turn.get("picked") == picked and (turn.get("outcome") == "failed" or not turn.get("sent"))
                    ][-missed:]
                    fixed = await repair(macros, picked, evidence)
                    record["repair"] = {"action": picked, "attempt": repairs, "replaced": fixed is not None}
                    await self._send(record)
                    if fixed is not None:
                        macros = fixed
                        failures = idle_turns = 0
                        misses.pop(picked, None)
                        # 고친 macro 는 새로 시험한다. 잠시 뺀 것과 입력 없이 끝난 기록도 비운다.
                        parked.clear()
                        idled.clear()
                        continue
                else:
                    await self._send(record)
                if missed >= spec.max_misses_per_action and picked not in dropped:
                    # 고칠 수 없거나 고쳐도 또 헛돈 action 은 이 step 에서 다시 고르지 못하게 뺀다.
                    dropped.append(picked)
                if failures >= spec.max_failures_in_a_row:
                    stuck = f"{failures} actions failed in a row (last: {picked}: {reason})"
                    if can_ask_planner:
                        # 복구 예산이 남았으면 멈추지 않고 planner 에게 넘긴다. 런 184 는 두 action 이 번갈아 실패해 이 규칙에 먼저
                        # 걸려, 복구 한 번이 남은 채 멈췄다.
                        play.repair_request = {"action": picked, "misses": None, "reason": stuck}
                        play.stopped = f"REPAIR: {stuck}"
                    else:
                        play.stopped = stuck
                    break
                if idle_turns >= spec.max_idle_turns_in_a_row:
                    play.stopped = (
                        f"no progress: {idle_turns} actions in a row ran without sending the game an input "
                        f"(last: {picked})"
                    )
                    break
                if outcome == "unobserved":
                    # 다음 reading 을 기다려 다시 고른다. 같은 action 의 연속 선택으로도 세지 않는다.
                    await channel.wait_for_reading(IDLE_LOOK_SECONDS)
                    continue
                if record.get("result") == "effect_met":
                    # 효과가 난 행동은 제자리걸음이 아니다. 적을 차례로 겨누거나 대사를 넘기는 반복이 여기서 끊겼다.
                    same_in_a_row, last_picked = 0, None
                else:
                    same_in_a_row = same_in_a_row + 1 if picked == last_picked else 1
                    last_picked = picked
                if same_in_a_row >= spec.max_same_action_in_a_row:
                    repeated = f"`{picked}` was picked {same_in_a_row} times in a row"
                    if can_ask_planner:
                        play.repair_request = {"action": picked, "misses": None, "reason": f"{repeated} without the step moving on"}
                        play.stopped = f"REPAIR: {repeated}"
                        break
                    if picked not in dropped:
                        # 복구 예산이 없으면 멈추지 않고 그 action 을 뺀다. 남은 action 이 있으면 잇고, 없으면 선택지를 만들 때
                        # 근거와 함께 멈춘다(개선 계획 묶음 2). 런 180 은 `end_turn` 6연속에서 바로 멈췄다.
                        dropped.append(picked)
                        record["dropped"] = f"{repeated} without the step moving on"
                        await channel.note(
                            f"`{picked}` is out of this step's choices: {repeated} without the step moving on",
                            LogCategory.THOUGHT,
                            step,
                        )
                    same_in_a_row = 0
                if not sent:
                    # 보낸 것이 없으면 화면도 그대로다. 다음 reading 을 기다려 같은 화면에 바로 다시
                    # 묻지 않는다. 보낸 것이 있으면 `act_and_look` 이 이미 기다렸다.
                    await channel.wait_for_reading(IDLE_LOOK_SECONDS)
        except JevStopped as error:
            play.stopped = f"JEV-ERROR: {error}"
            if play.turns:
                play.turns[-1]["jev_error"] = str(error)
                await self._send(play.turns[-1])
        except StateStopped as error:
            play.stopped = f"STATE-SET-ERROR: {error}"
            if play.turns:
                play.turns[-1]["state_error"] = str(error)
                await self._send(play.turns[-1])
        play.end_screen = screen(channel.scene.pulse)
        await self._read_evidence(play, "end")
        if play.evidence:
            # 근거 기록을 남긴다. 남지 않으면 같은 런의 근거를 다시 볼 길이 없다(개선 계획 묶음 1).
            await self._channel.emit(
                MessageType.LOG,
                EvidenceLogPayload(
                    category=LogCategory.OBSERVATION,
                    message=f"Step {step}: game values read {len(readings(play.evidence))} times",
                    step=step,
                    evidence={**evidence_log(play.evidence, play.evidence_unknown), "changes": play.changes},
                ),
            )
        play.end_frame = channel.scene.pulse.frame
        play.end_state_version = self._state_version
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

    async def _predicate(self, probe: MacroDefinition, step: int, flag: str = EFFECT_FLAG) -> str:
        """행동의 효과·준비 조건을 지금 화면에서 읽는다. `met`, `unmet`, 읽지 못했으면 `unknown` 이다 — 못 읽은 것을 거짓으로
        세지 않는다."""
        host = ChannelMacroHost(self._channel, self._jev, step)
        result = await run_macro(host, probe, {}, step)
        if result.failure is not None:
            return "unknown"
        return "met" if any(flagged.message == flag for flagged in result.flags) else "unmet"

    def _address_hints(self, not_ready: list[str], macros: StepMacros) -> dict[str, list[str]]:
        """준비가 안 된 행동들의 준비 조건(`ready_source`)에서 앞부분이 빠진 것으로 보이는 주소(`address_hints`)."""
        by_action = getattr(macros.binder, "by_action", {})
        texts = []
        for entry in not_ready:
            key = entry.split(" ", 1)[0]
            if key in by_action:
                binder, name = by_action[key]
                texts.append(ready_source(binder.source, name))
            elif key in macros.readies:
                texts.append(ready_source(macros.source, key))
        return address_hints("\n".join(dict.fromkeys(texts)), self._channel.scene.pulse)

    async def _not_ready(self, names: list[str], macros: StepMacros, step: int) -> list[str]:
        """`names` 중 준비 조건이 거짓으로 읽힌 행동. 읽지 못한 것은 빼지 않는다."""
        return [
            name for name in names
            if name in macros.readies and await self._predicate(macros.readies[name], step, READY_FLAG) == "unmet"
        ]

    async def _state(self) -> dict[str, Any]:
        """What a keyword-less macro `ask`/`decide` sends Jev: the scene and every live object.

        `state_source="state_set"` 이면 Orchestration 의 `STATE_SET` 에서 그린다. 동기화 전이면 reading 을
        기다리며 다시 묻고, `STATE_SYNC_WAIT_SECONDS` 안에 동기화되지 않거나 읽기가 실패하면 `StateStopped` 다.
        """
        if self._spec.state_source == "pulse_memory":
            state, _clipped = build_context_state(MacroMemories(scene=self._channel.scene), MacroScope(), ())
            return state
        waited = 0.0
        while True:
            try:
                snapshot = await self._state_reader(self._channel.qa_try_id, EXECUTER_SPEC)
            except StateSetUnavailable as error:
                raise StateStopped(str(error)) from error
            if snapshot.synced:
                break
            if waited >= STATE_SYNC_WAIT_SECONDS:
                raise StateStopped(
                    f"STATE_SET not synced after {STATE_SYNC_WAIT_SECONDS:g}s "
                    f"(stream {snapshot.stream_id}, waiting for a whole pulse)"
                )
            await self._channel.wait_for_reading(IDLE_LOOK_SECONDS)
            waited += IDLE_LOOK_SECONDS
        self._state_version = snapshot.state_version
        pulse = self._channel.scene.pulse
        memories = MacroMemories(scene=self._channel.scene)
        state, clipped = render_jev_state(snapshot, pulse.is_cosmetic, scene=memories.scene_name, view=self._view())
        self._omitted = clipped if self._spec.observation == "planner" else 0
        if self._fact_sources:
            # 고른 fact 를 맨 위에 싣는다. 값은 같은 snapshot 에서 정확한 이름으로 읽고, 못 읽은 것은 `unknown` 으로 보인다.
            values, self._fact_reasons = declared_values(snapshot, self._fact_sources)
            definitions = {name: self._game_facts.get(name).definition() for name, _scope, _member in self._fact_sources}
            state = {"facts": values, "fact_definitions": definitions, **state}
        if self._omitted:
            # 조용히 자르지 않는다. Jev 가 무엇을 못 보고 고르는지 알아야 `EXPAND_STATE`·`REPLAN` 을 고를 수 있다.
            state["omitted_objects"] = self._omitted
        return state

    async def read_values(self) -> dict[str, Any] | None:
        """지금 게임 값 전부(이름: 값), 대상 해석의 시작 값이다. 출처가 `STATE_SET` 이 아니거나 읽지 못하면 `None`."""
        if self._spec.state_source != "state_set":
            return None
        try:
            snapshot = await self._state_reader(self._channel.qa_try_id, EVIDENCE_SPEC)
        except StateSetUnavailable:
            return None
        if not snapshot.synced:
            return None
        scene = snapshot.scene or MacroMemories(scene=self._channel.scene).scene_name
        reading = evidence_values(snapshot, StateView(limit=TARGET_BASELINE_CHARS), self._channel.scene.pulse.is_cosmetic, scene=scene)
        return {ACTIVE_SCENE: scene, **reading.values}

    async def _read_evidence(self, play: StepPlay, at: str) -> None:
        """활성 씬과 play 의 check 범위 값을 지금 `STATE_SET` 에서 읽어 `play.evidence` 에 더한다. 못 읽어도 play 는 멈추지
        않는다 — 근거가 모자란 것은 evaluator 가 판정에서 말한다.

        활성 씬은 check 와 상관없이 늘 적는다. 씬 이름은 게임이 보고한 값이고, 씬에 들어가는 step 의 결과가 그것이다 — 런 163
        step 3 은 planner 가 없는 멤버 이름만 check 에 적어 기록이 비었고, 전투 씬에 들어갔는데도 근거 부족으로 실패했다.
        """
        check = play.check
        if self._spec.state_source != "state_set":
            return
        try:
            snapshot = await self._state_reader(self._channel.qa_try_id, EVIDENCE_SPEC)
        except StateSetUnavailable as error:
            play.evidence.append({"at": at, "error": str(error)[:MAX_REASON_CHARS]})
            return
        if not snapshot.synced:
            play.evidence.append({"at": at, "error": "STATE_SET not synced (waiting for a whole pulse)"})
            return
        scene = snapshot.scene or MacroMemories(scene=self._channel.scene).scene_name
        values: dict[str, Any] = {ACTIVE_SCENE: scene}
        unknown: list[str] = []
        entry: dict[str, Any] = {"at": at, "state_version": snapshot.state_version, "values": values}
        names = play.target.names() if play.target is not None else []
        if names:
            # 대상을 가리키는 값은 check 범위와 상관없이 읽는다. check 의 경로가 좁으면 그 객체가 빠질 수 있다.
            values.update(evidence_values(snapshot, StateView(members=tuple(names)), self._channel.scene.pulse.is_cosmetic, scene=scene).values)
        if check is not None:
            view = StateView(paths=check.paths, members=check.members)
            reading = evidence_values(snapshot, view, self._channel.scene.pulse.is_cosmetic, scene=scene)
            values.update(reading.values)
            # 지금 없는 경로를 거절하지 않는다 — 다음 씬에 생길 객체가 있다. 끝까지 한 번도 맞지 않은 이름만 남는다.
            unknown = reading.unknown_members + [f"path {path}" for path in unmatched_paths(snapshot, check.paths)]
            # 완전하지 않은 읽기에서 빠진 이름은 부재가 아니라 미확인이다. 그 근거를 기록에 남긴다.
            if reading.clipped:
                entry["clipped"] = reading.clipped
            if reading.omitted:
                entry["omitted"] = reading.omitted
        if at == "start":
            self._evidence_start = snapshot
        elif at == "end" and self._evidence_start is not None:
            play.changes, play.changes_left_out = state_changes(
                self._evidence_start, snapshot, self._channel.scene.pulse.is_cosmetic
            )
        read_before = any("values" in recorded for recorded in play.evidence)
        play.evidence.append(entry)
        # 한 번이라도 맞은 이름은 모르는 이름이 아니다. 씬이 바뀌어야 생기는 멤버가 있다.
        play.evidence_unknown = sorted(set(play.evidence_unknown) & set(unknown) if read_before else set(unknown))
        observe = self._observe
        missed = set(unmatched_paths(snapshot, observe.paths if observe is not None else None))
        play.observe_unknown = sorted(set(play.observe_unknown) & missed if read_before else missed)

    def _candidate_ids(self, names: tuple[str, ...]) -> list[tuple[str, int | None]]:
        """`_candidates` 의 selector 와 그 객체의 런타임 id. id 는 실행 직전에 같은 객체인지 확인하는 데 쓴다."""
        scene = self._scene()
        found: dict[str, int | None] = {}
        for held in self._channel.scene.pulse.held.values():
            if not held.live or held.scene not in (None, scene, PERSISTENT_SCENE):
                continue
            address = held.selector or held.path
            if address and _INDEX.sub("", address.rstrip("/").rsplit("/", 1)[-1]) in names:
                found[address] = held.id
        return sorted(found.items())[:MAX_PICK_CANDIDATES]

    async def _domain_ids(self, contract: "ActionContract", param: str, step: int) -> list[tuple[str, int | None]]:
        """인자 하나의 후보 `(selector, 런타임 id)`. 등록 계약은 그 인자의 `holds` 멤버를 가진 객체, planner 계약은 이름이다."""
        member = contract.holds.get(param)
        if member is None:
            return self._candidate_ids(contract.domains[param])
        return await self._holder_ids(member, step)

    async def _holder_ids(self, member: str, step: int) -> list[tuple[str, int | None]]:
        """`member`(`Type::member`)를 가진 객체 중 지금 씬에서 켜진 것. SDK `read_member` 가 holder 마다 보낸 id·selector 다.

        SDK 가 상속을 풀어 holder 를 찾으므로, 파생 타입 이름으로 감시되는 객체(`MagicEnemy`)도 선언 타입(`Combat.Enemies.Enemy`)으로
        찾는다. 받은 값은 공통 reader 에도 적어 둔다 — 준비 조건이 같은 멤버를 읽으면 그 객체의 값을 쓴다. holder 의 id 를 보내지
        않는 옛 SDK 에서는 후보가 없다.
        """
        answer = await ChannelMacroHost(self._channel, self._jev, step).read_member(member)
        if answer is None:
            return []
        _apply_read_member(self._channel.scene.pulse, member, answer)
        scene = self._scene()
        live = {
            held.selector or held.path
            for held in self._channel.scene.pulse.held.values()
            if held.live and held.scene in (None, scene, PERSISTENT_SCENE)
        }
        found = {
            one.selector: one.id
            for one in answer.values
            if one.active and one.selector and one.id is not None and one.selector in live
        }
        return sorted(found.items())[:MAX_PICK_CANDIDATES]

    async def _contract_options(
        self, names: list[str], macros: StepMacros, step: int
    ) -> tuple[dict[str, "ContractInstance"], list[str]]:
        """계약 행동의 후보: 바인딩마다 준비 조건이 참인 것. 거짓·확인 불가는 `<후보> (unmet|unknown)` 으로 따로 돌려준다."""
        instances: dict[str, ContractInstance] = {}
        unready: list[str] = []
        if macros.binder is None:
            return instances, unready
        for name in names:
            contract = macros.contracts[name]
            per_param = [await self._domain_ids(contract, param, step) for param in contract.params]
            for combo in itertools.islice(itertools.product(*per_param), MAX_PICK_CANDIDATES):
                binding = tuple((param, selector) for param, (selector, _id) in zip(contract.params, combo))
                ids = {selector: object_id for selector, object_id in combo}
                key = name if not binding else f"{name}{PICK_SEPARATOR}{' + '.join(selector for _p, selector in binding)}"
                probe = await macros.binder.ready(name, binding)
                ready = "met" if probe is None else await self._predicate(probe, step, READY_FLAG)
                if ready != "met":
                    unready.append(f"{key} ({ready})")
                    continue
                description = macros.description(name)
                if binding:
                    description += "\n\nThis option acts on " + ", ".join(f"`{selector}` as {param}" for param, selector in binding) + "."
                instances[key] = ContractInstance(name, binding, ids, description)
        return instances, unready

    def _alive(self, instance: "ContractInstance") -> bool:
        """바인딩한 객체가 지금도 같은 객체로 켜져 있는가. id 를 모르면 selector 로만 본다(약한 식별).

        켜진 객체 둘이 같은 selector 를 가지면 입력이 어느 쪽에 갈지 모른다 — 하나를 고르지 않고 살아 있지 않은 것으로 본다.
        전에는 selector 마다 마지막 객체만 남겨, 다른 객체가 같은 selector 를 가져도 확인을 통과했다."""
        live: dict[str, int | None] = {}
        shared: set[str] = set()
        for held in self._channel.scene.pulse.held.values():
            if not held.live:
                continue
            key = held.selector or held.path
            if key in live and live[key] != held.id:
                shared.add(key)
            live[key] = held.id
        for _param, selector in instance.binding:
            if selector not in live or selector in shared:
                return False
            expected = instance.ids.get(selector)
            if expected is not None and live[selector] is not None and live[selector] != expected:
                return False
        return True

    async def _prepare_contract(
        self, instance: "ContractInstance", macros: StepMacros, step: int, record: dict
    ) -> tuple[MacroDefinition, frozenset[str], tuple] | None:
        """실행 직전 확인: 대상이 같은 객체로 살아 있고 준비 조건이 아직 참인가. 아니면 `record["result"]` 를 남기고 `None`."""
        assert macros.binder is not None
        contract = macros.contracts[instance.action]
        if not self._alive(instance):
            record["result"] = "stale_target"
            return None
        probe = await macros.binder.ready(instance.action, instance.binding)
        if probe is not None:
            ready = await self._predicate(probe, step, READY_FLAG)
            if ready != "met":
                record["result"] = "precondition_false" if ready == "unmet" else "precondition_unknown"
                return None
        effect_probe = await macros.binder.effect(instance.action, instance.binding)
        before = (
            await self._predicate(effect_probe, step)
            if effect_probe is not None and contract.effect.mode == "transition"
            else None
        )
        definition = await macros.binder.body(instance.action, instance.binding)
        allowed = frozenset(selector for _param, selector in instance.binding) | frozenset(contract.fixed_targets)
        record["contract_revision"] = contract.revision
        return definition, allowed, (effect_probe, contract.effect, before)

    async def _judge_contract(self, action_run: "ActionRun", effect: tuple, step: int, record: dict) -> "ActionRun":
        """계약 행동의 결과를 엔진이 정한다. body 가 통과해도 효과가 나야 `effect_met` 이다. 입력이 갔다는 것만으로 통과가 아니다."""
        if action_run.outcome != "passed":
            reason = action_run.reason or ""
            record["result"] = (
                "contract_error" if reason.startswith("CONTRACT_ERROR")
                else "input_rejected" if reason.startswith("the game refused an input")
                else "stale_target" if reason.startswith(("SELECTOR_NOT_FOUND", "STALE_BINDING"))
                else "precondition_false" if reason.startswith("SCENE_MISMATCH")
                else "failed"
            )
            return action_run
        probe, contract, before = effect
        if contract.mode == "unobservable" or probe is None:
            record["result"] = "unobservable"
            return action_run
        deadline = self._clock() + contract.within
        known = False
        seen_met = False
        while True:
            now = await self._predicate(probe, step)
            known = known or now != "unknown"
            seen_met = seen_met or now == "met"
            # transition 은 거짓이던 것이 참이 되는 것이다. 실행 전에 읽지 못한 것은 거짓의 증거가 아니다.
            if now == "met" and (contract.mode == "state" or before == "unmet"):
                record["result"] = "effect_met"
                return action_run
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            await self._channel.wait_for_reading(min(remaining, IDLE_LOOK_SECONDS / 2))
        if contract.mode == "transition" and before == "met":
            # 실행 전에 이미 참이던 조건은 이 행동의 효과를 말하지 못한다. 헛돎으로도 세지 않는다.
            record["result"] = "already_true"
            return action_run
        if contract.mode == "transition" and before == "unknown" and seen_met:
            record["result"] = "unobserved"
            return ActionRun(
                "unobserved",
                "the effect holds now, but it could not be read before the action, so the change is not shown",
                action_run.sent,
                action_run.flags,
            )
        if not known:
            record["result"] = "unobserved"
            return ActionRun("unobserved", "the effect could not be read before its deadline", action_run.sent, action_run.flags)
        record["result"] = "effect_unmet"
        return action_run

    async def _expand_picks(
        self, options: dict[str, str], macros: StepMacros, step: int
    ) -> tuple[dict[str, str], list[str]]:
        """`pick` 을 남긴 action 을 지금의 후보 객체마다 하나씩으로 펼친 선택지와, 준비 조건이 거짓이라 뺀 후보.

        준비 조건은 그 developer macro 앞부분의 `require` 다. 읽지 못한 후보는 빼지 않는다 — `ready_<action>` 과 같다.
        런 190~219 는 쓰러진 적이 후보에 남아, Jev 가 고르면 `require` 가 "already defeated" 로 거절하기를 되풀이했다.
        """
        expanded: dict[str, str] = {}
        unready: list[str] = []
        for name, description in options.items():
            pick = macros.picks.get(name)
            if pick is None:
                expanded[name] = description
                continue
            for path in self._candidates(pick.names):
                key = f"{name}{PICK_SEPARATOR}{path}"
                if await self._pick_ready(pick, path, step) == "unmet":
                    unready.append(f"{key} (unmet)")
                    continue
                expanded[key] = f"{description}\n\nThis option acts on `{path}`."
        return expanded, unready

    async def _pick_ready(self, pick: ActionPick, path: str, step: int) -> str:
        """고른 객체에 대한 그 macro 앞부분 `require` 의 결과. 조건이 없으면 `met` 이다."""
        if pick.ready is None:
            return "met"
        probe = await pick.ready(path)
        if probe is None:
            return "unknown"
        return await self._predicate(probe, step, READY_FLAG)

    def _candidates(self, names: tuple[str, ...]) -> list[str]:
        """지금 씬(과 씬을 넘어 사는 객체)에서 켜져 있고 이름(마지막 마디, `[n]` 무시)이 `names` 중 하나인 객체의 `selector`.

        `selector` 를 쓴다. `path` 는 색인이 없어 같은 이름의 객체가 하나로 뭉친다(런 186: 손패 카드 여러 장이 `Card(Clone)`
        선택지 하나가 됐다). `selector` 는 지금의 주소이지 영구 식별자가 아니므로 실행 직전에 다시 확인한다.
        """
        scene = self._scene()
        found = {
            address
            for held in self._channel.scene.pulse.held.values()
            if held.live and (held.scene in (None, scene, PERSISTENT_SCENE))
            for address in [held.selector or held.path]
            if address and _INDEX.sub("", address.rstrip("/").rsplit("/", 1)[-1]) in names
        }
        return sorted(found)[:MAX_PICK_CANDIDATES]

    def now(self) -> float:
        """executer 의 시계. runner 가 step 예산을 계획들에 걸쳐 이어 셀 때 같은 시계를 쓴다."""
        return self._clock()

    def _planned(self) -> bool:
        """planner 가 범위를 정하는 런인가. `PulseMemory` 출처는 범위를 좁히지 않는다."""
        return self._spec.observation == "planner" and self._spec.state_source == "state_set"

    def _view(self) -> StateView:
        """지금 Jev 에게 보일 범위. 단계 0 은 planner 의 ```observe, 1 은 path 를 풀고, 2 는 전부다."""
        if not self._planned():
            return EVERYTHING
        observe = self._observe or StepObserve()
        # 범위는 씬 객체·DontDestroyOnLoad 객체·static 에 똑같이 걸리고, 가린 것은 `hidden` 에 그룹으로만 남는다.
        common = {
            "planned": True, "limit": self._spec.jev_state_chars,
            "facts": tuple((scope, member) for _name, scope, member in self._fact_sources),
        }
        if self._expansions == 0:
            return StateView(paths=observe.paths, members=observe.members, **common)
        if self._expansions == 1:
            return StateView(members=observe.members, **common)
        return StateView(**common)

    def _view_record(self) -> dict[str, Any]:
        """turn 기록의 `view`: Jev 에게 보이도록 허용한 범위. 범위를 안 좁히는 런은 `{"all": true}` 다."""
        if not self._planned():
            return {"all": True}
        view = self._view()
        record = {"paths": view.paths, "members": view.members, "level": self._expansions}
        if self._fact_sources:
            record["facts"] = [name for name, _scope, _member in self._fact_sources]
        return record

    def _narrows(self) -> bool:
        """지금 범위보다 넓힐 것이 있는가. 범위를 좁혔거나 상한에 걸려 빠진 것이 있을 때다."""
        if not self._planned() or self._expansions >= 2:
            return False
        observe = self._observe
        narrowed = observe is not None and (observe.paths is not None or observe.members is not None)
        return narrowed or self._omitted > 0

    async def _ask_goal(self, action: str) -> float:
        try:
            return await self._jev.probability(
                f"{GOAL_QUESTION} The step: {action}",
                await self._state(),
                true_means=ASK_TRUE_MEANS,
                false_means=ASK_FALSE_MEANS,
            )
        except JevError as error:
            raise JevStopped(str(error)) from error

    async def _pick(
        self,
        question: str,
        options: dict[str, str],
        recent: list[str] | None = None,
        state: dict[str, Any] | None = None,
    ) -> str:
        """Jev 가 고른 action. `recent` 가 있으면 state 에 `recent_actions` 로 함께 간다 — 화면이 안 움직인
        turn 은 화면만으로는 첫 turn 과 같아, 방금 무엇을 했고 그것이 입력을 보냈는지를 Jev 가 알아야
        같은 수를 되풀이하지 않는다(ARTEL-1288, 런 153)."""
        state = dict(state) if state is not None else await self._state()
        if recent:
            state["recent_actions"] = recent
        try:
            return await self._jev.choose(
                question,
                {**options, NONE_FITS: NONE_FITS_DESCRIPTION},
                state,
            )
        except JevError as error:
            raise JevStopped(str(error)) from error

    async def _run(self, definition: MacroDefinition, step: int, allowed: frozenset[str] | None = None) -> ActionRun:
        """실패나 pause 전까지 기록된 flags도 실행 결과와 함께 보존한다. `allowed` 가 있으면 그 밖의 대상에 가는 입력은 보내지
        않는다(계약 행동)."""
        host = ChannelMacroHost(self._channel, self._jev, step, allowed_targets=allowed)
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
        if host.contract_violation is not None:
            return ActionRun("failed", f"CONTRACT_ERROR: {host.contract_violation}"[:MAX_REASON_CHARS], host.sent, flags)
        if result.failure is not None:
            reason = f"{result.failure.code} at {result.stopped_at}: {result.failure.reason}"
            if result.failure.code == ACTION_REJECTED and result.failure.payload.get("gaps"):
                # SDK 가 자른 reading 에서는 부재를 판정할 수 없다. action 이 틀린 것이 아니라 관측이 모자란 것이다(개선 계획
                # 묶음 3, 런 182: 잘린 reading 의 `absent()` 거절 셋이 실패 3연속으로 세어져 play 가 멈췄다).
                return ActionRun("unobserved", reason[:MAX_REASON_CHARS], host.sent, flags)
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


def merge_plays(plays: list[StepPlay]) -> StepPlay:
    """step 하나의 play 들(재계획·planner 복구로 이어진 것)을 evaluator 가 읽을 한 기록으로. 하나뿐이면 그대로다.

    turn 에는 `plan`(몇 번째 계획인가)을 달고, 근거 기록의 `at` 앞에 `plan n · ` 를 붙인다. 앞선 계획에서 쓴 자원이나 어긴
    제약이 평가에서 빠지지 않게 한다(개선 계획 묶음 2). 멈춘 이유와 마지막 화면은 마지막 play 의 것이다.
    """
    if len(plays) == 1:
        return plays[0]
    first, last = plays[0], plays[-1]
    turns = [{**turn, "plan": number} for number, play in enumerate(plays, start=1) for turn in play.turns]
    evidence = [
        {**entry, "at": f"plan {number} · {entry['at']}"}
        for number, play in enumerate(plays, start=1)
        for entry in play.evidence
    ]
    unknown = set(first.evidence_unknown)
    for play in plays[1:]:
        unknown &= set(play.evidence_unknown)
    observe_unknown = set(first.observe_unknown)
    for play in plays[1:]:
        observe_unknown &= set(play.observe_unknown)
    changes = [f"plan {number} · {line}" for number, play in enumerate(plays, start=1) for line in play.changes]
    return StepPlay(
        last.stopped,
        turns=turns,
        start_screen=first.start_screen,
        end_screen=last.end_screen,
        seconds=round(sum(play.seconds for play in plays), 1),
        start_frame=first.start_frame,
        end_frame=last.end_frame,
        started_at=first.started_at,
        ended_at=last.ended_at,
        replan_requested=last.replan_requested,
        end_state_version=last.end_state_version,
        check=last.check,
        evidence=evidence,
        evidence_unknown=sorted(unknown),
        observe_unknown=sorted(observe_unknown),
        unseen_selectors=sorted({name for play in plays for name in play.unseen_selectors}),
        repair_request=last.repair_request,
        changes=changes,
        changes_left_out=sum(play.changes_left_out for play in plays),
        target=last.target,
        state_source=last.state_source,
    )


def recent_action_lines(turns: list[dict], limit: int = RECENT_ACTIONS_SHOWN) -> list[str]:
    """The last `RECENT_ACTIONS_SHOWN` turns that ran an action, one line each, oldest first.

    `turn 11: prepare_any_spell → passed, sent no input to the game` is the shape; a failure adds its
    reason and a `flag(...)` adds its message. Turns that picked `none_fits` or only read `done()` are
    left out — they changed nothing Jev needs to know about.
    """
    ran = [turn for turn in turns if turn.get("picked") and "outcome" in turn]
    lines = []
    for turn in ran[-limit:]:
        sent = turn.get("sent") or []
        line = ("before the new plan, " if turn.get("earlier_plan") else "") + f"turn {turn.get('turn')}: {turn['picked']} → {turn['outcome']}, "
        line += f"sent {len(sent)} input{'s' if len(sent) != 1 else ''} to the game" if sent else "sent no input to the game"
        if turn.get("reason"):
            line += f" ({str(turn['reason'])[:MAX_REASON_CHARS]})"
        if effect_missed(turn):
            line += "; its effect did not show"
        for flagged in turn.get("flags") or []:
            message = flagged.get("message") if isinstance(flagged, dict) else str(flagged)
            if message:
                line += f"; flag: {message}"
        if turn.get("repair"):
            line += "; the macros were rewritten after this" if turn["repair"].get("replaced") else "; a rewrite was refused after this"
        lines.append(line)
    return lines


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
    if _size(body) > MAX_TURN_LOG_BYTES and "jev_state" in body:
        # 큰 것은 Jev state 다. 잘린 글로 바꿔 나머지 기록은 살린다.
        drawn = json.dumps(body.pop("jev_state"), ensure_ascii=False)
        body["jev_state_clipped"] = True
        room = max(MAX_TURN_LOG_BYTES - _size(body) - 200, 0)
        # 따옴표가 escape 되며 글이 늘어나므로, 맞을 때까지 줄인다.
        while True:
            body["jev_state_text"] = drawn.encode()[:room].decode(errors="ignore")
            if _size(body) <= MAX_TURN_LOG_BYTES or room == 0:
                break
            room = max(room - (_size(body) - MAX_TURN_LOG_BYTES) - 64, 0)
    if _size(body) > MAX_TURN_LOG_BYTES:
        body = {key: record.get(key) for key in ("step", "turn", "done", "picked", "outcome")}
    return TurnLogPayload(
        category=LogCategory.OBSERVATION, message=", ".join(parts), step=record.get("step"), turn=body
    )


def _size(body: dict) -> int:
    return len(json.dumps(body, ensure_ascii=False).encode())


def _now() -> str:
    """UTC ISO 8601, 초 단위. orchestration 의 envelope `timestamp` 와 같은 눈금이다."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
