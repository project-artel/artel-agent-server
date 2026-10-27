"""The step source the planner writes, read with the macro parser.

The planner answers each step with one macro source and nothing else:

    def <action>() -> None:      # one or more: what to do, each with a docstring
        # docstring: what it does, and when it is the right move
        ...
    def done() -> bool:          # the step's goal, read from the screen
        ...

Every `-> None` def is an action Jev may pick; its docstring is the option's description. `done`
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
import re
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
# action 하나를 지금 할 수 있는지: `can_<action>() -> bool`. 거짓이면 executer 가 그 턴의 선택지에서 뺀다.
# Jev 는 할 수 없게 된 것을 스스로 빼지 못했다 — 이미 시전한 턴에도 시전을 0.77 로 골랐다(로컬 try 184 재현).
# developer macro 에는 `developer:` 를 뺀 이름으로 쓴다(`can_end_turn` → `developer:end_turn`).
FEASIBLE_PREFIX = "can_"
# 판단용 특징값: `feature_<name>() -> bool|int|float`. executer 가 턴마다 계산해 화면 오브젝트 목록 앞에 Jev 에게
# 보탠다(JEV-Star 의 구조화된 관측). 손패 수·이번 턴 시전 여부 같은 값이 있어야 Jev 가 공격과 턴 종료를 제때 골랐다
# (로컬 try 184·185 재현).
FEATURE_PREFIX = "feature_"


# 이름표의 상한. 첫 문단이 이보다 길면 첫 문장으로 줄이고, 그래도 길면 자른다.
MAX_LABEL_CHARS = 300
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def first_sentence(text: str) -> str:
    """설명의 첫 문단, 길면 그 첫 문장. Jev 에게는 이것만 간다.

    첫 줄을 쓰던 때는 docstring 이 줄바꿈된 자리에서 문장이 끊겼다. try 193 에서 Jev 가 받은 `prepare_any_spell`
    은 "In TurnBattleScene on the player's turn, fills the combination window from the hand without choosing:"
    였다. planner 는 이름표를 첫 줄 한 문단으로 쓰므로 그대로 남고, 길게 이어 쓴 developer macro 설명은 첫 문장이 된다.
    """
    lines: list[str] = []
    for line in (text or "").splitlines():
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line.strip())
    label = " ".join(lines)
    if len(label) > MAX_LABEL_CHARS:
        label = _SENTENCE_END.split(label, maxsplit=1)[0]
    if len(label) > MAX_LABEL_CHARS:
        return label[: MAX_LABEL_CHARS - 1] + "…"
    return label


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
    # action → `can_<action>` 을 읽는 probe. 없는 action 은 늘 할 수 있는 것으로 본다.
    feasible: dict[str, MacroDefinition] = field(default_factory=dict)
    # action → Jev 에게 보내는 한 줄. developer macro 는 developer 블록 줄 끝 주석이다.
    labels: dict[str, str] = field(default_factory=dict)
    # planner 가 쓴 `feature_*` 의 이름. 값은 `probe` 정의 안의 같은 이름 `def` 로 계산한다.
    features: tuple[str, ...] = ()

    def description(self, action: str) -> str:
        if action in self.descriptions:
            return self.descriptions[action]
        return self.actions[action].entry.docstring or ""

    def label(self, action: str) -> str:
        """Jev 가 읽는 선택지: 따로 준 한 줄, 없으면 설명의 첫 문장(`developer_macros.first_sentence`)."""
        if self.labels.get(action):
            return self.labels[action]
        return first_sentence(self.description(action)) or action


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
    # Jev 가 읽는 한 줄. developer 블록 줄 끝 주석, 없으면 docstring 첫 줄.
    label: str = ""


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
    actions = [node for node in defs if _returns(node, "None")]
    if not actions and not developer_actions:
        raise MacroRejection(
            "the source needs at least one action: a `def <name>() -> None:` whose docstring says "
            "what it does and when it is the right move, or a developer macro selected for the step."
        )
    reject_unstable_selectors(source)
    parsed = await _parse_actions(actions, source, registered, developer_actions)
    probe = await registered.parse(PROBE, source.rstrip("\n") + _PROBE_SOURCE)
    descriptions = {key: action.description for key, action in developer_actions.items()}
    feasible = await _parse_feasible(defs, source, registered, parsed)
    labels = {key: action.label for key, action in developer_actions.items() if action.label}
    return StepMacros(source, parsed, probe, descriptions, feasible, labels, _features(defs))


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
    actions = [node for node in defs if _returns(node, "None")]
    if source.strip():
        reject_unstable_selectors(source)
    parsed = await _parse_actions(actions, source, registered, developer_actions)
    probes = {
        case_id: await registered.parse(PROBE, source.rstrip("\n") + _probe_source(goal_name(case_id)))
        for case_id in case_ids
    }
    descriptions = {key: action.description for key, action in developer_actions.items()}
    feasible = await _parse_feasible(defs, source, registered, parsed)
    labels = {key: action.label for key, action in developer_actions.items() if action.label}
    return SceneMacros(source, parsed, probes, descriptions, feasible, labels, _features(defs))


@dataclass(frozen=True)
class SceneMacros:
    """A FREE scene plan's macros: the shared actions and one goal probe per case."""

    source: str
    actions: dict[str, MacroDefinition]
    probes: dict[int, MacroDefinition]
    descriptions: dict[str, str] = field(default_factory=dict)
    feasible: dict[str, MacroDefinition] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    features: tuple[str, ...] = ()

    def for_case(self, case_id: int) -> StepMacros:
        """What the executer plays for one case: the shared actions, that case's goal."""
        return StepMacros(
            self.source, self.actions, self.probes[case_id], self.descriptions, self.feasible, self.labels,
            self.features,
        )


def goal_name(case_id: int) -> str:
    return f"{GOAL_PREFIX}{case_id}"


def _probe_source(goal: str) -> str:
    return f'\n\ndef {PROBE}() -> None:\n    if {goal}():\n        flag("{PROBE_FLAG}")\n'


async def _parse_actions(
    actions: list[ast.FunctionDef],
    source: str,
    registered: RegisteredMacros,
    developer_actions: Mapping[str, DeveloperAction],
) -> dict[str, MacroDefinition]:
    """Each action parsed with itself as the entry; refused when it takes parameters or has no
    docstring. The developer macros selected for the step join them under their keys."""
    parsed: dict[str, MacroDefinition] = {}
    for node in actions:
        if node.args.args:
            raise MacroRejection(
                f"`def {node.name}` takes parameters, and the executer calls every action with "
                "none. Find what it acts on inside it (`find(...)`, `selector(...)`).",
                node.lineno,
            )
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


async def _parse_feasible(
    defs: list[ast.FunctionDef], source: str, registered: RegisteredMacros, actions: dict[str, MacroDefinition]
) -> dict[str, MacroDefinition]:
    """`can_<action>` 마다 그것을 읽는 probe. 받는 action 이 없거나 모양이 틀리면 `MacroRejection`."""
    feasible: dict[str, MacroDefinition] = {}
    for node in defs:
        if not node.name.startswith(FEASIBLE_PREFIX):
            continue
        target = node.name.removeprefix(FEASIBLE_PREFIX)
        keys = [key for key in actions if _bare(key) == target]
        if not keys:
            raise MacroRejection(
                f"`def {node.name}` says when the action `{target}` can be done, but there is no action "
                f"`{target}`: no `def {target}() -> None` and no developer macro `{target}` selected.",
                node.lineno,
            )
        if not _returns(node, "bool") or node.args.args:
            raise MacroRejection(
                f"`def {node.name}` must be `-> bool` with no parameters: True when `{target}` can be done "
                "on the screen now. It only reads; it never acts.",
                node.lineno,
            )
        probe = await registered.parse(PROBE, source.rstrip("\n") + _probe_source(node.name))
        for key in keys:
            feasible[key] = probe
    return feasible


def _features(defs: list[ast.FunctionDef]) -> tuple[str, ...]:
    """`feature_<name>` 마다 그 `def` 이름. parameter 가 있거나 `-> bool|int|float` 가 아니면 `MacroRejection`."""
    names = []
    for node in defs:
        if not node.name.startswith(FEATURE_PREFIX):
            continue
        if node.args.args or not any(_returns(node, kind) for kind in ("bool", "int", "float")):
            raise MacroRejection(
                f"`def {node.name}` must take no parameters and return `-> bool`, `-> int` or `-> float`: "
                "one value the decision model reads every turn. It only reads; it never acts.",
                node.lineno,
            )
        names.append(node.name)
    return tuple(names)


def _bare(key: str) -> str:
    """`developer:end_turn#2` → `end_turn`."""
    return key.removeprefix("developer:").split("#", 1)[0]


def _returns(node: ast.FunctionDef, name: str) -> bool:
    annotation = node.returns
    if name == "None":
        return isinstance(annotation, ast.Constant) and annotation.value is None
    return isinstance(annotation, ast.Name) and annotation.id == name
