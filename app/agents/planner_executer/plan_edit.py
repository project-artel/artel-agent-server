"""planner 복구를 지금 계획에 대한 수정으로 받는다.

복구 답은 계획 전체를 다시 쓰지 않는다. ```changes 블록이 기준 계획 버전과 교체·제거할 행동(이유와 함께)을 말하고, 새로 쓰거나
바꾼 `def`, 더할 developer macro, 바뀐 ```observe·```check·```after 만 함께 온다. 명시적으로 바꾸지 않은 것은 그대로 남는다.
합친 계획은 처음 쓴 계획과 같은 검사(파서·선후·계획 검증)를 거친 뒤 통째로 바뀐다. 기준 버전이 다르면 거절한다 — 오래된
계획에 부분 적용되어 둘이 섞이지 않게 한다.

런 186 은 매번 처음부터 다시 쓰는 복구가 앞 계획에서 고장으로 드러난 developer macro 를 다시 들여왔다.
"""

import ast
import json
import re
from dataclasses import dataclass, field

from app.agents.planner_executer.macros import effect_name, ready_name
from app.agents.qa.macro.errors import MacroRejection

_CHANGES_BLOCK = re.compile(r"```changes\s*\n(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class PlanDraft:
    """지금 계획의 글 조각들. 처음 쓴 답이든 수정을 합친 것이든 같은 모양이다."""

    version: int
    source: str
    # `(action 이름, ```developer 줄)`. 이름은 `bind_developer_calls` 가 붙인 그대로다(`developer:x`, `developer:x#2`).
    calls: tuple[tuple[str, str], ...]
    actions: tuple[str, ...]
    observe_text: str | None = None
    check_text: str | None = None
    after_text: str | None = None
    summary: str | None = None
    # 계약 모드의 ```actions 블록.
    actions_text: str | None = None
    # 행동 지침 모드의 ```guidance 블록.
    guidance_text: str | None = None
    # 그 지침을 런의 언어로 사람에게 옮긴 ```guidance_summary 블록(prompt v4). 로그에만 실린다.
    guidance_summary: str | None = None
    # ```facts 블록.
    facts_text: str | None = None


@dataclass(frozen=True)
class Changes:
    base: int
    replace: dict[str, str] = field(default_factory=dict)
    remove: dict[str, str] = field(default_factory=dict)
    add: tuple[str, ...] = ()

    def logged(self) -> dict:
        return {"base": self.base, "replace": self.replace, "remove": self.remove, "add": list(self.add)}


def split_changes_block(answer: str) -> tuple[str, str | None]:
    match = _CHANGES_BLOCK.search(answer)
    if match is None:
        return answer, None
    return answer[: match.start()] + answer[match.end() :], match.group(1)


def read_changes(text: str | None, draft: PlanDraft) -> Changes:
    """```changes 블록, 또는 `MacroRejection`. 이름은 지금 계획의 행동이어야 하고, 교체·제거에는 이유가 있어야 한다."""
    shape = (
        '{"base": <the plan version shown>, "replace": {"<action>": "<why>"}, "remove": {"<action>": "<why>"}, '
        '"add": ["<new action>"]}'
    )
    if text is None or not text.strip():
        raise MacroRejection(f"a rewrite answers with a ```changes block: {shape}.")
    try:
        block = json.loads(text)
    except json.JSONDecodeError as error:
        raise MacroRejection(f"the ```changes block is not JSON — {error.msg}. Write {shape}.") from None
    if not isinstance(block, dict) or not isinstance(block.get("base"), int):
        raise MacroRejection(f"the ```changes block must be an object with the plan version as `base`: {shape}.")
    if block["base"] != draft.version:
        raise MacroRejection(
            f"the ```changes block is based on plan {block['base']}, but the plan now is {draft.version}. "
            "Write the changes against the plan shown."
        )
    picked: dict[str, dict[str, str]] = {}
    for key in ("replace", "remove"):
        part = block.get(key) or {}
        if not isinstance(part, dict) or not all(isinstance(why, str) and why.strip() for why in part.values()):
            raise MacroRejection(f'"{key}" in the ```changes block maps each action to the reason, as a non-empty string.')
        unknown = [name for name in part if name not in draft.actions]
        if unknown:
            raise MacroRejection(
                f'"{key}" names {", ".join(unknown)}, which is not an action of the plan: {", ".join(draft.actions)}.'
            )
        picked[key] = dict(part)
    add = block.get("add") or []
    if not isinstance(add, list) or not all(isinstance(name, str) for name in add):
        raise MacroRejection('"add" in the ```changes block is a list of the new actions\' names.')
    return Changes(block["base"], picked["replace"], picked["remove"], tuple(add))


def merge(draft: PlanDraft, changes: Changes, source: str, calls: list[str]) -> tuple[str, list[str]]:
    """지금 계획에 수정을 합친 `(macro 글, ```developer 줄들)`. 교체·제거한 행동과 그 효과·준비 조건 predicate 는 빠지고,
    새 글의 `def` 는 같은 이름을 덮거나 더해진다. 그 밖의 `def` 와 `import` 는 그대로다."""
    gone = set(changes.replace) | set(changes.remove)
    dropped = gone | {effect_name(name) for name in gone} | {ready_name(name) for name in gone}
    old_imports, old_defs = _pieces(draft.source)
    new_imports, new_defs = _pieces(source)
    defs = {name: text for name, text in old_defs.items() if name not in dropped}
    defs.update(new_defs)
    imports = list(dict.fromkeys([*old_imports, *new_imports]))
    merged = "\n".join(imports) + ("\n\n\n" if imports else "") + "\n\n\n".join(defs.values()) + "\n"
    kept_calls = [call for name, call in draft.calls if name not in gone]
    return merged, [*kept_calls, *calls]


def _pieces(source: str) -> tuple[list[str], dict[str, str]]:
    if not source.strip():
        return [], {}
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise MacroRejection(f"this is not valid Python syntax — {error.msg}.", error.lineno) from None
    imports = [ast.get_source_segment(source, node) or "" for node in tree.body if isinstance(node, ast.ImportFrom)]
    defs = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    return imports, defs


def shown(draft: PlanDraft) -> str:
    """복구 요청에 보이는 지금 계획."""
    developer = "\n".join(call for _name, call in draft.calls)
    parts = [
        f"The plan now (version {draft.version}), actions: {', '.join(draft.actions)}",
        f"```python\n{draft.source.rstrip()}\n```",
    ]
    if developer:
        parts.append(f"```developer\n{developer}\n```")
    for name, text in (
        ("guidance", draft.guidance_text), ("actions", draft.actions_text), ("observe", draft.observe_text),
        ("check", draft.check_text), ("after", draft.after_text), ("facts", draft.facts_text),
    ):
        if text and text.strip():
            parts.append(f"```{name}\n{text.strip()}\n```")
    return "\n".join(parts)
