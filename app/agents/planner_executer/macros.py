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
from collections.abc import Awaitable, Callable, Mapping
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
# 행동의 효과 조건: `effect_<action>() -> bool`. developer macro 는 `developer:` 를 떼고, `#2` 는 `_2` 로 쓴다.
EFFECT_PREFIX = "effect_"
EFFECT_FLAG = "effect"


def effect_name(action: str) -> str:
    """행동 이름에 맞는 효과 조건의 이름. `developer:drag_card#2` → `effect_drag_card_2`."""
    return EFFECT_PREFIX + action.removeprefix("developer:").replace("#", "_")


# 행동의 준비 조건: `ready_<action>() -> bool`. 읽기만 하는 predicate 다. executer 가 고르기 전과 실행 직전에 읽어,
# 거짓이면 그 행동을 선택지에서 뺀다. 읽지 못하면 빼지 않는다. 이름 규칙은 효과 조건과 같다.
READY_PREFIX = "ready_"
READY_FLAG = "ready"


def ready_name(action: str) -> str:
    """행동 이름에 맞는 준비 조건의 이름. `developer:cast#2` → `ready_cast_2`."""
    return READY_PREFIX + action.removeprefix("developer:").replace("#", "_")


@dataclass(frozen=True)
class StepObserve:
    """Jev 가 이 step 에서 읽을 범위. planner 가 화면에서 읽은 글자 그대로다(`app/qa/state_set.StateView`)."""

    paths: tuple[str, ...] | None = None
    members: tuple[str, ...] | None = None


@dataclass(frozen=True)
class StepCheck:
    """evaluator 가 이 step 을 판정할 근거로 읽을 값. planner 의 ```check 블록이고 모양은 `StepObserve` 와 같다.

    executer 가 play 시작·매 turn·끝에 이 범위의 값을 `STATE_SET` 에서 읽어 `StepPlay.evidence` 에 남긴다.
    """

    paths: tuple[str, ...] | None = None
    members: tuple[str, ...] | None = None


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
    # planner 의 ```observe 블록: Jev 에게 보일 path 접두어와 `Type.member` 이름(ARTEL-1295 C). 블록이 없으면 `None`
    # 이고, executer 는 live object 전부를 보인다.
    observe: "StepObserve | None" = None
    # planner 의 ```check 블록: 판정 근거로 기록할 값. 블록이 없으면 `None` 이고 근거 값은 기록되지 않는다.
    check: "StepCheck | None" = None
    # 행동마다의 효과 조건 probe. planner 가 `effect_<action>` 을 쓴 행동만 있다.
    effects: dict[str, MacroDefinition] = field(default_factory=dict)
    # 행동마다의 준비 조건 probe. planner 가 `ready_<action>` 을 쓴 행동만 있다.
    readies: dict[str, MacroDefinition] = field(default_factory=dict)
    # planner 의 ```after 블록: 이 행동이 통과하면 다음 turn 에 고를 수 있는 행동. 준비 → 시전 같은 선후다.
    after: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 인자 하나를 매 turn 고르는 action. planner 는 ```developer 블록에 `pick("<객체 이름>", ...)` 으로 후보의 범위만
    # 적고, executer 가 turn 마다 지금 켜진 객체 중 그 이름에 맞는 것마다 선택지를 하나씩 내어 Jev 가 action 과 대상을 함께
    # 고른다.
    picks: dict[str, "ActionPick"] = field(default_factory=dict)
    # 계약 행동. 계약 모드에서 planner 가 쓴 행동은 모두 여기 있고 `actions` 에는 없다. executer 가 바인딩마다 후보를
    # 펼치고 `binder` 로 body·준비·효과 entry 를 얻는다.
    contracts: dict[str, "ActionContract"] = field(default_factory=dict)
    binder: "ContractBinder | None" = None
    # 행동 지침 모드의 ```guidance: planner 가 Jev 에게 주는 하위 목표·제약·지침. Jev 질문에 붙는다.
    guidance: str | None = None
    # planner 의 ```facts 블록: 이 step 에서 Jev 가 매 turn 값을 읽을 선언 fact 의 이름.
    facts: tuple[str, ...] = ()

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
class ActionPick:
    """매 turn 고르는 인자 하나. `names` 는 후보 객체의 이름이고, `bind` 는 고른 객체의 path 로 그 호출을 파싱한다.

    `ready` 는 고른 객체를 넣은 그 macro 앞부분 `require` 의 probe 를 준다. 거짓으로 읽힌 객체는 선택지에서 빠진다.
    그 인자를 읽는 `require` 가 없으면 `None` 이다.
    """

    names: tuple[str, ...]
    bind: Callable[[str], Awaitable[MacroDefinition]]
    ready: Callable[[str], Awaitable[MacroDefinition | None]] | None = None


@dataclass(frozen=True)
class DeveloperAction:
    """step 에 고른 developer macro 하나: 인자를 묶어 파싱한 정의와 Jev 가 읽을 설명."""

    definition: MacroDefinition
    description: str
    # 인자 하나를 `pick(...)` 으로 남겨 둔 호출이면 그 후보 범위. `definition` 은 첫 이름으로 묶어 둔 자리 표시다.
    pick: ActionPick | None = None
    # 인자를 읽지 않는 앞부분 `require` 의 probe. planner 가 `ready_<action>` 을 쓰지 않았으면 그 행동의 준비 조건이 된다.
    ready: MacroDefinition | None = None


async def parse_step(
    source: str,
    registered: RegisteredMacros,
    developer_actions: Mapping[str, DeveloperAction] | None = None,
    contracts_text: str | None = None,
) -> StepMacros:
    """The step's macros, or `MacroRejection` with what to write instead.

    The parser checks each action and `done`; this adds what the executer needs of the source as
    a whole — a `done` predicate, no parameters, a docstring on each action. A step may have no action at
    all: one that only looks at the screen is `done` alone.
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
    if contracts_text is not None:
        return await _parse_contract_step(source, defs, registered, developer_actions, contracts_text)
    # action 이 없어도 받는다(ARTEL-1453). 화면을 보기만 하는 step 은 `done` 하나로 끝나고, executer 는 그것이 참이 되기를
    # 기다린다. 전에는 거절해서 planner 가 같은 step 을 한 번 더 썼다 — 3주간 거절 26건 중 5건이 이것이었다.
    actions = _action_defs(defs)
    reject_unstable_selectors(source)
    parsed = await _parse_actions(actions, source, registered, developer_actions)
    probe = await registered.parse(PROBE, source.rstrip("\n") + _PROBE_SOURCE)
    descriptions = {key: action.description for key, action in developer_actions.items()}
    effects = await _parse_predicates(defs, list(parsed), source, registered, EFFECT_PREFIX, effect_name, EFFECT_FLAG)
    readies = await _parse_predicates(defs, list(parsed), source, registered, READY_PREFIX, ready_name, READY_FLAG)
    readies = _with_developer_readies(readies, developer_actions)
    picks = {key: action.pick for key, action in developer_actions.items() if action.pick is not None}
    return StepMacros(source, parsed, probe, descriptions, effects=effects, readies=readies, picks=picks)


def _with_developer_readies(
    readies: dict[str, MacroDefinition], developer_actions: Mapping[str, DeveloperAction]
) -> dict[str, MacroDefinition]:
    """planner 의 `ready_<action>` 에, 그것이 없는 developer macro 의 앞부분 `require` probe 를 더한다."""
    derived = {key: action.ready for key, action in developer_actions.items() if action.ready is not None}
    return {**derived, **readies}


async def _parse_contract_step(
    source: str,
    defs: list[ast.FunctionDef],
    registered: RegisteredMacros,
    developer_actions: Mapping[str, DeveloperAction],
    contracts_text: str,
) -> StepMacros:
    """계약 모드의 step. planner 의 행동은 모두 ```actions 계약이고, 고른 developer macro 는 legacy action 으로 남는다."""
    from app.agents.planner_executer.contract import (
        EFFECT_PREFIX as CONTRACT_EFFECT,
        READY_PREFIX as CONTRACT_READY,
        ContractBinder,
        read_contracts,
    )

    contracts = read_contracts(contracts_text, source)
    reject_unstable_selectors(source)
    binder = ContractBinder(source, contracts, registered.parse)
    # 자리 표시 바인딩(인자마다 첫 이름)으로 body·조건을 한 번 파싱해, 문법 거절을 지금 planner 에게 돌려준다.
    for name, contract in contracts.items():
        placeholder = tuple((param, contract.domains[param][0]) for param in contract.params)
        await binder.body(name, placeholder)
        await binder.ready(name, placeholder)
        await binder.effect(name, placeholder)
    parsed = await _parse_actions([], source, registered, developer_actions)
    probe = await registered.parse(PROBE, source.rstrip("\n") + _PROBE_SOURCE)
    descriptions = {key: action.description for key, action in developer_actions.items()}
    owned = {prefix + name for name in contracts for prefix in (CONTRACT_READY, CONTRACT_EFFECT)}
    legacy_defs = [node for node in defs if node.name not in owned]
    effects = await _parse_predicates(legacy_defs, list(parsed), source, registered, EFFECT_PREFIX, effect_name, EFFECT_FLAG)
    readies = await _parse_predicates(legacy_defs, list(parsed), source, registered, READY_PREFIX, ready_name, READY_FLAG)
    readies = _with_developer_readies(readies, developer_actions)
    picks = {key: action.pick for key, action in developer_actions.items() if action.pick is not None}
    for name in contracts:
        node = next(item for item in defs if item.name == name)
        descriptions[name] = ast.get_docstring(node) or ""
    return StepMacros(
        source, parsed, probe, descriptions, effects=effects, readies=readies, picks=picks,
        contracts=contracts, binder=binder,
    )


async def parse_guidance_step(
    source: str,
    registered: RegisteredMacros,
    developer_actions: Mapping[str, DeveloperAction],
    contracts: Mapping[str, "ActionContract"],
    binder: object,
    guidance: str | None,
) -> StepMacros:
    """행동 지침 모드의 step, 또는 `MacroRejection`.

    planner 의 글에는 `done` 과 그것이 읽는 `-> bool` helper 만 있다. 입력을 보내는 `def` 와 행동의 준비·효과 조건은 등록 행동의
    몫이라 받지 않는다. 행동은 ```developer 블록에서 고른 등록 행동뿐이다.
    """
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
    written = [node.name for node in defs if _returns(node, "None")]
    if written:
        raise MacroRejection(
            f"{', '.join(f'`{name}`' for name in written)} sends input to the game. In this step you do not write actions: "
            "choose registered actions in the ```developer block and say how to use them in the ```guidance block."
        )
    owned = [node.name for node in defs if node.name.startswith((READY_PREFIX, EFFECT_PREFIX))]
    if owned:
        raise MacroRejection(
            f"{', '.join(f'`{name}`' for name in owned)}: when an action can run and what it changes are part of the "
            "registered action, not of the plan. Leave them out."
        )
    # 등록 행동을 하나도 고르지 않은 step 도 받는다. 화면을 보기만 하는 step 이다(ARTEL-1453).
    if source.strip():
        reject_unstable_selectors(source)
    parsed = await _parse_actions([], source, registered, developer_actions)
    probe = await registered.parse(PROBE, source.rstrip("\n") + _PROBE_SOURCE)
    descriptions = {key: action.description for key, action in developer_actions.items()}
    descriptions.update({key: contract.description for key, contract in contracts.items()})
    readies = _with_developer_readies({}, developer_actions)
    return StepMacros(
        source, parsed, probe, descriptions, readies=readies, contracts=dict(contracts), binder=binder,
        guidance=guidance.strip() if guidance and guidance.strip() else None,
    )


async def _parse_predicates(
    defs: list[ast.FunctionDef],
    actions: list[str],
    source: str,
    registered: RegisteredMacros,
    prefix: str,
    name_of,
    flag_name: str,
) -> dict[str, MacroDefinition]:
    """행동마다 `<prefix><action>() -> bool`(효과 조건 `effect_`, 준비 조건 `ready_`)이 있으면 그것을 읽는 probe. `done` 과 같은
    방식이다: 그 predicate 가 참이면 flag 를 남기는 entry 를 덧붙여 parse 한다. 맞는 행동이 없는 이름은 거절한다 — 이름이 틀린
    조건은 조용히 안 읽히기 때문이다."""
    wanted = {node.name: node for node in defs if node.name.startswith(prefix)}
    by_name = {name_of(action): action for action in actions}
    for name, node in wanted.items():
        if name not in by_name:
            known = ", ".join(sorted(by_name)) or "(none)"
            raise MacroRejection(f"`{name}` matches no action; a `{prefix}` predicate is named after its action: {known}.")
        if not _returns(node, "bool") or node.args.args:
            raise MacroRejection(f"`{name}` must be `def {name}() -> bool:` with no parameters.")
    probes: dict[str, MacroDefinition] = {}
    for name in wanted:
        entry = f"executer_probe_{name}"
        probe_source = f'\n\ndef {entry}() -> None:\n    if {name}():\n        flag("{flag_name}")\n'
        probes[by_name[name]] = await registered.parse(entry, source.rstrip("\n") + probe_source)
    return probes


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
