"""행동 계약(`artel/docs/architecture/2026-11-01-pe-action-contract-design.md` 의 1차 구현).

planner 가 쓴 행동은 임의 macro 함수가 아니라 계약이다. ```actions 블록이 행동마다 대상 인자의 domain(객체 이름)과 효과 계약을
말하고, ```python 의 `def` 가 그 실행 body 와 준비·효과 조건이다.

- 실행 body `def <action>(<인자>: object, ...) -> None`: 바인딩된 인자와 body 에 글자로 적은 고정 대상에만 입력한다. 대상을
  고르는 `find`·`find_all`, 대상을 도는 반복, 게임 결과를 기다리는 `wait_until`·`while`, 선택 모델을 다시 부르는 `ask`·
  `decide` 는 쓰지 않는다. 고르기는 executer 가 후보를 열거하고 Jev 가 하며, 확인은 executer 의 효과 판정이 한다.
- 준비 조건 `ready_<action>(<같은 인자>) -> bool`, 효과 조건 `effect_<action>(<같은 인자>) -> bool`: 읽기만 하는 macro
  predicate 다. macro 엔진이 이미 검사하는 제한된 언어라 따로 식 언어를 두지 않는다. 읽지 못하면(멈춤) unknown 이다.
- 효과 계약: `transition`(행동 전에 거짓이고 뒤에 참), `state`(뒤에 참), `unobservable`(이유를 적는다). `within` 초 안에
  판정한다.

developer macro 는 MCP 로 등록할 때 계약을 함께 낼 수 있다(`registered_contract`). 그때 인자의 대상은 객체 이름이 아니라
그 객체가 가진 멤버(`holds`)로 말하고, executer 가 그 멤버를 가진 지금 켜진 객체를 후보로 펼친다. 계약 없이 올라온 macro 는
legacy 로 돈다.
"""

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field

from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.grammar import TOOL_NAMES
from app.agents.qa.macro.model import MacroDefinition

_ACTIONS_BLOCK = re.compile(r"```actions\s*\n(.*?)```", re.DOTALL)
_INDEX = re.compile(r"\[\d+\]")
READY_PREFIX = "ready_"
EFFECT_PREFIX = "effect_"
EFFECT_MODES = ("transition", "state", "unobservable")
# 효과를 기다리는 한도(초). 계약이 적지 않으면 기본, 적어도 상한까지만.
DEFAULT_WITHIN_SECONDS = 3.0
MAX_WITHIN_SECONDS = 15.0
# body 에서 쓰지 않는 호출. 고르기·결과 대기·선택 모델 재호출이다.
_FORBIDDEN_CALLS = {
    "find": "pick objects with `find`; the object to act on is a parameter the executer binds",
    "find_all": "walk objects with `find_all`; the object to act on is a parameter the executer binds",
    "wait_until": "wait for a game result; the effect contract is how the executer checks the result",
    "ask": "ask the decision model; the decision model picks the action and its objects before the body runs",
    "decide": "ask the decision model; the decision model picks the action and its objects before the body runs",
}
# 입력 도구 중 대상을 받는 자리.
_TARGET_ARGS = {"click": (0,), "double_click": (0,), "move_pointer": (0,), "enter_text": (0,), "drag": (0, 1)}
_SHAPE = (
    '{"<action>": {"params": {"<parameter>": ["<object name>", ...]}, '
    '"effect": {"mode": "transition" | "state" | "unobservable", "within": <seconds>, "reason": "<why, for unobservable>"}}}'
)


@dataclass(frozen=True)
class EffectContract:
    mode: str
    within: float = DEFAULT_WITHIN_SECONDS
    reason: str = ""


@dataclass(frozen=True)
class ActionContract:
    """행동 하나의 계약. `params` 는 body 의 인자 순서, `domains` 는 인자마다 후보 객체의 이름이다."""

    name: str
    params: tuple[str, ...]
    domains: dict[str, tuple[str, ...]]
    effect: EffectContract
    has_ready: bool
    # body(와 그것이 부르는 helper)에 글자로 적은 고정 대상 selector, `[n]` 을 뺀 꼴.
    fixed_targets: tuple[str, ...]
    revision: str
    # 등록 계약의 인자 대상: 인자마다 그 객체가 가진 멤버(`Type::member`). 이것이 있는 인자는 `domains` 를 쓰지 않는다.
    holds: dict[str, str] = field(default_factory=dict)
    # Jev 가 읽는 설명. 등록 행동이면 그 macro 의 docstring 이다(planner 행동은 `StepMacros.descriptions` 에 따로 있다).
    description: str = ""


def split_actions_block(answer: str) -> tuple[str, str | None]:
    match = _ACTIONS_BLOCK.search(answer)
    if match is None:
        return answer, None
    return answer[: match.start()] + answer[match.end() :], match.group(1)


def read_contracts(text: str | None, source: str) -> dict[str, ActionContract]:
    """```actions 블록과 macro 글로 계약을 읽는다. 못 하면 `MacroRejection`. 글의 행동 `def` 는 모두 계약이 있어야 한다."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise MacroRejection(f"this is not valid Python syntax — {error.msg}.", error.lineno) from None
    defs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    if not any(_returns(node, "None") for node in defs.values()) and (text is None or text.strip() in ("", "{}")):
        # developer macro 만 고른 step 이다. 계약할 planner 행동이 없다.
        return {}
    if text is None or not text.strip():
        raise MacroRejection(f"the answer needs one ```actions block with each action's contract: {_SHAPE}.")
    try:
        block = json.loads(text)
    except json.JSONDecodeError as error:
        raise MacroRejection(f"the ```actions block is not JSON — {error.msg}. Write {_SHAPE}.") from None
    if not isinstance(block, dict) or not block:
        raise MacroRejection(f"the ```actions block must be an object with one entry per action: {_SHAPE}.")
    contracts: dict[str, ActionContract] = {}
    for name, entry in block.items():
        contracts[name] = _contract(name, entry, defs, source)
    helpers_called = {called for contract in contracts.values() for called in _called(defs, contract.name)}
    missing = [
        name for name, node in defs.items()
        if _returns(node, "None") and name not in contracts and name not in helpers_called
    ]
    if missing:
        raise MacroRejection(
            f"{', '.join(f'`{name}`' for name in missing)} has no entry in the ```actions block. Every action needs its "
            f"contract; a `-> None` def that is only a helper must be called by an action."
        )
    return contracts


def _contract(name: str, entry: object, defs: dict[str, ast.FunctionDef], source: str) -> ActionContract:
    node = defs.get(name)
    if node is None or not _returns(node, "None"):
        raise MacroRejection(f"`{name}` in the ```actions block has no `def {name}(...) -> None:` in the python block.")
    if not isinstance(entry, dict):
        raise MacroRejection(f'"{name}" in the ```actions block must be an object: {_SHAPE}.')
    params = tuple(argument.arg for argument in node.args.args)
    for argument in node.args.args:
        annotation = argument.annotation
        if not (isinstance(annotation, ast.Name) and annotation.id == "object"):
            raise MacroRejection(f"`{name}`'s parameter `{argument.arg}` must be `object`: the executer binds an object to it.")
    raw = entry.get("params") or {}
    if not isinstance(raw, dict) or set(raw) != set(params):
        raise MacroRejection(
            f'"params" of `{name}` must name exactly its parameters ({", ".join(params) or "none"}), each with the '
            "names of the objects it may be."
        )
    domains: dict[str, tuple[str, ...]] = {}
    for param, names in raw.items():
        if not isinstance(names, list) or not names or not all(isinstance(item, str) and item.strip() for item in names):
            raise MacroRejection(f'"params"."{param}" of `{name}` is a list of object names, as the screen spells them.')
        domains[param] = tuple(names)
    effect = _effect(name, entry.get("effect"), defs, params)
    ready = defs.get(READY_PREFIX + name)
    if ready is not None:
        _predicate_shape(ready, params)
    _check_body(name, defs)
    text = "\n".join(ast.get_source_segment(source, defs[item]) or "" for item in _revision_defs(defs, name))
    return ActionContract(
        name, params, domains, effect, ready is not None, tuple(sorted(_literals(defs, name))),
        hashlib.sha256(text.encode()).hexdigest()[:12],
    )


_REGISTERED_SHAPE = (
    '{"params": {"<parameter>": {"holds": "<Declaring.Type>::<member>"}}, '
    '"effect": {"mode": "transition" | "state" | "unobservable", "within": <seconds>, "reason": "<why, for unobservable>"}}'
)
_HOLDS = re.compile(r"^[A-Za-z_][\w.]*::[A-Za-z_][\w.]*$")


def registered_contract(name: str, raw: object, source: str) -> ActionContract:
    """MCP 로 등록하는 developer macro `name` 의 계약. 못 하면 `MacroRejection`.

    planner 행동의 계약(`read_contracts`)과 같은 검사를 한다: body 는 바인딩된 인자와 글자로 적은 selector 에만 입력하고, 고르기·
    대상 순회·결과 대기를 하지 않는다. 준비·효과 조건은 같은 글의 `ready_<name>`·`effect_<name>` 이다. 다른 점은 인자의 대상이다 —
    planner 는 화면의 이름을 적었지만 등록 계약은 그 객체가 가진 멤버를 적는다. 같은 이름의 객체가 계획 뒤에 새로 나타나도 후보가
    된다. 다른 developer macro 를 가져오는 글(`from macros import`)은 받지 않는다 — 가져온 본문까지 검사할 수 없다.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise MacroRejection(f"this is not valid Python syntax — {error.msg}.", error.lineno) from None
    if any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in tree.body):
        raise MacroRejection(
            "a macro registered with a contract is self-contained: write its helpers in the same source instead of "
            "importing them, so the contract can check every input it sends."
        )
    defs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    node = defs.get(name)
    if node is None or not _returns(node, "None"):
        raise MacroRejection(f"the contract is for `{name}`, but the source has no `def {name}(...) -> None:`.")
    if not isinstance(raw, dict):
        raise MacroRejection(f"the contract must be an object: {_REGISTERED_SHAPE}.")
    params = tuple(argument.arg for argument in node.args.args)
    for argument in node.args.args:
        annotation = argument.annotation
        if not (isinstance(annotation, ast.Name) and annotation.id == "object"):
            raise MacroRejection(
                f"`{name}`'s parameter `{argument.arg}` must be `object`: the executer binds one object to it."
            )
    given = raw.get("params") or {}
    if not isinstance(given, dict) or set(given) != set(params):
        raise MacroRejection(
            f'"params" of the contract must name exactly the parameters of `{name}` ({", ".join(params) or "none"}), '
            'each with {"holds": "<Declaring.Type>::<member>"}: a member every object it may act on holds.'
        )
    holds: dict[str, str] = {}
    for param, target in given.items():
        member = target.get("holds") if isinstance(target, dict) else None
        if not isinstance(member, str) or not _HOLDS.match(member):
            raise MacroRejection(
                f'"params"."{param}" needs "holds": the declaring type and member every object it may act on holds, '
                'as `Declaring.Type::member`.'
            )
        holds[param] = member
    effect = _effect(name, raw.get("effect"), defs, params)
    ready = defs.get(READY_PREFIX + name)
    if ready is not None:
        _predicate_shape(ready, params)
    _check_body(name, defs)
    text = "\n".join(ast.get_source_segment(source, defs[item]) or "" for item in _revision_defs(defs, name))
    text += json.dumps({"holds": holds, "effect": [effect.mode, effect.within, effect.reason]}, sort_keys=True)
    return ActionContract(
        name, params, {}, effect, ready is not None, tuple(sorted(_literals(defs, name))),
        hashlib.sha256(text.encode()).hexdigest()[:12], holds,
    )


def contract_json(contract: ActionContract) -> dict:
    """저장할 계약. `registered_contract` 가 다시 읽을 수 있는 모양에 `revision` 을 더한 것이다."""
    effect: dict = {"mode": contract.effect.mode, "within": contract.effect.within}
    if contract.effect.reason:
        effect["reason"] = contract.effect.reason
    return {
        "params": {param: {"holds": member} for param, member in contract.holds.items()},
        "effect": effect,
        "revision": contract.revision,
    }


def _effect(name: str, raw: object, defs: dict[str, ast.FunctionDef], params: tuple[str, ...]) -> EffectContract:
    if not isinstance(raw, dict) or raw.get("mode") not in EFFECT_MODES:
        raise MacroRejection(f'"effect" of `{name}` needs "mode": one of {", ".join(EFFECT_MODES)}.')
    mode = raw["mode"]
    within = raw.get("within", DEFAULT_WITHIN_SECONDS)
    if not isinstance(within, (int, float)) or within <= 0:
        raise MacroRejection(f'"effect"."within" of `{name}` is a number of seconds.')
    predicate = defs.get(EFFECT_PREFIX + name)
    if mode == "unobservable":
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise MacroRejection(f'an unobservable effect of `{name}` needs "reason": why its result cannot be read.')
        return EffectContract(mode, float(within), reason.strip())
    if predicate is None:
        raise MacroRejection(
            f"a {mode} effect of `{name}` needs `def {EFFECT_PREFIX}{name}({', '.join(f'{p}: object' for p in params)}) -> bool:`."
        )
    _predicate_shape(predicate, params)
    return EffectContract(mode, min(float(within), MAX_WITHIN_SECONDS))


def _predicate_shape(node: ast.FunctionDef, params: tuple[str, ...]) -> None:
    if not _returns(node, "bool") or tuple(argument.arg for argument in node.args.args) != params:
        raise MacroRejection(
            f"`{node.name}` must be `def {node.name}({', '.join(f'{p}: object' for p in params)}) -> bool:` — the same "
            "parameters as its action."
        )


def _check_body(name: str, defs: dict[str, ast.FunctionDef]) -> None:
    """body 와 그것이 부르는 helper 를 따라가며 고르기·대상 순회·결과 대기를 거절한다. 입력 대상은 인자나 글자로 적은 selector 다."""
    for function in [name, *sorted(_called(defs, name))]:
        node = defs[function]
        allowed = {argument.arg for argument in node.args.args}
        for item in ast.walk(node):
            if isinstance(item, ast.While):
                raise MacroRejection(
                    f"`{function}` has a `while`. A body does not loop until a result; the executer checks the effect."
                )
            if isinstance(item, ast.For) and not (
                isinstance(item.iter, ast.Call) and isinstance(item.iter.func, ast.Name) and item.iter.func.id == "range"
            ):
                raise MacroRejection(
                    f"`{function}` loops over objects. A body acts on the objects bound to its parameters; the executer "
                    "offers one option per object."
                )
            if isinstance(item, ast.Match):
                raise MacroRejection(f"`{function}` has a `match`; the decision model chooses before the body runs.")
            if isinstance(item, ast.Assign) or isinstance(item, ast.AnnAssign):
                value = item.value
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                if _is_selector_literal(value):
                    allowed.update(target.id for target in targets if isinstance(target, ast.Name))
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Name):
                called = item.func.id
                if called in _FORBIDDEN_CALLS:
                    raise MacroRejection(f"`{function}` calls `{called}`: a body does not {_FORBIDDEN_CALLS[called]}.")
                for position in _TARGET_ARGS.get(called, ()):
                    if position >= len(item.args):
                        continue
                    target = item.args[position]
                    if _is_selector_literal(target):
                        continue
                    if not (isinstance(target, ast.Name) and target.id in allowed):
                        raise MacroRejection(
                            f"`{function}` sends `{called}` to something that is neither a parameter nor a "
                            '`selector("...")` written in the body.'
                        )


def _called(defs: dict[str, ast.FunctionDef], name: str) -> set[str]:
    """`name` 이 (helper 를 거쳐) 부르는 이 글의 다른 `-> None` def."""
    seen: set[str] = set()
    todo = [name]
    while todo:
        current = todo.pop()
        for item in ast.walk(defs[current]):
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Name):
                callee = item.func.id
                if callee in defs and callee != name and callee not in seen and _returns(defs[callee], "None"):
                    seen.add(callee)
                    todo.append(callee)
    return seen


def _revision_defs(defs: dict[str, ast.FunctionDef], name: str) -> list[str]:
    """계약 `revision` 에 드는 def: body·`ready_`·`effect_` 와, 셋이 (helper 를 거쳐) 부르는 이 글의 모든 def.

    전에는 body 가 부르는 `-> None` helper 만 따라가, `ready_`·`effect_` 만 부르는 bool helper 를 바꿔도 revision 이 같았다
    (종합 감사 F7). 준비 조건이 바뀐 macro 가 같은 계약으로 기록되면 런끼리 비교할 수 없다."""
    roots = [item for item in (name, READY_PREFIX + name, EFFECT_PREFIX + name) if item in defs]
    seen = list(roots)
    todo = list(roots)
    while todo:
        for item in ast.walk(defs[todo.pop()]):
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id in defs and item.func.id not in seen:
                seen.append(item.func.id)
                todo.append(item.func.id)
    return roots + sorted(set(seen) - set(roots))


def _literals(defs: dict[str, ast.FunctionDef], name: str) -> set[str]:
    found: set[str] = set()
    for function in [name, *_called(defs, name)]:
        for item in ast.walk(defs[function]):
            if _is_selector_literal(item):
                found.add(_INDEX.sub("", item.args[0].value).strip("/"))
    return found


def _is_selector_literal(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "selector"
        and len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
    )


def _returns(node: ast.FunctionDef, kind: str) -> bool:
    return isinstance(node.returns, ast.Constant if kind == "None" else ast.Name) and (
        (kind == "None" and node.returns.value is None) or (kind != "None" and getattr(node.returns, "id", None) == kind)
    )


def selector_source(selector: str) -> str:
    return "selector(" + json.dumps(selector, ensure_ascii=False) + ")"


@dataclass
class ContractBinder:
    """행동과 바인딩(인자 → 객체 selector)마다 body·준비·효과 entry 를 파싱한다. 같은 바인딩은 한 번만 파싱한다.

    준비·효과 조건은 실제로 읽는 인자만 묶는다. 행동이 대상을 없애거나 장면을 바꾸면, 읽지도 않는 인자를 묶는 줄에서 판정이
    멈춰 효과를 영영 확인하지 못한다.
    """

    source: str
    contracts: dict[str, ActionContract]
    parse: object  # `RegisteredMacros.parse`
    _cache: dict[tuple, MacroDefinition] = field(default_factory=dict)
    _reduced: dict[str, tuple[tuple[str, ...], str]] = field(default_factory=dict)

    async def _entry(self, kind: str, name: str, binding: tuple[tuple[str, str], ...], call: str, extra: str = "") -> MacroDefinition:
        key = (kind, name, binding)
        if key not in self._cache:
            entry = f"executer_{kind}_{name}"
            lines = [f"    {param}: object = {selector_source(selector)}" for param, selector in binding]
            body = "\n".join([*lines, f"    {call}"])
            text = self.source.rstrip("\n") + extra + f"\n\n\ndef {entry}() -> None:\n{body}\n"
            self._cache[key] = await self.parse(entry, text)
        return self._cache[key]

    def _predicate(self, predicate: str) -> tuple[tuple[str, ...], str]:
        """`(읽는 인자, 그 인자만 받는 사본의 글)`. 모든 인자를 읽으면 사본 없이 원래 이름을 쓴다."""
        if predicate not in self._reduced:
            tree = ast.parse(self.source)
            node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == predicate)
            read = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
            used = tuple(argument.arg for argument in node.args.args if argument.arg in read)
            if len(used) == len(node.args.args):
                self._reduced[predicate] = (used, "")
            else:
                node.name = f"{predicate}_bound"
                node.args.args = [argument for argument in node.args.args if argument.arg in used]
                self._reduced[predicate] = (used, "\n\n\n" + ast.unparse(node))
        return self._reduced[predicate]

    async def _condition(self, kind: str, prefix: str, name: str, binding: tuple[tuple[str, str], ...]) -> MacroDefinition:
        predicate = prefix + name
        used, extra = self._predicate(predicate)
        bound = tuple((param, selector) for param, selector in binding if param in used)
        callee = f"{predicate}_bound" if extra else predicate
        call = f'if {callee}({", ".join(param for param, _selector in bound)}):\n        flag("{kind}")'
        return await self._entry(kind, name, bound, call, extra)

    async def body(self, name: str, binding: tuple[tuple[str, str], ...]) -> MacroDefinition:
        args = ", ".join(param for param, _selector in binding)
        return await self._entry("run", name, binding, f"{name}({args})")

    async def ready(self, name: str, binding: tuple[tuple[str, str], ...]) -> MacroDefinition | None:
        if not self.contracts[name].has_ready:
            return None
        return await self._condition("ready", READY_PREFIX, name, binding)

    async def effect(self, name: str, binding: tuple[tuple[str, str], ...]) -> MacroDefinition | None:
        if self.contracts[name].effect.mode == "unobservable":
            return None
        return await self._condition("effect", EFFECT_PREFIX, name, binding)


@dataclass
class DispatchBinder:
    """행동마다 제 글을 가진 계약(등록한 developer macro)의 binder. 행동 키(`developer:<name>`)로 그 macro 의 binder 와 이름을 찾는다."""

    by_action: dict[str, tuple[ContractBinder, str]]

    async def body(self, action: str, binding: tuple[tuple[str, str], ...]) -> MacroDefinition:
        binder, name = self.by_action[action]
        return await binder.body(name, binding)

    async def ready(self, action: str, binding: tuple[tuple[str, str], ...]) -> MacroDefinition | None:
        binder, name = self.by_action[action]
        return await binder.ready(name, binding)

    async def effect(self, action: str, binding: tuple[tuple[str, str], ...]) -> MacroDefinition | None:
        binder, name = self.by_action[action]
        return await binder.effect(name, binding)
