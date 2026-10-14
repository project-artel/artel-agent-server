"""한 `scene` visit 동안 부른 macro 를 엮어 stage 하나를 치우는 초안으로 만든다 (ARTEL-1026).

**왜 step 초안과 따로 있나.** `lift.draft_for_step` 은 한 step 에 손으로 보낸 action 을 올린다.
macro 를 켠 런은 이미 등록된 macro 를 `run_macro` 로 부르는데, 그 호출은 `dispatches` 에 안
남아 step 초안이 비거나 빈약하다. 정작 다음 런에 값진 것은 "이 `scene` 에서 어떤 macro 를 어떤
순서로 부르면 `scene` 이 끝나나" 이고, 그것은 `MacroCallLog` 에 있다.

**무엇을 만드나.** entry `def clear_<scene>()` 하나가 `while scene() == "<scene>":` 안에서
그 visit 에서 부른 macro 를 첫 등장 순서대로 부른다. 부른 macro 의 등록된 원문이 helper 로
통째로 복사된다. 한 번의 `run_macro` 로 stage 를 치운다는 것이 목적이다.

**원문을 문자열로 고친다.** 이름이 겹치면 이름을 바꿔야 하는데, `ast.unparse` 는 따옴표를
바꾸고 주석을 버린다(`parser.py`, `model.py` 가 쓰지 않는 이유와 같다). 그래서 `ast` 로 이름의
위치만 찾고 그 글자만 갈아 끼운다. `lift._rename` 은 `find` 로 bind 한 이름의 번호를 맞추는
함수라 쓰임이 다르다.

**만들지 않는 경우.** 책에 없는 macro(등록되지 않은 것), `checkpoint` 가 든 macro(턴마다 모델
호출이 한 번씩 든다), 리터럴로 못 적는 인자(`object` 인자가 `#id`·좌표·빈 값·spawn 된 경로),
그리고 `macro_definition_from_source` 가 거절하는 결과(깊이 3, statement 128, 이름 충돌).
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any

from app.agents.qa.macro.book import MacroBook
from app.agents.qa.macro.call_log import MacroCall, MacroCallLog
from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.grammar import CHECKPOINT, MacroType
from app.agents.qa.macro.model import MacroDefinition, MacroParameter
from app.agents.qa.macro.lift import _SPAWNED
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.macro.selector_check import is_unstable_selector, reject_unstable_selectors

# 한 visit 에서 이만큼은 불렀어야 한다. 하나뿐인 호출은 그 macro 자체가 이미 초안이다.
MIN_VISIT_CALLS = 2


@dataclass(frozen=True)
class StageDraft:
    name: str
    source: str
    scene: str
    # 초안이 부르는 macro 의 이름, 첫 등장 순서대로.
    macros: tuple[str, ...]
    # 이 초안의 근거가 된 `run_macro` 호출 수.
    calls: int
    # 그 visit 에서 macro 를 부를 때 서 있던 `screen.id`. 등록할 때 이것으로 관계를 맺는다.
    screens: tuple[str, ...] = ()


def stage_draft_for_visit(
    log: MacroCallLog,
    book: MacroBook,
    standing_scene: str,
    taken: set[str],
    handled_visits: set[int],
) -> tuple[StageDraft | None, str]:
    """끝난 visit 중 아직 따져 보지 않은 것의 초안, 최근 것부터. `(초안, 안 만든 이유)`.

    `handled_visits` 는 이미 따져 본 visit 의 번호이고 이 함수가 채운다. 초안을 못 만든
    visit 도 담는다 — 같은 visit 은 다음 `report_step` 에서도 같은 이유로 못 만든다.
    가장 최근 호출만 보지 않는 이유: 전투 1 에서 세 번 부르고 Reward 를 지나 전투 2 에서 한
    번 부르면, 전투 2 가 끝난 뒤 가장 최근 호출은 전투 2 의 것이라 전투 1 을 영영 놓친다.
    네트워크를 읽지 않는다. 정의는 `book` 에 있는 것만 쓴다.
    """
    standing = standing_scene.strip()
    if not log.calls or not standing:
        return None, ""
    visits: list[int] = []
    for call in reversed(log.calls):
        if call.visit not in visits:
            visits.append(call.visit)
    why = ""
    for visit in visits:
        if visit in handled_visits:
            continue
        calls = log.calls_in_visit(visit)
        scene = calls[0].scene
        if not scene:
            continue
        if visit == log.visit and scene == standing:
            # 아직 그 `scene` 에 있다. visit 이 끝나지 않았다.
            continue
        # 여기서부터 이 visit 은 끝났다. 어떻게 끝나든 한 번만 따진다.
        handled_visits.add(visit)
        draft, why = _draft_for(calls, scene, book, taken)
        if draft is not None:
            return draft, ""
    return None, why


def _draft_for(
    visit_calls: list[MacroCall], scene: str, book: MacroBook, taken: set[str]
) -> tuple[StageDraft | None, str]:
    """끝난 visit 하나의 초안."""

    # 끝까지 간 호출만 쓴다. 실패했거나 멈춘 호출을 되풀이하는 초안은 같은 곳에서 또 멈춘다.
    ran = [call for call in visit_calls if call.passed and not call.paused]
    if len(ran) < MIN_VISIT_CALLS:
        return None, ""

    first_calls: dict[str, MacroCall] = {}
    for call in ran:
        first_calls.setdefault(call.name, call)

    definitions: list[MacroDefinition] = []
    for name in first_calls:
        registered = book.registered(name)
        if registered is None:
            return None, f"`{name}` is not in this run's book"
        if _calls_checkpoint(registered.definition.source):
            return None, f"`{name}` has a checkpoint, which would cost a model call per turn"
        definitions.append(registered.definition)

    lines: list[str] = []
    for definition, call in zip(definitions, first_calls.values()):
        written, why = _call_line(definition, call.arguments)
        if written is None:
            return None, why
        lines.append(written)

    entry = _free_name(f"clear_{_snake(scene)}", taken)
    names = tuple(first_calls)
    source = _assemble(entry, scene, names, definitions, lines, len(ran))
    try:
        macro_definition_from_source(entry, source)
        # 복사한 helper 는 저장된 원문이라 `#id` 를 담고 있을 수 있다. `register_macro` 가
        # 그것을 거절하므로, 등록이 기본인 초안이라면 여기서 내지 않는다.
        reject_unstable_selectors(source)
    except MacroRejection as rejected:
        return None, f"the stage draft did not parse: {rejected}"
    screens = tuple(dict.fromkeys(call.screen for call in ran if call.screen))
    return StageDraft(entry, source, scene, names, len(ran), screens), ""


# --- 조립 ---------------------------------------------------------------------


def _assemble(
    entry: str,
    scene: str,
    names: tuple[str, ...],
    definitions: list[MacroDefinition],
    lines: list[str],
    calls: int,
) -> str:
    """entry `def` 와 복사한 macro 원문을 한 소스로. helper 이름이 겹치면 바꾼다."""
    quoted = json.dumps(scene, ensure_ascii=False)
    used = {entry}
    copies: list[str] = []
    call_lines = list(lines)
    for index, definition in enumerate(definitions):
        renames = _renames(definition, used)
        used.update(renames.get(function, function) for function in _function_names(definition))
        copies.append(_rename_in_source(definition.source, renames).strip("\n"))
        # entry macro 가 바뀐 이름으로 불려야 한다.
        new_entry = renames.get(definition.name)
        if new_entry is not None:
            call_lines[index] = new_entry + call_lines[index][len(definition.name) :]

    docstring = _docstring(scene, names, calls)
    head = [
        f"def {entry}() -> None:",
        f"    {docstring}",
        f"    require(scene() == {quoted}, "
        f"{json.dumps(f'Open {scene} before calling this; it was drafted there.', ensure_ascii=False)})",
        f"    while scene() == {quoted}:",
    ]
    head += [f"        {line}" for line in call_lines]
    return "\n".join(head) + "\n\n\n" + "\n\n\n".join(copies) + "\n"


def _docstring(scene: str, names: tuple[str, ...], calls: int) -> str:
    """등록된 macro 의 `summary` 로 목록에 실리는 줄. 세 겹 따옴표 안에서 닫히지 않게 escape 한다."""
    combined = ", ".join(names)
    text = (
        f"Clear {scene} by calling {combined} until the scene is left "
        f"(drafted from {calls} run_macro calls)."
    )
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"""{escaped}"""'


def _function_names(definition: MacroDefinition) -> list[str]:
    return [definition.entry.name, *(helper.name for helper in definition.helpers)]


def _renames(definition: MacroDefinition, used: set[str]) -> dict[str, str]:
    """이 macro 의 `def` 중 `used` 와 겹치는 것의 새 이름.

    entry 는 번호 접미사만 붙이고, helper 는 macro 이름을 앞에 붙인다. 그래도 겹치면
    번호를 붙인다. 새 이름도 같은 macro 의 다른 `def` 와 겹치면 안 되어 함께 센다.
    """
    own = set(_function_names(definition))
    taken = set(used) | own
    renames: dict[str, str] = {}
    for function in _function_names(definition):
        if function not in used:
            continue
        base = function if function == definition.name else f"{definition.name}_{function}"
        fresh = _free_name(base, taken)
        taken.add(fresh)
        renames[function] = fresh
    return renames


def _rename_in_source(source: str, renames: dict[str, str]) -> str:
    """`def` 이름과 호출 자리의 이름만 글자 단위로 바꾼다. 나머지 글자는 그대로다."""
    if not renames:
        return source
    lines = source.split("\n")
    edits: list[tuple[int, int, int, str]] = []  # (줄 번호, 시작 byte, 끝 byte, 새 이름)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name in renames:
            raw = lines[node.lineno - 1].encode("utf-8")
            found = re.compile(rb"def\s+").match(raw, node.col_offset)
            if found is not None:
                start = found.end()
                edits.append((node.lineno - 1, start, start + len(node.name), renames[node.name]))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in renames
        ):
            start = node.func.col_offset
            edits.append(
                (node.func.lineno - 1, start, start + len(node.func.id), renames[node.func.id])
            )
    for row, start, end, fresh in sorted(edits, reverse=True):
        raw = lines[row].encode("utf-8")
        lines[row] = (raw[:start] + fresh.encode("utf-8") + raw[end:]).decode("utf-8")
    return "\n".join(lines)


def _calls_checkpoint(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return True
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == CHECKPOINT
        for node in ast.walk(tree)
    )


# --- 호출 줄 ------------------------------------------------------------------


def _call_line(definition: MacroDefinition, given: dict[str, Any]) -> tuple[str | None, str]:
    """macro 하나를 부르는 줄. 인자 하나라도 리터럴로 못 쓰면 `(None, 이유)`."""
    parameters = {parameter.name: parameter for parameter in definition.entry.parameters}
    unknown = [name for name in given if name not in parameters]
    if unknown:
        return None, f"`{definition.name}` was called with {', '.join(unknown)}, which it does not take"
    written: list[str] = []
    for parameter in definition.entry.parameters:
        if parameter.name not in given:
            if parameter.default is None:
                return None, f"`{definition.name}` was called without `{parameter.name}`"
            continue
        literal = _argument(parameter, given[parameter.name])
        if literal is None:
            return None, (
                f"`{parameter.name}` of `{definition.name}` cannot be written as a literal "
                f"({given[parameter.name]!r})"
            )
        written.append(f"{parameter.name}={literal}")
    return f"{definition.name}({', '.join(written)})", ""


def _argument(parameter: MacroParameter, value: Any) -> str | None:
    """인자 하나를 parser 가 받는 리터럴로. 못 쓰면 `None`."""
    declared = parameter.declared_type
    if declared is MacroType.object_:
        if not isinstance(value, str):
            return None
        selector = value.strip()
        if is_unstable_selector(selector) or selector.startswith("-") or _SPAWNED in selector:
            return None
        if not _is_selector(selector):
            return None
        return f"selector({json.dumps(selector, ensure_ascii=False)})"
    if declared is MacroType.bool_:
        return str(value) if isinstance(value, bool) else None
    if isinstance(value, bool):
        return None
    if declared is MacroType.int_:
        return repr(value) if isinstance(value, int) else None
    if declared is MacroType.float_:
        return repr(float(value)) if isinstance(value, (int, float)) else None
    if declared is MacroType.string:
        return json.dumps(value, ensure_ascii=False) if isinstance(value, str) else None
    return None


def _is_selector(text: str) -> bool:
    """`run_macro` 가 selector 로 읽는 글인가. 좌표나 id 로 읽히면 아니다."""
    from app.agents.qa.tools.action_tools import parse_target

    try:
        parsed = parse_target(text)
    except ValueError:
        return False
    return len(parsed) == 1 and isinstance(parsed[0], str)


# --- 이름 ---------------------------------------------------------------------


def _snake(scene: str) -> str:
    """`StoryScene` → `story_scene`, `Battle 2` → `battle_2`. ASCII 만 남기고 비면 `scene`."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", scene)
    snake = re.sub(r"[^0-9a-zA-Z]+", "_", spaced).strip("_").lower()
    return snake or "scene"


def _free_name(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    suffix = 2
    while f"{base}_{suffix}" in taken:
        suffix += 1
    return f"{base}_{suffix}"


# --- 답에 붙는 글 -------------------------------------------------------------


def offer_stage(draft: StageDraft) -> str:
    """`report_step` 의 답에 붙는 글. 등록이 기본이고 거절은 `decline_macro_draft` 뿐이다."""
    combined = ", ".join(f"`{name}`" for name in draft.macros)
    return (
        f"\n\nStage draft ready — `{draft.name}` combines {combined}, the macros you called "
        f"{draft.calls} times while you were in {draft.scene}. It calls them in the order you "
        f"first called them, with the arguments you gave, for as long as the scene is "
        f"{draft.scene}; each macro's registered source is copied into it:\n\n"
        f"```python\n{draft.source}```\n"
        "The next run that reaches this scene can then clear it with one `run_macro` call "
        f"instead of {draft.calls}. An unregistered draft is dropped when the run ends.\n"
        f"- Default: call `register_macro` with name `{draft.name}`.\n"
        f"- If something in it is wrong, fix it with `edit_macro` on `{draft.name}` first "
        "(the draft counts as already read), then register it.\n"
        "- Only if replaying it in the next run would do something wrong, call "
        f"`decline_macro_draft` with name `{draft.name}` and say in `reason` what."
    )
