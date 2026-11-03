"""런 단위 행동 장부. turn 마다 실패를 종류로 나눠 쌓고, 그 action 의 지금 버전에 걸리는 구조적 결함만 계획 검증에 쓴다.

실패했다는 사실만으로 action 을 막지 않는다. 종류마다 적용 범위가 다르다.

| 종류 | 뜻 | 범위 |
|---|---|---|
| `structural` | 조건이 이 런에서 한 번도 보고되지 않은 객체를 기다렸다 — 그 소스로는 통과할 수 없다 | 그 소스 버전, 런 끝까지 |
| `precondition` | `require` 가 거짓 — 지금 상태에서 할 수 없었다 | 그 turn |
| `target_gone` | 가리킨 객체가 없거나 바뀌었다 | 그 대상 |
| `unobserved` | 잘린 reading 이라 판정하지 못했다 | 판단 불가 |
| `effect_unmet` | 통과하고 입력도 갔는데 효과 조건이 바뀌지 않았다 | 기록만 |
| `timeout` | 기다린 조건이 한도 안에 오지 않았다 | 기록만 |
| `refused_input` | 게임이 입력을 거절했다 | 기록만 |

버전은 소스 글의 hash 다. 이름만으로 막으면 고친 수정본을 막거나, 이름만 바꿔 같은 결함을 다시 들여온다.
"""

import ast
import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.agents.planner_executer.developer_macros import DEVELOPER_ACTION_PREFIX, LoadedDeveloperMacros
from app.agents.planner_executer.executer import PICK_SEPARATOR, StepPlay, effect_missed, unseen_selectors
from app.agents.planner_executer.macros import StepMacros


class FailureKind(StrEnum):
    STRUCTURAL = "structural"
    PRECONDITION = "precondition"
    TARGET_GONE = "target_gone"
    UNOBSERVED = "unobserved"
    EFFECT_UNMET = "effect_unmet"
    TIMEOUT = "timeout"
    REFUSED_INPUT = "refused_input"
    OTHER = "other"


def classify(turn: dict) -> FailureKind | None:
    """turn 하나의 실패 종류. 실패가 아니면 `None`."""
    if turn.get("outcome") == "unobserved":
        return FailureKind.UNOBSERVED
    if turn.get("target_gone"):
        return FailureKind.TARGET_GONE
    if turn.get("outcome") == "passed":
        return FailureKind.EFFECT_UNMET if effect_missed(turn) else None
    if turn.get("outcome") != "failed":
        return None
    reason = turn.get("reason") or ""
    if turn.get("unseen_selectors"):
        return FailureKind.STRUCTURAL
    if reason.startswith(("SELECTOR_NOT_FOUND", "STALE_BINDING", "SELECTOR_AMBIGUOUS")):
        return FailureKind.TARGET_GONE
    if reason.startswith("SCENE_MISMATCH"):
        return FailureKind.PRECONDITION
    if reason.startswith("WAIT_TIMEOUT"):
        return FailureKind.TIMEOUT
    if reason.startswith("the game refused an input"):
        return FailureKind.REFUSED_INPUT
    return FailureKind.OTHER


def version(source: str) -> str:
    return hashlib.sha256(source.strip().encode()).hexdigest()[:12]


def action_sources(macros: StepMacros, developer: LoadedDeveloperMacros | None) -> dict[str, str]:
    """action 이름 → 그 action 의 소스 글. developer macro 는 프로젝트에 올라간 본문, planner 의 `def` 는 그 `def` 의 글이다."""
    sources: dict[str, str] = {}
    try:
        tree = ast.parse(macros.source)
    except SyntaxError:
        tree = None
    defs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)} if tree is not None else {}
    for name in [*macros.actions, *macros.contracts]:
        if name.startswith(DEVELOPER_ACTION_PREFIX):
            base = name.removeprefix(DEVELOPER_ACTION_PREFIX).split("#", 1)[0]
            usable = developer.usable.get(base) if developer is not None else None
            if usable is not None:
                sources[name] = usable.macro.source
        elif name in defs:
            sources[name] = ast.get_source_segment(macros.source, defs[name]) or ""
    return sources


def identity(action: str) -> str:
    """버전과 함께 결함을 묶는 이름. developer macro 는 `#2` 를 뺀 macro 이름, planner 의 `def` 는 그 이름이다."""
    return action.split("#", 1)[0] if action.startswith(DEVELOPER_ACTION_PREFIX) else action


@dataclass(frozen=True)
class LedgerEntry:
    action: str
    version: str
    kind: FailureKind
    step: int
    turn: int | None
    target: str | None
    reason: str
    # 그 실패가 가리킨, 이 런에서 아직 보고된 적 없던 selector(구조적 결함의 근거).
    unseen: tuple[str, ...] = ()


@dataclass
class ActionLedger:
    """런 하나의 장부. runner 가 step 의 play 마다 `record` 하고, planner 가 계획을 검증할 때 `structural` 을 읽는다."""

    entries: list[LedgerEntry] = field(default_factory=list)

    def record(self, step: int, play: StepPlay, macros: StepMacros, developer: LoadedDeveloperMacros | None) -> None:
        sources = action_sources(macros, developer)
        for turn in play.turns:
            kind = classify(turn)
            picked = turn.get("picked")
            if kind is None or not isinstance(picked, str):
                continue
            action = picked.split(PICK_SEPARATOR, 1)[0]
            source = sources.get(action)
            if source is None:
                continue
            self.entries.append(
                LedgerEntry(
                    identity(action), version(source), kind, step, turn.get("turn"), turn.get("target"),
                    (turn.get("reason") or "")[:300], tuple(turn.get("unseen_selectors") or ()),
                )
            )

    def structural(self, pulse: Any = None) -> dict[tuple[str, str], LedgerEntry]:
        """`(이름, 버전)` → 그 버전이 이 런에서 통과할 수 없다는 첫 근거.

        근거는 본 적 없던 selector 다. 그 selector 가 그 뒤에 보고됐으면(`pulse`) 아직 안 생긴 객체였을 뿐이라 결함이 아니다 —
        다시 고를 수 있다(종합 감사 F4). 전에는 한 번 본 적 없으면 런 끝까지 막았다. `pulse` 가 없으면 전처럼 막는다."""
        found: dict[tuple[str, str], LedgerEntry] = {}
        for entry in self.entries:
            if entry.kind != FailureKind.STRUCTURAL:
                continue
            if pulse is not None and entry.unseen and not unseen_selectors(
                " ".join(f'selector("{name}")' for name in entry.unseen), pulse
            ):
                continue
            found.setdefault((entry.action, entry.version), entry)
        return found

    def check(self, macros: StepMacros, developer: LoadedDeveloperMacros | None, pulse: Any = None) -> str | None:
        """계획 검증(파서와 따로): 이 런에서 구조적 결함이 확인된 버전을 다시 고른 action 이 있으면 그 거절 문장."""
        broken = self.structural(pulse)
        if not broken:
            return None
        sources = action_sources(macros, developer)
        refused = [
            (name, broken[(identity(name), version(source))])
            for name, source in sources.items()
            if (identity(name), version(source)) in broken
        ]
        if not refused:
            return None
        lines = "; ".join(f"`{name}` — {entry.reason}" for name, entry in refused)
        return (
            f"PLAN_CHECK: this run already found these actions, in this very source, unable to succeed: {lines}. "
            "Do not select them again. Write your own `def` that does their job in a way that can hold, or use another."
        )

    def lessons(self, pulse: Any = None) -> str:
        """planner 의 요청에 싣는 글: 이 런에서 통과할 수 없다고 확인된 action. 없으면 빈 글."""
        broken = self.structural(pulse)
        if not broken:
            return ""
        lines = "\n".join(
            f"- `{name}`: {entry.reason} (step {entry.step})" for (name, _version), entry in broken.items()
        )
        return (
            "What this run found about actions (do not select these sources again; they can never succeed here):\n"
            f"{lines}\n\n"
        )
