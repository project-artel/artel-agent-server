"""The step source the planner writes, read with the macro parser.

The planner answers each step with one macro source and nothing else:

    def <action>() -> None:      # one or more: what to do, each with a docstring
        # docstring: what it does, and when it is the right move
        ...
    def done() -> bool:          # the step's goal, read from the screen
        ...

Every `-> None` def **without parameters** is an action Jev may pick; its docstring is the option's
description. A `-> None` def with parameters is a helper the actions call (`fill_spell("Shoot")`) and
is not offered — the executer calls every action with no arguments, so there is nothing it could
pass (ARTEL-1288: the editor reuses developer macro sources, which are written that way). `done`
is a predicate. The text is read by `macro_definition_from_source`, the same parser the tool loop
uses when a macro is written, once per action with that action as the entry. Imports
(`from macros import ...`) are resolved against the macros registered for this build, asked for
with `MACRO_READ` the way the tool loop's `_parsed` asks. The project's developer macros are a
separate namespace (`DeveloperMacros`): the planner does not import them but selects them for a
step (`developer_macros.py`). The macro package itself is not changed.

`done` is a predicate, and the runner only runs an entry that acts. To read it on its own the
source is parsed once more with a generated entry, `executer_probe_done`, that does nothing but
`if done(): flag("done")`; the executer runs that entry and looks for the flag.
"""

import ast
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.model import MacroDefinition
from app.agents.qa.macro.parser import imported_names, macro_definition_from_source
from app.agents.qa.macro.selector_check import reject_unstable_selectors
from app.qa.channel import KnowledgeRequestFailed, QaCancelled, QaRunChannel
from app.qa.envelope import MacroReadPayload

DONE = "done"
PROBE = "executer_probe_done"
PROBE_FLAG = "done"
_PROBE_SOURCE = f'\n\ndef {PROBE}() -> None:\n    if {DONE}():\n        flag("{PROBE_FLAG}")\n'
# A FREE scene plan's goal per case: `done_<case_id>` (ARTEL-1149).
GOAL_PREFIX = "done_"


@dataclass(frozen=True)
class StepMacros:
    """One step's macros, parsed: the actions in the order written, and the `done` probe.

    `actions` 에는 planner 가 쓴 `def` 와, 그 step 에 고른 developer macro(`developer:<name>` 키)가
    함께 있다. developer macro 의 설명은 `descriptions` 에 있다.
    """

    source: str
    actions: dict[str, MacroDefinition]
    probe: MacroDefinition
    descriptions: dict[str, str] = field(default_factory=dict)

    def description(self, action: str) -> str:
        if action in self.descriptions:
            return self.descriptions[action]
        return self.actions[action].entry.docstring or ""


class _ImportResolver:
    """`from macros import` 를 한 namespace 안에서 끝까지 푼다. 이름마다 한 번씩만 찾는다."""

    # 가져온 이름을 못 찾았을 때 거절 문장 끝에 붙는 말.
    _missing_hint = ""

    def __init__(self) -> None:
        # 이름 → 파싱된 정의, 또는 못 가져온 이유.
        self._found: dict[str, MacroDefinition | str] = {}

    async def _source(self, name: str) -> tuple[str, str | None, str]:
        """`(진입점 이름, 원문, 못 찾은 이유)`. 원문이 없으면 이유가 있다."""
        raise NotImplementedError

    async def definition(
        self, name: str, importing: tuple[str, ...] = ()
    ) -> tuple[MacroDefinition | None, str]:
        """macro 하나와, 못 찾았으면 그 이유."""
        known = self._found.get(name)
        if isinstance(known, MacroDefinition):
            return known, ""
        if isinstance(known, str):
            return None, known
        entry, source, problem = await self._source(name)
        if source is not None:
            try:
                definition = await self.parse(entry, source, importing)
            except MacroRejection as rejection:
                problem = f"It is registered but no longer parses: {rejection.render()}"
            else:
                self._found[name] = definition
                return definition, ""
        self._found[name] = problem
        return None, problem

    async def parse(
        self, name: str, source: str, importing: tuple[str, ...] = ()
    ) -> MacroDefinition:
        """`source` 를 `name` 이 진입점인 macro 로 파싱한다. 못 하면 `MacroRejection`."""
        imports: dict[str, MacroDefinition] = {}
        for imported in imported_names(source):
            if imported == name or imported in importing:
                chain = " → ".join((*importing, name, imported))
                raise MacroRejection(
                    f"`{imported}` cannot be imported here, because the imports go round in "
                    f"a circle: {chain}."
                )
            definition, problem = await self.definition(imported, (*importing, name))
            if definition is None:
                raise MacroRejection(
                    f"`{imported}` is imported but cannot be found. {problem} {self._missing_hint}"
                )
            imports[imported] = definition
        return macro_definition_from_source(name, source, imports=imports)


class RegisteredMacros(_ImportResolver):
    """planner 가 쓴 글의 `from macros import` 가 가리키는 이 build 의 등록된 macro.

    `MACRO_READ` 로만 묻는다. 프로젝트의 developer macro 는 여기서 풀리지 않는다 — 이름이 같아도
    다른 macro 다(`DeveloperMacros`).
    """

    _missing_hint = "Import only macros registered in this build, or write the steps yourself."

    def __init__(self, channel: QaRunChannel) -> None:
        super().__init__()
        self._channel = channel

    async def _source(self, name: str) -> tuple[str, str | None, str]:
        try:
            answer = await self._channel.read_macro(MacroReadPayload(name=name))
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - 조회 하나로 런이 끝나면 안 된다
            return name, None, f"The content map could not be asked — {error}."
        if isinstance(answer, KnowledgeRequestFailed):
            return name, None, f"The content map does not have it — {answer.reason}."
        if answer is None:
            return name, None, "The content map did not answer."
        return answer.name or name, answer.source, ""


class DeveloperMacros(_ImportResolver):
    """프로젝트의 developer macro(이름 → 원문). 그 안의 `from macros import` 도 이 안에서만 푼다.

    build 의 등록된 macro 에 묻지 않는다. developer macro 는 같은 프로젝트의 developer macro 만
    가져오도록 검사받고 등록된다(orchestration 의 검사, ARTEL-1087).
    """

    _missing_hint = "Only another developer macro of this project can be imported there."

    def __init__(self, sources: Mapping[str, str]) -> None:
        super().__init__()
        self._sources = dict(sources)

    async def _source(self, name: str) -> tuple[str, str | None, str]:
        if name in self._sources:
            return name, self._sources[name], ""
        return name, None, "This project has no developer macro by that name."


@dataclass(frozen=True)
class DeveloperAction:
    """step 에 고른 developer macro 하나: 인자를 묶어 파싱한 정의와 Jev 가 읽을 설명."""

    definition: MacroDefinition
    description: str


async def parse_step(
    source: str,
    registered: RegisteredMacros,
    developer_actions: Mapping[str, DeveloperAction] | None = None,
) -> StepMacros:
    """The step's macros, or `MacroRejection` with what to write instead.

    The parser checks each action and `done`; this adds what the executer needs of the source as
    a whole — a `done` predicate, at least one action, no parameters, a docstring on each action.
    `developer_actions` 는 planner 가 이 step 에 고른 developer macro 다. 하나라도 있으면 planner 의
    `def` action 이 없어도 된다.
    """
    developer_actions = developer_actions or {}
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise MacroRejection(f"this is not valid Python syntax — {error.msg}.", error.lineno) from None
    defs = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if PROBE in {node.name for node in defs}:
        raise MacroRejection(f"`{PROBE}` is a name the executer uses; name the def otherwise.")
    goal = next((node for node in defs if node.name == DONE), None)
    if goal is None or not _returns(goal, "bool") or goal.args.args:
        raise MacroRejection(
            "the source needs the step's goal as `def done() -> bool:` with no parameters — a "
            "predicate that returns True once the step's goal is reached on the screen."
        )
    actions = _action_defs(defs)
    if not actions and not developer_actions:
        raise MacroRejection(
            "the source needs at least one action: a `def <name>() -> None:` with no parameters whose "
            "docstring says what it does and when it is the right move, or a developer macro selected "
            "for the step. A `-> None` def with parameters is a helper, not an action."
        )
    reject_unstable_selectors(source)
    parsed = await _parse_actions(actions, source, registered, developer_actions)
    probe = await registered.parse(PROBE, source.rstrip("\n") + _PROBE_SOURCE)
    descriptions = {key: action.description for key, action in developer_actions.items()}
    return StepMacros(source, parsed, probe, descriptions)


async def parse_scene(
    source: str,
    registered: RegisteredMacros,
    case_ids: list[int],
    developer_actions: Mapping[str, DeveloperAction] | None = None,
) -> "SceneMacros":
    """A FREE scene plan's macros (ARTEL-1149), or `MacroRejection` with what to write instead.

    The same checks as `parse_step`, except the goal: one `done_<case_id>() -> bool` per case the
    plan verifies instead of one `done`. Actions are shared by every case and may be none — a case
    whose result shows on the screen as it is needs no action.
    """
    developer_actions = developer_actions or {}
    try:
        tree = ast.parse(source) if source.strip() else ast.Module(body=[], type_ignores=[])
    except SyntaxError as error:
        raise MacroRejection(f"this is not valid Python syntax — {error.msg}.", error.lineno) from None
    defs = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if PROBE in {node.name for node in defs}:
        raise MacroRejection(f"`{PROBE}` is a name the executer uses; name the def otherwise.")
    goals = {node.name: node for node in defs if node.name.startswith(GOAL_PREFIX)}
    for case_id in case_ids:
        goal = goals.get(goal_name(case_id))
        if goal is None or not _returns(goal, "bool") or goal.args.args:
            raise MacroRejection(
                f"the plan verifies case {case_id}, so the source needs `def {goal_name(case_id)}() -> bool:` "
                "with no parameters — a predicate that returns True once that case's goal is reached "
                "on the screen."
            )
    actions = _action_defs(defs)
    if source.strip():
        reject_unstable_selectors(source)
    parsed = await _parse_actions(actions, source, registered, developer_actions)
    probes = {
        case_id: await registered.parse(PROBE, source.rstrip("\n") + _probe_source(goal_name(case_id)))
        for case_id in case_ids
    }
    descriptions = {key: action.description for key, action in developer_actions.items()}
    return SceneMacros(source, parsed, probes, descriptions)


@dataclass(frozen=True)
class SceneMacros:
    """A FREE scene plan's macros: the shared actions and one goal probe per case."""

    source: str
    actions: dict[str, MacroDefinition]
    probes: dict[int, MacroDefinition]
    descriptions: dict[str, str] = field(default_factory=dict)

    def for_case(self, case_id: int) -> StepMacros:
        """What the executer plays for one case: the shared actions, that case's goal."""
        return StepMacros(self.source, self.actions, self.probes[case_id], self.descriptions)


def goal_name(case_id: int) -> str:
    return f"{GOAL_PREFIX}{case_id}"


def _probe_source(goal: str) -> str:
    return f'\n\ndef {PROBE}() -> None:\n    if {goal}():\n        flag("{PROBE_FLAG}")\n'


def _action_defs(defs: list[ast.FunctionDef]) -> list[ast.FunctionDef]:
    """The `-> None` defs the executer offers Jev: the ones with no parameters.

    A `-> None` def with parameters is a helper. It is parsed with the actions that call it (the macro
    parser reads the whole source for every entry) and refused there if its grammar is wrong, but it
    is no option — the executer has nothing to pass it. Before ARTEL-1288 it was refused outright,
    which stopped the editor from reusing a developer macro's helpers (`fill_spell(word: string)`).
    """
    return [node for node in defs if _returns(node, "None") and not node.args.args]


async def _parse_actions(
    actions: list[ast.FunctionDef],
    source: str,
    registered: RegisteredMacros,
    developer_actions: Mapping[str, DeveloperAction],
) -> dict[str, MacroDefinition]:
    """Each action parsed with itself as the entry; refused when it has no docstring. The developer
    macros selected for the step join them under their keys."""
    parsed: dict[str, MacroDefinition] = {}
    for node in actions:
        definition = await registered.parse(node.name, source)
        if not definition.entry.docstring:
            raise MacroRejection(
                f"`def {node.name}` has no docstring. The docstring is what the decision model "
                "reads to choose between actions: say what it does and when it is the right move.",
                node.lineno,
            )
        parsed[node.name] = definition
    for key, action in developer_actions.items():
        parsed[key] = action.definition
    return parsed


def _returns(node: ast.FunctionDef, name: str) -> bool:
    annotation = node.returns
    if name == "None":
        return isinstance(annotation, ast.Constant) and annotation.value is None
    return isinstance(annotation, ast.Name) and annotation.id == name
