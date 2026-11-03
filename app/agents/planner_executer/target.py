"""step 의 대상 계약: 시나리오가 가리키는 대상을 런 시작 때 한 번 정하고, 그 런 동안 바꾸지 않는다.

런 229 는 step 5("클리어 화면 진입")를 2스테이지의 클리어 화면으로 통과시켰다. step 4 의 planner `done` 이 지도까지 가게 썼고,
step 5 의 `done` 은 어느 스테이지의 클리어 화면이든 참이었으며, evaluator 는 근거에 스테이지 값이 바뀐 것이 있는데도 대상을
보지 않았다. 셋 다 "이 단계의 대상이 무엇인가" 가 어디에도 고정돼 있지 않아서다.

대상 계약은 둘을 나눠 정한다.

- 대상의 뜻: `TargetResolver` 가 런 시작 때 시나리오 전체, 시작 값(`STATE_SET`), 개발자의 identity 선언을 읽어 step 마다 "무엇의
  몇 번째"(`kind`, `index`)를 정한다. planner 의 답·macro 는 읽지 않는다 — 계획이 대상을 끌고 가지 못하게 한다. 이 뜻은 런 동안
  바뀌지 않는다. 플레이가 다른 대상으로 가도 기대값은 따라가지 않는다.
- 확인할 값: step 을 시작할 때 `observe` 가 코드로 고른다. 그 종류의 지금 것이라고 선언된(`role` 이 `current`) 값만 쓴다. 타이틀
  화면에서 정보 없이 값을 고르던 해석은 진행도 값으로 고정해 대상을 가려내지 못했다(런 239 step 3·6).
- `guard` 는 evaluator 의 판정 뒤에 대상 불일치·미확인·예측과 다른 움직임을 통과로 남기지 않는다. executer 는 `done()` 이 참이
  될 때 대상의 대조를 turn 에 기록만 한다.

계약은 런 로그(`step_contract`, `step_target`, `target_check`)에만 남는다.
"""

import json
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import BaseModel

# 값 대신 들어가는 상태 글(`evidence.py`·`state_set.py`). 이런 값으로는 대상을 정하거나 대조할 수 없다.
_NOT_A_VALUE = ("(absent)", "(not read)", "(unknown)", "(stale)", "(present)", "(inactive)")

Change = Literal["same", "increase", "decrease", "plus_one", "minus_one"]


class DraftChange(BaseModel):
    name: str
    change: Change


class DraftRelative(BaseModel):
    """앞 step 의 대상에 묶인 대상. `offset` 0 은 같은 것, 1 은 그다음 것이다."""

    step: int
    offset: int = 0


class DraftStep(BaseModel):
    step: int
    # 시나리오가 이 step 에 대상을 정하지 않으면(타이틀에서 시작 누르기 같은) `None` 이다.
    target: str | None = None
    # 무엇의 몇 번째인가. `kind` 는 개발자가 지금 것을 알려 주는 값을 선언한 종류 중 하나, `index` 는 0 부터 센 순서다.
    kind: str | None = None
    index: int | None = None
    relative_to: DraftRelative | None = None
    changes: list[DraftChange] = []
    note: str = ""


class TargetDraft(BaseModel):
    steps: list[DraftStep] = []


@dataclass(frozen=True)
class StepTarget:
    """step 하나의 대상. `none` 은 대상을 따지지 않는 step, `fixed` 는 뜻만 정해진 대상, `bound` 는 그 step 을 시작할 때 확인할
    값까지 정해진 대상, `unknown` 은 정하지 못한 대상이다."""

    step: int
    status: Literal["none", "fixed", "bound", "unknown"]
    target: str | None = None
    identity: tuple[tuple[str, Any], ...] = ()
    changes: tuple[tuple[str, str], ...] = ()
    reason: str = ""
    kind: str | None = None
    index: int | None = None

    def names(self) -> list[str]:
        """executer 가 근거 기록과 함께 읽을 값 이름."""
        return list(dict.fromkeys([name for name, _ in self.identity] + [name for name, _ in self.changes]))

    def logged(self) -> dict[str, Any]:
        body: dict[str, Any] = {"status": self.status, "target": self.target}
        if self.kind is not None:
            body |= {"target_kind": self.kind, "index": self.index}
        if self.identity:
            body["identity"] = [{"name": name, "equals": value} for name, value in self.identity]
        if self.changes:
            body["changes"] = [{"name": name, "change": change} for name, change in self.changes]
        if self.reason:
            body["reason"] = self.reason
        return body


@dataclass(frozen=True)
class StepContracts:
    version: int
    steps: dict[int, StepTarget] = field(default_factory=dict)

    def get(self, step: int) -> StepTarget | None:
        return self.steps.get(step)

    def with_step(self, target: StepTarget) -> "StepContracts":
        return replace(self, steps={**self.steps, target.step: target})

    def logged(self) -> dict[str, Any]:
        return {"version": self.version, "steps": {str(step): target.logged() for step, target in sorted(self.steps.items())}}


def reading_of(values: dict[str, Any], name: str) -> tuple[bool, Any]:
    """근거 기록 하나(`{"static T.m": v, "path T.m": v, ...}`)에서 `name`(`Type.member`)의 값. 정할 수 없으면 `(False, 이유)`.

    여러 객체가 같은 멤버를 서로 다른 값으로 가지면 어느 것이 대상인지 모른다 — 고르지 않는다."""
    found = [value for key, value in values.items() if key == f"static {name}" or key.endswith(f" {name}")]
    if not found:
        return False, "not read"
    distinct = {json.dumps(value, sort_keys=True, default=str) for value in found}
    if len(distinct) > 1:
        return False, f"several objects carry it with different values ({', '.join(sorted(distinct))[:120]})"
    value = found[0]
    if isinstance(value, str) and (value in _NOT_A_VALUE or value.startswith("(unread")):
        return False, value
    return True, value


def lasting(values: dict[str, Any], name: str) -> bool:
    """`name` 이 씬이 바뀌어도 남는 값인가: static 이거나 씬을 넘어 사는 객체(`@persistent/`)의 멤버.

    대상은 step 이 끝날 때 확인한다. 한 씬에만 있는 값(런 231 의 지도 씬 `MapMove.position`)은 전투 씬으로 넘어가면 읽히지 않아,
    맞게 플레이한 step 도 대상을 확인할 수 없다."""
    keys = [key for key in values if key == f"static {name}" or key.endswith(f" {name}")]
    return bool(keys) and all(key.startswith(("static ", "@persistent/")) for key in keys)


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return str(actual).lower() == str(expected).lower()
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual == expected
    return str(actual) == str(expected)


def fix_intents(draft: TargetDraft, steps: int, version: int, kinds: frozenset[str]) -> StepContracts:
    """초안에서 step 마다 대상의 뜻(무엇의 몇 번째)을 고정한다. 값 이름은 여기서 정하지 않는다(`observe`).

    `kinds` 는 개발자가 지금 것을 알려 주는 값(`role` 이 `current`)을 선언한 종류다. 그 밖의 종류이거나 순서를 정할 수 없으면 그
    step 의 대상은 `unknown` 이다. 앞 step 에 묶인 대상(`relative_to`)은 그 step 의 순서에서 센다. 앞의 것이 정해지지 않았거나
    종류가 다르면 정할 수 없다. 선언이 없으면 지금 것을 확인할 값이 없으니 대상이 있는 step 은 모두 `unknown` 이다.
    """
    drafted = {item.step: item for item in draft.steps if 1 <= item.step <= steps}
    result: dict[int, StepTarget] = {}
    for step in range(1, steps + 1):
        item = drafted.get(step)
        if item is None or not (item.target or "").strip():
            result[step] = StepTarget(step, "none")
            continue
        target = item.target.strip()
        kind = (item.kind or "").strip().lower()
        changes = tuple((entry.name, entry.change) for entry in item.changes)
        if not kinds:
            result[step] = StepTarget(step, "unknown", target, changes=changes, reason="no value is declared to tell the current one apart")
            continue
        if kind not in kinds:
            why = f"its kind {kind!r} is not one a declared value tells apart ({', '.join(sorted(kinds))})" if kind else "no kind was given"
            result[step] = StepTarget(step, "unknown", target, changes=changes, reason=why)
            continue
        index: int | None = None
        why = "no index was given"
        if item.relative_to is not None:
            earlier = result.get(item.relative_to.step) if item.relative_to.step < step else None
            if earlier is None:
                why = f"it names step {item.relative_to.step}, which is not an earlier step"
            elif earlier.status != "fixed" or earlier.kind != kind or earlier.index is None:
                why = f"step {item.relative_to.step}, which it names, has no fixed {kind}"
            else:
                index = earlier.index + item.relative_to.offset
        elif item.index is not None:
            index = item.index
        if index is None or index < 0:
            result[step] = StepTarget(step, "unknown", target, changes=changes, reason=why if index is None else f"its index {index} is below 0", kind=kind)
        else:
            result[step] = StepTarget(step, "fixed", target, changes=changes, kind=kind, index=index)
    return StepContracts(version, result)


def observe(target: StepTarget | None, values: dict[str, Any] | None, identities: tuple = ()) -> StepTarget | None:
    """step 을 시작할 때, 뜻이 정해진 대상을 확인할 값을 고른다. 모델을 부르지 않는다.

    개발자가 그 종류의 지금 것(`role` 이 `current`)이라고 선언한 값만 쓰고, 기대값은 고정된 순서에 선언의 시작 수(`base`)를 더한
    것이다. 진행도 값(`progress`)은 어느 것에 들어가도 같을 수 있어 대상을 가려내지 못한다(런 239 step 6). 지금 읽히지 않는 값도
    받는다 — 그 객체가 아직 없을 수 있고(타이틀, 지도), 끝에 확인하지 못하면 `guard` 가 통과로 남기지 않는다. 한 씬에만 사는 값과
    여러 객체가 다른 값으로 가진 값은 끝에 확인할 수 없어 뺀다. 예측한 움직임은 지금 읽히고 씬을 넘는 값만 남긴다.
    """
    if target is None or target.status != "fixed":
        return target
    now = values or {}
    identity: list[tuple[str, Any]] = []
    problems: list[str] = []
    for item in identities:
        if item.role != "current" or item.identifies.strip().lower() != target.kind:
            continue
        readable, why = reading_of(now, item.name)
        if readable and not lasting(now, item.name):
            problems.append(f"{item.name}: it lives in one scene only, so the step's end cannot read it")
        elif not readable and str(why).startswith("several objects"):
            problems.append(f"{item.name}: {why}")
        else:
            identity.append((item.name, target.index + (item.base or 0)))
    if not identity:
        reason = "; ".join(problems) or f"no declared value tells the current {target.kind} apart"
        return replace(target, status="unknown", reason=reason)
    changes = tuple((name, change) for name, change in target.changes if reading_of(now, name)[0] and lasting(now, name))
    return replace(target, status="bound", identity=tuple(identity), changes=changes)


def target_state(target: StepTarget | None, values: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """지금 값이 대상과 맞는가: `none`(따지지 않음)·`match`·`mismatch`·`unknown`, 그리고 이름마다의 대조."""
    if target is None or target.status == "none":
        return "none", []
    if target.status == "unknown":
        return "unknown", []
    rows = []
    state = "match"
    for name, expected in target.identity:
        readable, actual = reading_of(values, name)
        if not readable:
            rows.append({"name": name, "expected": expected, "actual": None, "why": actual})
            state = "unknown" if state == "match" else state
        elif _same(actual, expected):
            rows.append({"name": name, "expected": expected, "actual": actual})
        else:
            rows.append({"name": name, "expected": expected, "actual": actual})
            state = "mismatch"
    return state, rows


def change_holds(change: str, start: Any, end: Any) -> bool | None:
    """예측한 움직임이 시작·끝 값과 맞는가. 숫자가 아니라 크기를 말할 수 없으면 `None`."""
    if change == "same":
        return _same(start, end)
    if not (isinstance(start, (int, float)) and isinstance(end, (int, float))) or isinstance(start, bool) or isinstance(end, bool):
        return None
    return {
        "increase": end > start,
        "decrease": end < start,
        "plus_one": end == start + 1,
        "minus_one": end == start - 1,
    }.get(change)


def cross_check(target: StepTarget | None, first: dict[str, Any], last: dict[str, Any]) -> tuple[bool | None, list[dict[str, Any]]]:
    """런 교차 검사: step 의 처음과 끝 값이 예측한 움직임과 맞는가. 예측이 없으면 `None`."""
    if target is None or target.status != "bound" or not target.changes:
        return None, []
    rows = []
    consistent: bool | None = True
    for name, change in target.changes:
        read_start, start = reading_of(first, name)
        read_end, end = reading_of(last, name)
        holds = change_holds(change, start, end) if read_start and read_end else None
        rows.append({"name": name, "change": change, "start": start if read_start else None, "end": end if read_end else None, "holds": holds})
        if holds is False:
            consistent = False
        elif holds is None and consistent is True:
            consistent = None
    return consistent, rows


@dataclass(frozen=True)
class TargetCheck:
    """step 하나의 대상 확인: 끝 값의 대조, 교차 검사, 그리고 판정을 바꿨으면 그 까닭."""

    state: str
    identity: list[dict[str, Any]]
    consistent: bool | None
    changes: list[dict[str, Any]]
    overruled: str | None = None

    def logged(self, target: StepTarget) -> dict[str, Any]:
        return {
            "target": target.target,
            "status": target.status,
            "target_kind": target.kind,
            "index": target.index,
            "state": self.state,
            "identity": self.identity,
            "consistent": self.consistent,
            "changes": self.changes,
            "overruled": self.overruled,
        }


def guard(verdict: Any, target: StepTarget | None, first: dict[str, Any] | None, last: dict[str, Any] | None) -> tuple[Any, TargetCheck | None]:
    """evaluator 의 판정을 대상 계약으로 거른다. 대상을 따지는 step 은 대상이 맞고 움직임이 예측과 어긋나지 않을 때만 통과로 남는다.

    대상을 정하지 못했거나, 끝 값이 대상과 다르거나, 끝 값으로 대상을 확인할 수 없거나, 예측한 움직임과 실제가 어긋나면 통과가
    아니다. 넷 다 근거 부족(`evidence_sufficient=False`)으로 남긴다. 대상이 어긋난 것은 대개 게임이 아니라 플레이나 해석이 다른
    곳으로 간 것이고, 게임 결함인지는 실패 분석이 turn 의 입력을 보고 가린다. 값의 확인 상태로 나누지 않는다.
    """
    if target is None or target.status == "none":
        return verdict, None
    if target.status != "bound":
        return _overrule(verdict, TargetCheck("unknown", [], None, []), f"the step's target could not be fixed ({target.reason})", False)
    state, rows = target_state(target, last or {})
    consistent, changes = cross_check(target, first or {}, last or {})
    check = TargetCheck(state, rows, consistent, changes)
    if state == "mismatch":
        wrong = [row for row in rows if row.get("actual") is not None and not _same(row["actual"], row["expected"])]
        shown = ", ".join(f"{row['name']} is {row['actual']!r}, not {row['expected']!r}" for row in wrong)
        return _overrule(verdict, check, f"play ended on another target than {target.target!r}: {shown}", False)
    if state == "unknown":
        return _overrule(verdict, check, f"the target {target.target!r} could not be confirmed at the end", False)
    if consistent is False:
        moved = ", ".join(f"{row['name']} {row['start']!r} → {row['end']!r} (expected {row['change']})" for row in changes if row["holds"] is False)
        return _overrule(verdict, check, f"the target's values moved unlike the contract: {moved}", False)
    return verdict, check


def _overrule(verdict: Any, check: TargetCheck, why: str, sufficient: bool) -> tuple[Any, TargetCheck]:
    """통과였으면 실패로 바꾸고 까닭을 남긴다. 이미 실패면 판정은 그대로 두고 대조만 기록한다."""
    if not verdict.passed:
        return verdict, check
    overruled = replace(
        verdict,
        passed=False,
        message=f"TARGET-CHECK: {why}. The evaluator had passed it: {verdict.message}",
        evidence_sufficient=sufficient,
    )
    return overruled, replace(check, overruled=why)


def planner_note(contracts: StepContracts | None, step: int, next_step_text: str | None) -> str:
    """planner 요청에 붙는 계약 글. 계획 대상이 아니라 읽기만 하는 맥락이다."""
    if contracts is None:
        return ""
    lines = []
    target = contracts.get(step)
    if target is not None and target.status == "bound":
        named = ", ".join(f"{name} = {json.dumps(value, ensure_ascii=False)}" for name, value in target.identity)
        lines.append(f"This step's target is fixed for the run and you cannot change it: {target.target} (told apart by {named}).")
        lines.append("Write done() so that it holds on that target only.")
    elif target is not None and target.status == "unknown":
        lines.append(f"This step's target could not be fixed ({target.target}): the step cannot pass on a target nobody confirmed.")
    if next_step_text:
        lines.append(f"The next step will ask: {next_step_text}. Stop this step before that; do not play into the next step's goal.")
    return ("Step contract (read only):\n" + "\n".join(f"- {line}" for line in lines) + "\n\n") if lines else ""


def evaluator_note(target: StepTarget | None) -> str:
    """evaluator 요청에 붙는 계약 글. 값은 근거 기록에 함께 실린다."""
    if target is None or target.status == "none":
        return ""
    if target.status == "unknown":
        return f"The step's target: {target.target} — it could not be fixed ({target.reason}).\n\n"
    named = ", ".join(f"{name} = {json.dumps(value, ensure_ascii=False)}" for name, value in target.identity)
    return (
        f"The step's target, fixed for the run: {target.target}. The game values {named} tell it apart; they are among the "
        "values below. A result on another target is not this step's result.\n\n"
    )


class TargetResolver:
    """런 시작 때 한 번 도는 대상 해석. 사용자의 Claude 키로 run 의 모델을 부른다(Jev 가 아니다).

    step 마다 대상의 뜻(무엇의 몇 번째)만 정한다. 확인할 값은 고르지 않는다 — 그 값은 step 을 시작할 때 `observe` 가 선언에서
    고른다. planner 의 답·macro 는 받지 않는다. 시나리오 전체, 시작 값, 선언만 읽는다. `ask` 는 테스트가 바꿔 끼운다.
    """

    def __init__(self, config: Any, ask: Any = None, identities: tuple = ()) -> None:
        from app.agents.planner_executer.prompts import TARGET_RESOLVER_ROLE, prompt_text

        self._config = config
        self._identities = tuple(identities)
        self._kinds = frozenset(item.identifies.strip().lower() for item in self._identities if item.role == "current")
        self._system = prompt_text(TARGET_RESOLVER_ROLE, config.prompt_version)
        self._ask = ask or self._ask_model

    async def resolve(self, scenario: Any, baseline: dict[str, Any] | None, version: int = 1) -> StepContracts:
        """시작 값은 시나리오가 시작 상태에 견줘 말한 대상("다음 스테이지")을 순서로 바꾸는 데만 쓴다."""
        from app.agents.planner_executer.prompts import this_step

        steps = len(scenario.steps)
        meanings = "".join(f"{item.line()}\n" for item in self._identities)
        user = (
            "Kinds the game's developer declared a value for that tells the current one apart: "
            + (", ".join(sorted(self._kinds)) or "(none)")
            + "\n\n"
            + (f"What the game's developer declared these values mean:\n{meanings}\n" if meanings else "")
            + "The scenario, every step:\n"
            + "\n".join(this_step(scenario, step) for step in range(1, steps + 1))
            + "\n\nThe game values now (name: value):\n"
            + ("\n".join(f"{name}: {json.dumps(value, ensure_ascii=False, default=str)}" for name, value in (baseline or {}).items()) or "(not read)")
            + "\n"
        )
        try:
            answer = await self._ask(self._system, user)
            draft = answer if isinstance(answer, TargetDraft) else TargetDraft.model_validate(answer if isinstance(answer, dict) else json.loads(answer))
        except Exception as error:  # noqa: BLE001 - an unreadable draft must not stop the run; it runs without a contract
            return _unreadable(steps, version, f"{type(error).__name__}: {str(error)[:160]}")
        return fix_intents(draft, steps, version, self._kinds)

    async def _ask_model(self, system: str, user: str) -> TargetDraft:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.llm.chat_model import structured

        chain = structured(self._config.model, TargetDraft, self._config.reasoning)
        return await chain.ainvoke([SystemMessage(system), HumanMessage(content=user)])


def _unreadable(steps: int, version: int, why: str) -> StepContracts:
    """해석의 답을 읽지 못한 런. 어느 step 이 대상을 가리키는지조차 모르므로 계약 없이(`none`) 돌고, 까닭은 로그에 남는다."""
    return StepContracts(version, {step: StepTarget(step, "none", reason=f"the target draft could not be read: {why}") for step in range(1, steps + 1)})
