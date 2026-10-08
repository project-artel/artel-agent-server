"""agent 가 손으로 보낸 action tool 호출을 기록하고, 한 step 의 것을 macro 초안으로 올린다.

**왜 시스템이 초안을 만드나.** L1 에서 macro 를 켠 arm 이 18회 동안 macro 를 한 번도 안
만들었다(`macro-ab-l1b`·`-nudge`·`-memory`, 2026-10-07~08). Skills 절의 권유도, 지식화
단계의 질문도 효과가 없었다 — 결정하는 순간 agent 는 처음부터 macro 를 쓰는 대신 "남길 것
없음" 을 고른다. 그래서 쓰는 일을 시스템이 하고 agent 에게는 등록할지만 묻는다.

**기록은 보낸 순간에 바꿔 올린다(ARTEL-915, ARTEL-916).** `#12345` 나 `640,360` 을 그대로
적으면 macro 는 저장되는 순간 깨져 있다 — id 는 지금 scene 안에서만 살고 좌표는 해상도를
탄다. 그 값을 그 순간의 `PulseMemory` 와 대조해 `selector(...)` 나 `find(...)` 로 바꾼다.
action 이 돈 뒤에는 대상이 사라질 수 있으므로(카드는 조합하면 없어진다) 보내기 전에 한다.

**못 바꾸는 값은 지어내지 않는다.** 좌표 밑에 객체가 없거나 여럿이 겹치면, spawn 된 객체의
글자와 이름이 둘 다 유일하지 않으면, 그 호출은 못 올린다고 적는다. 그런 호출이 하나라도 낀
step 은 초안을 안 만든다 — 하나를 빼고 만든 초안은 다른 일을 하는 macro 다.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.agents.qa.macro.binding import _live, _segments
from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.grammar import TOOLS_BY_NAME
from app.agents.qa.macro.parser import macro_definition_from_source
from app.qa.pulse import PulseMemory

_ID = re.compile(r"^#(\d+)$")
_POINT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_INDEX = re.compile(r"\[\d+\]$")

# 런타임에 spawn 된 객체의 경로 표지. 앞의 형제 하나가 사라지면 같은 이름의 다른 객체를
# 조용히 가리키므로(`ScenePathSelectorResolver` 는 형제 번호를 안 본다) 경로로 올리지 않는다.
_SPAWNED = "(Clone)"

# 실제 tool 과 macro 문법이 인자 이름을 다르게 부르는 자리. `enter_text` 는 tool 이 id 를
# 받고 macro 가 target 을 받는다(`grammar.ToolParameter.as_instance_id`).
_REAL_NAME = {("enter_text", "target"): "target_id"}

# 초안이 되려면 step 하나에 이만큼은 보냈어야 한다. 하나뿐인 클릭은 순서가 아니다.
MIN_DRAFT_ACTIONS = 2

# 초안 첫 주석에 싣는 step 행위 문장의 길이. 이 주석이 다음 런의 macro 목록 한 줄이 되고,
# 그 줄에는 이름과 인자와 scene 도 들어간다. 시나리오 step 의 행위는 한 문장이 보통이라
# 100자면 거의 안 잘리고, 긴 설명이 들어와도 목록의 줄이 둘로 늘지 않는다.
MAX_STEP_TEXT_CHARS = 100

# scene 이 바뀔 때까지 누르는 연타는 `while scene() == "<scene>":` 하나로 쓴다. 따로 상한을
# 두지 않는다: runner 가 `while` 한 번을 `MAX_LOOP_PASSES` 회에서 `LOOP_LIMIT` 로 끊고,
# 문법에 `break` 와 `return` 이 없어 바깥에 상한용 `for` 를 한 겹 더 씌울 방법도 없다.
# 보낸 횟수가 그 상한보다 커도 같은 곳에서 멈춘다. `range(n)` 은 n 이 상한을 넘으면 저장 때
# 거절되므로, 이 쪽이 오히려 긴 대화에서 더 안전하다.
_SCENE_BOUNDED_TOOLS = frozenset({"press_key"})


@dataclass(frozen=True)
class LiftedTarget:
    """target 하나를 macro 가 적을 수 있는 모양으로 바꾼 것."""

    kind: Literal["selector", "label", "name"]
    value: str


@dataclass(frozen=True)
class DispatchRecord:
    """agent 가 손으로 부른 action tool 하나. `QaRunState.dispatches` 에 순서대로 쌓인다.

    `arguments` 는 tool 이 받은 그대로이고, `targets` 는 그중 target 자리를 보낸 순간의
    `PulseMemory` 로 바꿔 올린 것이다. 못 올렸으면 `unliftable` 에 이유가 있다.
    """

    tool: str
    step: int | None
    scene: str
    arguments: dict[str, Any]
    targets: dict[str, LiftedTarget] = field(default_factory=dict)
    unliftable: str = ""
    # 게임에 닿았나. 누름이 하나도 안 닿았거나 사람이 마우스를 쥐었으면 `False` 다.
    landed: bool = True
    # tool 이 돌아온 뒤 게임이 알린 scene. 모르면 빈 문자열이다. 같은 키 연타의 마지막
    # 호출에서 이 값이 `scene` 과 다르면 그 연타는 "scene 이 바뀔 때까지" 누른 것이다.
    scene_after: str = ""


@dataclass(frozen=True)
class Draft:
    name: str
    source: str
    actions: int


def lift_call(
    tool: str, arguments: dict[str, Any], memory: PulseMemory
) -> tuple[dict[str, LiftedTarget], str]:
    """tool 호출 하나의 target 자리를 바꿔 올린다. `(올린 것, 못 올린 이유)`."""
    spec = TOOLS_BY_NAME.get(tool)
    if spec is None:
        return {}, f"`{tool}` is not something a macro can send"
    lifted: dict[str, LiftedTarget] = {}
    for parameter in spec.parameters:
        if not parameter.is_target:
            continue
        raw = arguments.get(_REAL_NAME.get((tool, parameter.name), parameter.name))
        if raw is None:
            return {}, f"`{tool}` was sent without a `{parameter.name}`"
        found = lift_target(str(raw) if not isinstance(raw, int) else f"#{raw}", memory)
        if isinstance(found, str):
            return {}, found
        lifted[parameter.name] = found
    return lifted, ""


def lift_target(raw: str, memory: PulseMemory) -> LiftedTarget | str:
    """target 문자열 하나를 지금의 pulse 기록으로 바꿔 올린다. 못 올리면 이유 문자열."""
    live = _live(memory)
    if match := _ID.match(raw.strip()):
        wanted = int(match.group(1))
        records = [record for _key, record in live if record.id == wanted]
        if len(records) != 1:
            return f"`{raw}` matches no object the game has reported"
        return _form(records[0], live, raw)
    if match := _POINT.match(raw):
        x, y = float(match.group(1)), float(match.group(2))
        under = [record for _key, record in live if _contains(record.rect, x, y)]
        if not under:
            return f"nothing the game has reported lies under `{raw}`"
        under.sort(key=lambda record: _area(record.rect))
        if len(under) > 1 and _area(under[0].rect) == _area(under[1].rect):
            return f"two objects of the same size overlap at `{raw}`"
        return _form(under[0], live, raw)
    selector = raw.strip()
    records = [
        record for _key, record in live if selector in (record.selector, record.path)
    ]
    if records:
        return _form(records[0], live, raw)
    # pulse 에 없어도 agent 가 그 경로로 눌렀다. 고정된 경로면 그대로 믿는다.
    if _SPAWNED not in selector:
        return LiftedTarget("selector", selector)
    return f"`{raw}` is a spawned object the game has not reported"


def _form(record, live, raw: str) -> LiftedTarget | str:
    """기록 하나를 macro 가 겨눌 모양으로. 고정된 경로면 selector, spawn 된 것이면 find."""
    address = record.selector or record.path
    if address and _SPAWNED not in address:
        return LiftedTarget("selector", address)
    text = (record.text or "").strip()
    if text and sum(1 for _key, other in live if other.text == record.text) == 1:
        return LiftedTarget("label", record.text)
    if address:
        name = _INDEX.sub("", address.rsplit("/", 1)[-1])
        if sum(1 for _key, other in live if name in _segments(other)) == 1:
            return LiftedTarget("name", name)
    return f"`{raw}` is a spawned object whose label and name are not unique on screen"


def _contains(rect: dict | None, x: float, y: float) -> bool:
    if not rect:
        return False
    left, top, width, height = (rect.get(key) for key in ("x", "y", "w", "h"))
    if not all(isinstance(value, (int, float)) for value in (left, top, width, height)):
        return False
    return left <= x <= left + width and top <= y <= top + height


def _area(rect: dict | None) -> float:
    return float((rect or {}).get("w", 0) or 0) * float((rect or {}).get("h", 0) or 0)


# --- 초안 ---------------------------------------------------------------------


def draft_for_step(
    records: list[DispatchRecord], step: int, taken: set[str], step_text: str = ""
) -> tuple[Draft | None, str]:
    """한 step 에 보낸 것을 macro 초안 하나로. `(초안, 안 만든 이유)`.

    본문 첫 주석이 그 step 을 말한다(`_summary_comment`). 다음 런이 scene context 에서 이
    macro 를 목록으로 만나는데, 그 줄의 설명이 이 주석 한 줄이기 때문이다. `step_text` 는
    시나리오가 그 step 에 적은 행위 문장이고, 모르면 빈 문자열이다.

    게임에 안 닿은 호출(빈 자리를 누른 클릭)은 뺀다 — 해 본 것이지 한 것이 아니다. 못 올린
    호출이 하나라도 있으면 만들지 않는다. 같은 줄이 연달아 나오면 `for _ in range(n)` 한 줄로
    바꾼다 — 대화를 넘기는 키 연타가 이 경우다. 단 그 연타가 한 scene 에서 시작해 다른 scene
    으로 끝났으면 누른 횟수가 아니라 scene 이 바뀐 시점이 끝이므로
    `while scene() == "<scene>"` 으로 쓴다(`_fold`).
    """
    sent = [record for record in records if record.step == step and record.landed]
    if len(sent) < MIN_DRAFT_ACTIONS:
        return None, ""
    blocked = [record for record in sent if record.unliftable]
    if blocked:
        return None, blocked[0].unliftable

    units = [_unit(record, index) for index, record in enumerate(sent)]
    body: list[str] = []
    index = 0
    while index < len(units):
        run = 1
        while index + run < len(units) and _same(units[index], units[index + run]):
            run += 1
        lines = _rename(units[index], index)
        if run > 1:
            body.extend(_fold(lines, sent[index : index + run]))
        else:
            body.extend(lines)
        index += run

    name = _free_name(f"replay_step_{step}", taken)
    scene = sent[0].scene
    head = [f"def {name}() -> None:", f"    {_summary_comment(step, step_text, len(sent))}"]
    if scene:
        head.append(
            f"    require(scene() == {json.dumps(scene, ensure_ascii=False)}, "
            f"\"Open {scene} before calling this; it was drafted there.\")"
        )
    source = "\n".join(head + [f"    {line}" for line in body]) + "\n"
    try:
        macro_definition_from_source(name, source)
    except MacroRejection as rejected:
        return None, f"the draft did not parse: {rejected}"
    return Draft(name=name, source=source, actions=len(sent)), ""


def _fold(lines: list[str], run: list[DispatchRecord]) -> list[str]:
    """같은 줄이 `len(run)` 번 연달아 나온 것을 반복문으로.

    키 연타가 한 scene 안에서만 눌렸고 마지막 누름 뒤의 scene 이 다른 값으로 알려졌으면
    `while scene() == "<scene>"` 이다. 대화 길이는 런마다 다르므로 13번이라는 횟수는 우연이고
    사실은 "StoryScene 이 끝날 때까지" 눌렀다. 마지막 scene 을 모르면(빈 문자열) 바뀌었다고
    말할 근거가 없으니 `range(n)` 으로 둔다.
    """
    first, last = run[0], run[-1]
    until_changed = (
        first.tool in _SCENE_BOUNDED_TOOLS
        and first.scene
        and all(record.scene == first.scene for record in run)
        and last.scene_after
        and last.scene_after != first.scene
    )
    if until_changed:
        header = [
            f"# Press until {first.scene} is left, however many presses that takes "
            f"({len(run)} were sent by hand).",
            f"while scene() == {json.dumps(first.scene, ensure_ascii=False)}:",
        ]
    else:
        header = [f"for _ in range({len(run)}):"]
    return header + [f"    {line}" for line in lines]


def _summary_comment(step: int, step_text: str, actions: int) -> str:
    """초안 본문의 첫 주석. 이 줄이 등록된 macro 의 `summary` 로 목록에 실린다.

    `# Step 3: 대화를 끝까지 넘긴다 — 13 actions, drafted from a hand-driven run.` 모양이다.
    step 의 행위 문장은 공백을 한 칸으로 접고 `MAX_STEP_TEXT_CHARS` 에서 자른다 — 여러 줄
    문장이 들어오면 주석이 둘이 되어 첫 줄만 summary 로 읽히고, 긴 문장은 목록 한 줄을
    혼자 채운다. 문장을 모르면 step 번호와 동작 수만 적는다.
    """
    text = " ".join(step_text.split())
    if len(text) > MAX_STEP_TEXT_CHARS:
        text = text[:MAX_STEP_TEXT_CHARS].rstrip() + "…"
    label = f"Step {step}: {text} — " if text else f"Step {step}: "
    return f"# {label}{actions} actions, drafted from a hand-driven run."


def _unit(record: DispatchRecord, index: int) -> list[str]:
    """호출 하나를 macro 줄로. find 로 올린 target 은 바로 앞에 bind 하는 줄이 붙는다."""
    spec = TOOLS_BY_NAME[record.tool]
    lines: list[str] = []
    arguments: list[str] = []
    for parameter in spec.parameters:
        if parameter.is_target:
            target = record.targets[parameter.name]
            if target.kind == "selector":
                arguments.append(f"selector({json.dumps(target.value, ensure_ascii=False)})")
            else:
                bound = f"t{index}_{parameter.name}"
                lines.append(
                    f"{bound}: object = find({target.kind}="
                    f"{json.dumps(target.value, ensure_ascii=False)})"
                )
                arguments.append(bound)
            continue
        value = record.arguments.get(parameter.name)
        if value is None:
            break
        arguments.append(_literal(value))
    lines.append(f"{record.tool}({', '.join(arguments)})")
    return lines


def _same(left: list[str], right: list[str]) -> bool:
    """bind 이름의 번호만 다르면 같은 줄이다."""
    return [_NUMBERED.sub("t_", line) for line in left] == [
        _NUMBERED.sub("t_", line) for line in right
    ]


_NUMBERED = re.compile(r"\bt\d+_")


def _rename(lines: list[str], index: int) -> list[str]:
    return [_NUMBERED.sub(f"t{index}_", line) for line in lines]


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def _free_name(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    suffix = 2
    while f"{base}_{suffix}" in taken:
        suffix += 1
    return f"{base}_{suffix}"


def offer(draft: Draft) -> str:
    """`report_step` 의 답에 붙는 글. 등록이 기본이고 거절은 이름과 이유를 대야 한다.

    "한 번뿐이면 두라" 고 쓰면 agent 가 모든 step 을 한 번뿐인 것으로 읽는다(24회 실험에서
    초안 거의 전부가 거절됐다). 그 뒤 "다음 런이 등록된 macro 를 본다" 는 사실을 적어도 "다음
    런에서 다시 쓸 일이 없다" 며 거절했다(2026-10-08). 그래서 이 글은 등록하지 않는 쪽의 값을
    센다 — 다음 런이 같은 action 을 손으로 다시 보내는 횟수다. 거절은 `decline_macro_draft`
    뿐이고, 거기서 이름과 이유(재생하면 무엇이 틀어지는지)를 요구한다.

    연타가 `while scene() == ` 로 쓰였으면 보낸 횟수가 아니라 scene 이 바뀔 때까지 누른다는
    것을 한 구절로 적는다. 아니면 agent 가 `N` 번을 고정 횟수로 읽고 고치려 든다.
    """
    loop = (
        " The macro presses until the scene changes, not a fixed number of times."
        if "while scene() ==" in draft.source
        else ""
    )
    return (
        f"\n\nMacro draft ready — `{draft.name}` replays the {draft.actions} actions you "
        "sent for this step, with every id and coordinate turned into a selector or a "
        "`find`:\n\n```python\n"
        f"{draft.source}```\n"
        "This step passed, this scenario runs again on later builds, and the next run "
        "reaches this step and is shown the macros registered on this build. Without "
        f"`{draft.name}`, the next run sends these actions by hand again: "
        f"{draft.actions} tool calls.{loop} An unregistered draft is dropped when the run "
        "ends.\n"
        f"- Default: call `register_macro` with name `{draft.name}`.\n"
        f"- If something in it is wrong, fix it with `edit_macro` on `{draft.name}` first "
        "(the draft counts as already read), then register it.\n"
        "- Only if replaying it in the next run would do something wrong, call "
        f"`decline_macro_draft` with name `{draft.name}` and say in `reason` what."
    )
