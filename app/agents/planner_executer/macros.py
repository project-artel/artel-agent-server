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
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace

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
# 대상을 받는 행동의 선호 조건: `prefer_<action>(<대상>) -> bool`(pe14). 묶은 행동을 고른 뒤 코드가 대상마다 읽어, 참인 대상
# 중에서 고른다. 계산이 필요한 선택(배분·상성)은 Jev 가 하지 못한다 — 계산식은 planner 가 쓰고 계산은 코드가 한다.
PREFER_PREFIX = "prefer_"
PREFER_FLAG = "prefer"
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
    # planner 의 ```goals 블록(prompt v6): 순서 있는 하위 목표 (이름, 문장). Jev 는 step 문장 대신 지금 하위 목표를 읽는다.
    goals: tuple[tuple[str, str], ...] = ()
    # 하위 목표마다 `goal_<이름>() -> bool` 을 읽는 probe. 참이면 다음 하위 목표로 넘어간다.
    goal_probes: dict[str, "MacroDefinition"] = field(default_factory=dict)
    # planner 의 ```segment 블록: 같은 씬에서 이어지는 step 들을 이 계획이 함께 맡는다. step 번호 → (그 step 의 하위 목표
    # 이름들, 그 step 의 완료 probe, 그 step 에서 고를 수 있는 action). 지금 step 의 probe 는 `None` 이고 `done()` 을 쓴다.
    segment: dict[int, tuple[tuple[str, ...], "MacroDefinition | None", tuple[str, ...]]] = field(default_factory=dict)

    # 대상을 받는 행동마다 planner 가 쓴 `prefer_<action>(<대상>)`(pe14).
    prefers: dict[str, "PreferRule"] = field(default_factory=dict)

    def description(self, action: str) -> str:
        if action in self.descriptions:
            return self.descriptions[action]
        return self.actions[action].entry.docstring or ""


def prefer_name(action: str) -> str:
    """행동 이름에 맞는 선호 조건의 이름. `developer:place_card` → `prefer_place_card`."""
    return PREFER_PREFIX + action.removeprefix("developer:").replace("#", "_")


@dataclass
class PreferRule:
    """planner 가 쓴 `prefer_<action>(<param>, ...)`. 대상의 주소를 넣어 읽는 probe 를 만든다(pe14)."""

    name: str
    params: tuple[str, ...]
    source: str
    registered: "RegisteredMacros"
    _probes: dict[tuple[str, ...], MacroDefinition] = field(default_factory=dict)

    async def probe(self, binding: tuple[tuple[str | None, str], ...]) -> MacroDefinition | None:
        """`binding` 은 `(param, selector)` 들이다. param 이름이 맞는 것을 넣고, 이름이 없으면 순서대로 넣는다. 못 맞추면 `None`."""
        named = {param: selector for param, selector in binding if param}
        unnamed = [selector for param, selector in binding if not param]
        args: list[str] = []
        for param in self.params:
            if param in named:
                args.append(named[param])
            elif unnamed:
                args.append(unnamed.pop(0))
            else:
                return None
        key = tuple(args)
        if key not in self._probes:
            entry = f"executer_probe_{self.name}"
            call = ", ".join(f"selector({_quoted(arg)})" for arg in args)
            wrapper = f'\n\ndef {entry}() -> None:\n    if {self.name}({call}):\n        flag("{PREFER_FLAG}")\n'
            self._probes[key] = await self.registered.parse(entry, self.source.rstrip("\n") + wrapper)
        return self._probes[key]


def _quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _prefer_rules(
    defs: list[ast.FunctionDef], actions: list[str], source: str, registered: "RegisteredMacros"
) -> dict[str, PreferRule]:
    """`prefer_<action>(<대상>) -> bool` 마다 그 행동의 선호 조건. 맞는 행동이 없거나 모양이 틀리면 거절한다."""
    by_name = {prefer_name(action): action for action in actions}
    rules: dict[str, PreferRule] = {}
    for node in defs:
        if not node.name.startswith(PREFER_PREFIX):
            continue
        if node.name not in by_name:
            known = ", ".join(sorted(by_name)) or "(none)"
            raise MacroRejection(f"`{node.name}` matches no action; a `prefer_` predicate is named after its action: {known}.")
        if not _returns(node, "bool") or not node.args.args:
            raise MacroRejection(
                f"`{node.name}` must be `def {node.name}(<target>: object) -> bool:` with the action's target as its parameter."
            )
        rules[by_name[node.name]] = PreferRule(node.name, tuple(arg.arg for arg in node.args.args), source, registered)
    return rules


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
                definition = await self.parse(entry, source, importing, game_functions=False)
            except MacroRejection as rejection:
                problem = f"It is registered but no longer parses: {rejection.render()}"
            else:
                self._found[name] = definition
                return definition, ""
        self._found[name] = problem
        return None, problem

    async def parse(
        self, name: str, source: str, importing: tuple[str, ...] = (), *, game_functions: bool = True,
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
        return macro_definition_from_source(name, source, imports=imports, game_functions=game_functions)


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


# planner·editor 가 쓴 글에서 받지 않는 호출(pe11). 둘 다 그 자리의 판단을 decision model 에게 넘긴다. 대상 고르기는 executer 가
# 대상마다 선택지를 펼쳐 Jev 에게 묻고, 끝·준비·효과는 상태에서 읽는 predicate 로 쓴다. try 24 step 3 의
# `while ask("뒤에 대사가 더 있나?")` 는 상태에 없는 것을 Jev 에게 물었다.
DECISION_CALLS = ("ask", "decide")


def reject_decision_calls(source: str) -> None:
    """글의 어느 `def` 든 `ask`·`decide` 를 부르면 `MacroRejection`. 가져온 developer macro 의 본문은 이 글이 아니라 보지 않는다."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in DECISION_CALLS:
            raise MacroRejection(
                f"`{node.func.id}(...)` hands a judgement to the decision model, and plans do not use it. The executer "
                "already offers one option per target object and asks the decision model to pick among the ready options. "
                "Read when to act, when an action worked and when the step is done from the game's state in a predicate; "
                "what the state cannot tell, leave to another end condition.",
                node.lineno,
            )


async def parse_step(
    source: str,
    registered: RegisteredMacros,
    developer_actions: Mapping[str, DeveloperAction] | None = None,
    contracts_text: str | None = None,
    contract_keys: tuple[str, ...] = (),
) -> StepMacros:
    """The step's macros, or `MacroRejection` with what to write instead.

    `contract_keys` 는 legacy 모드에서 등록 계약으로 도는 developer macro 의 키다. planner 가 그 이름으로 쓴 `effect_` 는
    쓰지 않는다 — 등록된 계약이 이긴다. `ready_` 는 남긴다: 계약에 준비 조건이 없으면 executer 가 그것을 쓴다(pe13).

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
    reject_decision_calls(source)
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
    named = [*parsed, *contract_keys]
    effects = await _parse_predicates(defs, named, source, registered, EFFECT_PREFIX, effect_name, EFFECT_FLAG)
    readies = await _parse_predicates(defs, named, source, registered, READY_PREFIX, ready_name, READY_FLAG)
    effects = {key: probe for key, probe in effects.items() if key not in contract_keys}
    readies = _with_developer_readies(readies, developer_actions)
    picks = {key: action.pick for key, action in developer_actions.items() if action.pick is not None}
    prefers = _prefer_rules(defs, named, source, registered)
    return StepMacros(source, parsed, probe, descriptions, effects=effects, readies=readies, picks=picks, prefers=prefers)


def _with_developer_readies(
    readies: dict[str, MacroDefinition], developer_actions: Mapping[str, DeveloperAction]
) -> dict[str, MacroDefinition]:
    """planner 의 `ready_<action>` 에, 그것이 없는 developer macro 의 앞부분 `require` probe 를 더한다."""
    derived = {key: action.ready for key, action in developer_actions.items() if action.ready is not None}
    # developer macro 가 제 준비 조건을 가지면 그것이 이긴다(pe9). planner 의 `ready_` 는 step 이 기대하는 화면을 적는 일이
    # 잦았다 — 로컬 try 8·9 step 11~18 은 그 조건이 다른 화면에서 거짓이라 "준비된 동작 없음" 으로 줄줄이 멈췄다.
    return {**readies, **derived}


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
    reject_decision_calls(source)
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


GOAL_DEF_PREFIX = "goal_"
GOAL_FLAG = "goal"
_GOAL_LINE = re.compile(r"^\s*([a-z][a-z0-9_]{0,39})\s*:\s*(\S.*)$")


async def read_goals(
    text: str | None, source: str, registered: RegisteredMacros
) -> tuple[tuple[tuple[str, str], ...], dict[str, MacroDefinition]]:
    """```goals 블록의 하위 목표와 그 완료 조건 probe. 블록이 없으면 둘 다 비어 있다.

    줄마다 `<이름>: <목표>` 이고, 이름마다 source 에 `def goal_<이름>() -> bool:` 이 있어야 한다. 이름이 틀린 조건은 조용히 안
    읽히므로 거절한다.
    """
    if text is None or not text.strip():
        return (), {}
    goals: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        matched = _GOAL_LINE.match(line)
        if matched is None:
            raise MacroRejection(
                "each line of the ```goals block is `<name>: <goal>` with a lowercase name of letters, digits and `_`."
            )
        name, goal = matched.group(1), matched.group(2).strip()
        if any(name == seen for seen, _ in goals):
            raise MacroRejection(f"the goal `{name}` is listed twice in the ```goals block.")
        goals.append((name, goal))
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise MacroRejection(f"this is not valid Python syntax — {error.msg}.", error.lineno) from None
    defs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    probes: dict[str, MacroDefinition] = {}
    for name, _goal in goals:
        node = defs.get(GOAL_DEF_PREFIX + name)
        if node is None:
            raise MacroRejection(
                f"the goal `{name}` needs `def {GOAL_DEF_PREFIX}{name}() -> bool:` in the ```python block, reading when it is reached."
            )
        if not _returns(node, "bool") or node.args.args:
            raise MacroRejection(f"`{GOAL_DEF_PREFIX}{name}` must be `def {GOAL_DEF_PREFIX}{name}() -> bool:` with no parameters.")
        entry = f"executer_probe_{GOAL_DEF_PREFIX}{name}"
        probe_source = f'\n\ndef {entry}() -> None:\n    if {GOAL_DEF_PREFIX}{name}():\n        flag("{GOAL_FLAG}")\n'
        probes[name] = await registered.parse(entry, source.rstrip("\n") + probe_source)
    return tuple(goals), probes


_SEGMENT_LINE = re.compile(r"^\s*(\d+)\s*:\s*([^|]*)\|(.*)$")
SEGMENT_DONE_PREFIX = "done_"


async def read_segment(
    text: str | None,
    source: str,
    registered: RegisteredMacros,
    step: int,
    goal_names: tuple[str, ...],
    action_names: tuple[str, ...] = (),
) -> dict[int, tuple[tuple[str, ...], MacroDefinition | None, tuple[str, ...]]]:
    """```segment 블록: 이 계획이 맡는 연속 step 과 step 마다의 하위 목표·완료 조건·action. 블록이 없으면 비어 있다.

    줄마다 `<step 번호>: <하위 목표 이름>, ... | <action 이름>, ...` 이다. 첫 줄은 지금 step 이고 그 뒤는 바로 다음 step
    부터 빠짐없이 이어진다. 뒤 step 마다 `def done_<번호>() -> bool:` 이 있어야 한다. 하위 목표는 ```goals 에, action 은
    이 계획에 있는 것이어야 한다. action 을 step 마다 나누는 것은, 묶음의 action 이 모든 step 에 열리면 보기만 하는 step 이
    다음 step 의 버튼을 누르기 때문이다(로컬 try 13 step 1 이 '시작하기' 를 눌렀다).
    """
    if text is None or not text.strip():
        return {}
    rows: list[tuple[int, tuple[str, ...]]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        matched = _SEGMENT_LINE.match(line)
        if matched is None:
            raise MacroRejection(
                "each line of the ```segment block is `<step number>: <goal name>, ... | <action>, ...`; a step that only "
                "looks lists no action after the `|`."
            )
        names = tuple(name.strip() for name in matched.group(2).split(",") if name.strip())
        actions: list[str] = []
        for raw in (name.strip() for name in matched.group(3).split(",") if name.strip()):
            known = raw if raw in action_names else f"developer:{raw}" if f"developer:{raw}" in action_names else None
            if known is None:
                listed = ", ".join(action_names) or "(none)"
                raise MacroRejection(f"the ```segment block names `{raw}`, which is not an action of this plan: {listed}.")
            actions.append(known)
        unknown = [name for name in names if name not in goal_names]
        if unknown:
            raise MacroRejection(
                f"the ```segment block names goals that are not in the ```goals block: {', '.join(unknown)}."
            )
        rows.append((int(matched.group(1)), names, tuple(actions)))
    numbers = [number for number, _names, _actions in rows]
    if numbers != list(range(step, step + len(rows))):
        raise MacroRejection(
            f"the ```segment block lists consecutive steps starting with the step you are writing ({step}); it lists {numbers}."
        )
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise MacroRejection(f"this is not valid Python syntax — {error.msg}.", error.lineno) from None
    defs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    segment: dict[int, tuple[tuple[str, ...], MacroDefinition | None, tuple[str, ...]]] = {}
    for number, names, actions in rows:
        if number == step:
            segment[number] = (names, None, actions)
            continue
        name = f"{SEGMENT_DONE_PREFIX}{number}"
        node = defs.get(name)
        if node is None or not _returns(node, "bool") or node.args.args:
            raise MacroRejection(
                f"step {number} is in the ```segment block, so the source needs `def {name}() -> bool:` reading that step's result."
            )
        entry = f"executer_probe_{name}"
        probe_source = f'\n\ndef {entry}() -> None:\n    if {name}():\n        flag("{PROBE_FLAG}")\n'
        segment[number] = (names, await registered.parse(entry, source.rstrip("\n") + probe_source), actions)
    return segment


def step_of_segment(macros: StepMacros, step: int) -> StepMacros:
    """묶음 계획에서 `step` 하나를 맡는 모양. 완료 probe 와 하위 목표를 그 step 의 것으로 바꾼다. 묶음이 없으면 그대로다."""
    if step not in macros.segment:
        return macros
    names, probe, actions = macros.segment[step]
    goals = tuple(goal for goal in macros.goals if goal[0] in names) if names else macros.goals
    return replace(
        macros,
        probe=probe or macros.probe,
        goals=goals,
        goal_probes={name: macros.goal_probes[name] for name, _goal in goals if name in macros.goal_probes},
        actions={name: action for name, action in macros.actions.items() if name in actions},
        contracts={name: contract for name, contract in macros.contracts.items() if name in actions},
        after={first: nexts for first, nexts in macros.after.items() if first in actions},
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


def action_code(source: str, names: "Iterable[str]") -> dict[str, str]:
    """planner 가 쓴 행동마다 그 `def` 와, 그 안에서 부르는 같은 계획의 `def` 의 코드(pe13). evaluator 가 행동이 무엇을
    확인했는지 읽는다 — 이름과 "passed" 만으로는 그 행동 안의 `wait_until` 이 무엇을 기다렸는지 모른다(로컬 try 31 step 3).
    읽지 못하는 글이면 빈 dict 다."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    defs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    code: dict[str, str] = {}
    for name in names:
        if name not in defs:
            continue
        seen: list[str] = []
        pending = [name]
        while pending:
            current = pending.pop(0)
            if current in seen or current not in defs:
                continue
            seen.append(current)
            for call in ast.walk(defs[current]):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in defs:
                    pending.append(call.func.id)
        code[name] = "\n\n".join(ast.get_source_segment(source, defs[each]) or "" for each in seen)
    return code

