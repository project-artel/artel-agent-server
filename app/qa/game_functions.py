"""게임 코드가 등록한 cheat 와 behavior 의 모양, 그리고 그것을 읽는 순수 함수.

계약은 workspace 의 `.plan/general/2026-10-14-game-functions-wire-contract.md` 다. 여기서
모양을 지어내지 않는다 — SDK, orchestration, artel-home 이 같은 파일을 보고 따로 만든다.

"function" 은 cheat 와 behavior 둘을 함께 부르는 말이다. 계약이 그렇게 쓴다.
"""

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

FunctionKind = Literal["cheat", "behavior"]

# test case 의 `functions` 한 칸. `"all"`, `"none"`, 이름 목록 셋 중 하나다(계약 4절).
FunctionPermission = Literal["all", "none"] | list[str]

# `target` 이 여럿에 걸렸을 때 거절문이 대는 후보 수. SDK 의 `pass target as one of` 와 같다.
MAX_TARGET_CANDIDATES = 5

# `pulse` view 의 한 줄에 대는 entry 수. instance function 은 live instance 마다 entry 가
# 하나라, 적 서른 마리에 붙은 behavior 하나가 view 를 서른 칸 늘리지 않게 자른다.
MAX_ENTRIES_PER_LINE = 12


class GameFunctionParam(BaseModel):
    """catalog entry 의 인자 하나. `min`·`max`·`default`·`values` 는 없으면 `null` 이다."""

    model_config = ConfigDict(extra="allow")

    name: str
    type: str = "string"
    min: float | None = None
    max: float | None = None
    default: Any = None
    values: list[str] | None = None


class GameFunctionTarget(BaseModel):
    """instance method 의 대상 component. static method 면 entry 의 `target` 이 `null` 이다."""

    model_config = ConfigDict(extra="allow")

    component: str


class GameFunctionEntry(BaseModel):
    """catalog entry 하나(계약 1절). SDK 에서 orchestration 을 거쳐 그대로 온다."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str = ""
    params: list[GameFunctionParam] = Field(default_factory=list)
    target: GameFunctionTarget | None = None
    group: str | None = None

    @property
    def is_static(self) -> bool:
        return self.target is None


class GameFunctionCatalog(BaseModel):
    """session open 이 준 run 의 catalog. 런 동안 바뀌지 않는다."""

    cheats: list[GameFunctionEntry] = Field(default_factory=list)
    behaviors: list[GameFunctionEntry] = Field(default_factory=list)

    def entries(self, kind: FunctionKind) -> list[GameFunctionEntry]:
        return self.cheats if kind == "cheat" else self.behaviors

    def entry(self, kind: FunctionKind, name: str) -> GameFunctionEntry | None:
        return next((one for one in self.entries(kind) if one.name == name), None)

    def names(self, kind: FunctionKind) -> list[str]:
        return [one.name for one in self.entries(kind)]


class CaseFunctions(BaseModel):
    """test case 가 쓰게 해 주는 function (계약 4절 `Test case permission`).

    키가 없으면 `"all"` 이다. run 이 받은 목록을 좁히기만 하고 넓히지 않는다 — 그 계산은
    `allowed_names` 가 한다.
    """

    model_config = ConfigDict(extra="allow")

    cheats: FunctionPermission = "all"
    behaviors: FunctionPermission = "all"

    def permission(self, kind: FunctionKind) -> FunctionPermission:
        return self.cheats if kind == "cheat" else self.behaviors


def allowed_names(
    run_names: list[str], permission: FunctionPermission | None
) -> list[str]:
    """run catalog 의 이름 중 이 test case 가 허용하는 것, run catalog 순서대로.

    빈 목록은 `"none"` 과 같다. case 목록에만 있는 이름은 빠진다 — 좁히기만 한다.
    """
    if permission is None or permission == "all":
        return list(run_names)
    if permission == "none":
        return []
    allowed = set(permission)
    return [name for name in run_names if name in allowed]


class FunctionPulseEntry(BaseModel):
    """`PULSE.functions.entries` 의 하나. `(kind, name, target)` 이 key 다."""

    model_config = ConfigDict(extra="allow")

    kind: str
    name: str
    target: str | None = None
    available: bool = True
    reason: str | None = None
    removed: bool = False

    @property
    def key(self) -> str:
        return function_key(self.kind, self.name, self.target)


def function_key(kind: str, name: str, target: str | None) -> str:
    return f"{kind}|{name}|{target or ''}"


class PulseFunctions(BaseModel):
    """`PULSE.functions`. `whole` 이면 entry 전부이고, 아니면 바뀐 것만이다."""

    model_config = ConfigDict(extra="allow")

    whole: bool = False
    entries: list[FunctionPulseEntry] = Field(default_factory=list)


class RunningBehavior(BaseModel):
    """`PULSE.behaviors` 의 하나. 지금 도는 behavior 다."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    behavior_id: int = Field(validation_alias=AliasChoices("behaviorId", "behavior_id"))
    name: str
    target: str | None = None
    seconds: float = 0.0
    max_seconds: float | None = Field(
        default=None, validation_alias=AliasChoices("maxSeconds", "max_seconds")
    )
    report: str | None = None


class BehaviorEndedPayload(BaseModel):
    """`BEHAVIOR_ENDED` envelope 의 payload. SDK frame 에서 `type` 만 뺀 것이다(계약 2절, 4절)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    behavior_id: int = Field(validation_alias=AliasChoices("behaviorId", "behavior_id"))
    name: str = ""
    target: str | None = None
    reason: str = ""
    detail: str | None = None
    seconds: float | None = None
    frame: int | None = None


class UsedCheat(BaseModel):
    """`report_step` 의 `STATUS` 에 agent-server 가 적는 cheat 호출 하나(계약 4절 `Step status`)."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    target: str | None = None
    success: bool


class ActiveBehavior(BaseModel):
    """`report_step` 의 `STATUS` 에 적는, 판정할 때 돌던 behavior 하나.

    wire 이름이 camelCase 인 `behaviorId` 다. 계약 4절의 예시가 그렇게 적는다.
    """

    behaviorId: int
    name: str
    target: str | None = None
    seconds: float


def resolve_target(partial: str, candidates: list[str]) -> tuple[str | None, str | None]:
    """일부만 적은 selector 를 후보 중 하나의 전체 selector 로 바꾼다.

    `(selector, None)` 이 성공, `(None, why)` 가 실패다. 정확히 같은 것이 있으면 그것이고,
    없으면 대소문자를 무시한 부분 일치가 정확히 하나일 때만 받는다. `inspect_object` 가
    `PulseMemory.inspect` 에서 부분 일치를 받는 것과 같은 이유다 — 블록의 주소는 길고 대괄호
    안 숫자는 씬을 다시 걸을 때마다 달라질 수 있다.

    여럿이면 고르지 않는다. 잘못 고른 instance 에 cheat 가 들어가면 그 결과는 다른 객체의
    값으로 읽힌다.
    """
    needle = partial.strip()
    if not needle:
        return None, "`target` is empty. Leave it out, or name one of the targets listed for it."
    if needle in candidates:
        return needle, None
    lowered = needle.lower()
    matches = [candidate for candidate in candidates if lowered in candidate.lower()]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        if not candidates:
            return None, (
                f"No live target matches {partial!r}: the game lists no instance for it "
                "right now. Nothing was sent."
            )
        listed = ", ".join(candidates[:MAX_TARGET_CANDIDATES])
        return None, f"No target matches {partial!r}. Nothing was sent. Targets now: {listed}"
    listed = ", ".join(matches[:MAX_TARGET_CANDIDATES])
    more = f" (+{len(matches) - MAX_TARGET_CANDIDATES} more)" if len(matches) > MAX_TARGET_CANDIDATES else ""
    return None, (
        f"{len(matches)} targets match {partial!r}. Nothing was sent. Name one of: {listed}{more}"
    )


def _number(value: float) -> str:
    """`1000000`, `0.5`. `:g` 는 `1e+06` 으로 적어 모델이 범위를 잘못 읽는다."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _param_line(param: GameFunctionParam) -> str:
    """`amount: int 1..1000000` 꼴. 모르는 칸은 안 적는다."""
    text = f"{param.name}: {param.type}"
    if param.values:
        text += f" ({'/'.join(param.values)})"
    if param.min is not None or param.max is not None:
        low = "" if param.min is None else _number(param.min)
        high = "" if param.max is None else _number(param.max)
        text += f" {low}..{high}"
    if param.default is not None:
        text += f" = {param.default!r}"
    return text


def _catalog_line(entry: GameFunctionEntry) -> str:
    params = ", ".join(_param_line(param) for param in entry.params)
    where = "static" if entry.target is None else f"on {entry.target.component}"
    group = f", group {entry.group}" if entry.group else ""
    description = f" — {entry.description.strip()}" if entry.description.strip() else ""
    return f"- {entry.name}({params})  [{where}{group}]{description}"


def render_catalog(
    cheats: list[GameFunctionEntry] | None, behaviors: list[GameFunctionEntry] | None
) -> str:
    """첫 user message 끝에 붙는 catalog. 축이 꺼진 쪽은 `None` 으로 받아 안 적는다.

    첫 message 인 이유는 `runner._macro_discovery` 와 같다. 런이 시작할 때 한 번 정해지고
    prompt 접두로 남으므로 매 호출 다시 쓰지 않는다.
    """
    sections: list[str] = []
    if cheats is not None:
        if cheats:
            lines = "\n".join(_catalog_line(entry) for entry in cheats)
            sections.append(
                "Cheats this run may call with `run_cheat` (load the cheats skill first):\n"
                f"{lines}"
            )
        else:
            sections.append("Cheats: none were given to this run.")
    if behaviors is not None:
        if behaviors:
            lines = "\n".join(_catalog_line(entry) for entry in behaviors)
            sections.append(
                "Behaviors this run may start with `start_behavior` (load the behaviors skill "
                f"first):\n{lines}"
            )
        else:
            sections.append("Behaviors: none were given to this run.")
    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)


def _entry_label(entry: FunctionPulseEntry) -> str:
    return entry.name if entry.target is None else f"{entry.name} on {entry.target}"


def _joined(items: list[str]) -> str:
    shown = items[:MAX_ENTRIES_PER_LINE]
    more = len(items) - len(shown)
    return ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")


def render_function_lines(
    entries: list[FunctionPulseEntry],
    running: list[RunningBehavior],
    allowed_cheats: list[str] | None,
    allowed_behaviors: list[str] | None,
) -> list[str]:
    """`pulse` view 의 function 줄. 축이 꺼진 쪽은 `None` 이고 그 쪽 줄이 없다.

    지금 test case 가 허용하는 이름만 그린다. 허용하지 않는 것을 그리면 agent 가 그것을 부르고
    거절당하는 왕복을 산다. `Not now` 는 게임의 `when` 이 거짓이라는 뜻이고, 그 이유를 같이
    적는다 — 부르면 SDK 가 `unavailable:` 로 거절할 것이다.
    """
    lines: list[str] = []
    for kind, title, allowed in (
        ("cheat", "Cheats", allowed_cheats),
        ("behavior", "Behaviors", allowed_behaviors),
    ):
        if allowed is None:
            continue
        names = set(allowed)
        mine = [entry for entry in entries if entry.kind == kind and entry.name in names]
        now = [_entry_label(entry) for entry in mine if entry.available]
        not_now = [
            _entry_label(entry) + (f" ({entry.reason})" if entry.reason else "")
            for entry in mine
            if not entry.available
        ]
        if now:
            lines.append(f"{title} now: {_joined(now)}")
        if not_now:
            lines.append(f"{title} not now: {_joined(not_now)}")
    if allowed_behaviors is not None:
        for behavior in running:
            lines.append(f"Behavior running: {render_running(behavior)}")
    return lines


def render_running(behavior: RunningBehavior) -> str:
    """`#7 auto_attack on <target>, 12.4s of 60s — <report>`."""
    where = f" on {behavior.target}" if behavior.target else ""
    limit = f" of {behavior.max_seconds:g}s" if behavior.max_seconds is not None else ""
    report = f" — {behavior.report}" if behavior.report else ""
    return f"#{behavior.behavior_id} {behavior.name}{where}, {behavior.seconds:g}s{limit}{report}"


def render_behavior_ended(ended: BehaviorEndedPayload) -> str:
    """`BEHAVIOR_ENDED` 하나를 다음 tool 결과 끝에 붙일 한 줄로."""
    where = f" on {ended.target}" if ended.target else ""
    detail = f" ({ended.detail})" if ended.detail else ""
    after = f" after {ended.seconds:g}s" if ended.seconds is not None else ""
    return f"Behavior ended: #{ended.behavior_id} {ended.name}{where} — {ended.reason}{detail}{after}"
