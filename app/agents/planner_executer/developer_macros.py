"""프로젝트의 developer macro 를 planner–executer 런에 싣는다 (ARTEL-1101).

게임 개발자가 MCP 로 등록한 developer macro 를 런 시작에 orchestration 의
`GET /internal/projects/{projectId}/developer-macros` 로 한 번 읽는다. build 로 거르지 않는다 —
프로젝트의 것을 전부 싣고, planner 에게는 각 macro 가 어느 build 를 기준으로 쓰였는지와 이 런의
build 와 다른지를 보인다.

developer macro 는 build 의 등록된 macro 와 다른 namespace 다(`macros.DeveloperMacros`). 이름이
같아도 다른 macro 이므로 어느 쪽도 다른 쪽을 가리지 않는다. planner 는 developer macro 를 import 하지
않고, 답의 ```developer 블록에 step 마다 고른다. 고른 것은 `developer:<name>` 키로 executer 의
선택지가 되어 Jev 가 planner 의 `def` action 과 함께 고른다.

조회는 런을 멈추지 않는다. `project_id` 가 없거나, orchestration 이 없거나, 응답이 실패하거나
읽히지 않으면 log 를 남기고 빈 목록으로 돈다. 파싱되지 않는 macro 하나는 log 를 남기고 뺀다.
"""

import ast
import json
import logging
import re
from dataclasses import dataclass, replace

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.agents.planner_executer.contract import ActionContract, ContractBinder, DispatchBinder, registered_contract
from app.agents.planner_executer.macros import READY_FLAG, ActionPick, DeveloperAction, DeveloperMacros
from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.model import MacroDefinition
from app.agents.qa.macro.selector_check import reject_unstable_selectors
from app.config import get_settings

logger = logging.getLogger(__name__)

DEVELOPER_MACROS_PATH = "/internal/projects/{project_id}/developer-macros"

# executer 의 선택지에서 developer macro 의 키 앞에 붙는다. `:` 는 identifier 에 없으므로 planner 의
# `def` 이름과 겹치지 않는다.
DEVELOPER_ACTION_PREFIX = "developer:"
# 인자를 묶은 호출을 감싸는 진입점과, 꺼낸 `find(...)` 인자의 이름 앞에 붙는다.
BOUND_PREFIX = "bound_"
# ```developer 블록에서 인자를 매 turn 고르게 남기는 표시: `name(pick("Enemy(Clone)", ...))`.
PICK = "pick"
# developer macro 앞부분의 `require` 를 준비 조건으로 읽는 probe 진입점 이름 앞에 붙는다.
GUARD_PREFIX = "executer_guard_"

_DEVELOPER_BLOCK = re.compile(r"```developer[ \t]*\n(.*?)```", re.DOTALL)

# 런이 첫 step 을 쓰기 전에 기다리는 한 번의 왕복이다. `SCENE_CONTEXT_TIMEOUT_SECONDS` 와 같은 값이다.
DEVELOPER_MACROS_TIMEOUT_SECONDS = 5.0


class _DeveloperMacroItem(BaseModel):
    """`InternalDeveloperMacroListResponse.items` 한 줄 중 이 엔진이 읽는 field."""

    model_config = ConfigDict(alias_generator=to_camel, extra="ignore", frozen=True)

    name: str
    source: str
    # orchestration 의 다른 id 처럼 문자열이다(`GameBuildResponse.id`).
    game_build_id: str
    build_version: str
    build_label: str | None = None
    # 등록 때 검사를 통과한 행동 계약. 계약 없이 올라왔거나 orchestration 이 칸을 모르면 없다.
    contract: dict | None = None


class _DeveloperMacroList(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    items: list[_DeveloperMacroItem]


@dataclass(frozen=True)
class DeveloperMacro:
    name: str
    source: str
    build_version: str
    build_label: str | None
    # 이 런의 build 가 아닌 build 를 기준으로 쓰였다. 런의 build 를 모르면 False 다.
    other_build: bool
    contract: dict | None = None


@dataclass(frozen=True)
class UsableDeveloperMacro:
    """파싱을 통과한 developer macro 와 그 정의. planner 가 읽는 목록의 한 줄이다."""

    macro: DeveloperMacro
    definition: MacroDefinition
    # 등록 계약을 다시 읽은 것. 없거나 읽히지 않으면 `None` 이다.
    contract: "ActionContract | None" = None
    # 계약이 등록돼 있었는데 지금 읽히지 않은 이유. 계약이 없던 macro 와 가른다 — 행동 지침 모드는 이 macro 를 계약
    # 없이 돌리지 않고 고를 수 없는 것으로 둔다. 다른 모드는 전처럼 legacy 로 돈다.
    contract_error: str | None = None


async def fetch_developer_macros(
    project_id: int | None,
    game_build_id: int | None,
    base_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[DeveloperMacro, ...]:
    """프로젝트의 developer macro 전부. 어떤 이유로든 못 읽으면 빈 tuple 이다."""
    if project_id is None:
        logger.warning("[QA] no project_id on the run, running without developer macros")
        return ()
    root = base_url if base_url is not None else get_settings().orchestration_base_url
    if not root:
        # 물을 orchestration 이 없다. 로컬 런과 테스트가 여기다.
        return ()
    url = root.rstrip("/") + DEVELOPER_MACROS_PATH.format(project_id=project_id)
    try:
        if client is not None:
            response = await client.get(url)
        else:
            async with httpx.AsyncClient(timeout=DEVELOPER_MACROS_TIMEOUT_SECONDS) as owned:
                response = await owned.get(url)
        response.raise_for_status()
        body = _DeveloperMacroList.model_validate_json(response.content)
    except Exception as error:  # noqa: BLE001 - 조회 하나로 런이 시작하지 못하면 안 된다
        logger.warning("[QA] developer macros unavailable, running without them: %s", error)
        return ()
    run_build = str(game_build_id) if game_build_id is not None else None
    return tuple(
        DeveloperMacro(
            name=item.name,
            source=item.source,
            build_version=item.build_version,
            build_label=item.build_label,
            other_build=run_build is not None and item.game_build_id != run_build,
            contract=item.contract,
        )
        for item in body.items
    )


@dataclass(frozen=True)
class LoadedDeveloperMacros:
    """런의 developer macro: 그 namespace 와, 파싱을 통과한 것(이름 → 한 줄, 받은 순서)."""

    resolver: DeveloperMacros
    usable: dict[str, UsableDeveloperMacro]


NO_DEVELOPER_MACROS = LoadedDeveloperMacros(DeveloperMacros({}), {})


async def load_developer_macros(macros: tuple[DeveloperMacro, ...]) -> LoadedDeveloperMacros:
    """developer macro 를 그 namespace 안에서 파싱한다. 안 되는 것은 log 를 남기고 뺀다."""
    resolver = DeveloperMacros({macro.name: macro.source for macro in macros})
    usable: dict[str, UsableDeveloperMacro] = {}
    for macro in macros:
        definition, problem = await resolver.definition(macro.name)
        if definition is None:
            logger.warning("[QA] developer macro %s skipped: %s", macro.name, problem)
            continue
        contract = None
        contract_error = None
        if macro.contract is not None:
            try:
                contract = registered_contract(macro.name, macro.contract, macro.source)
            except MacroRejection as rejection:
                # 등록 때 통과한 계약이다. 지금 읽히지 않으면 저장된 계약과 검사 규칙이 어긋난 것이다.
                contract_error = rejection.render()
                logger.warning("[QA] developer macro %s contract unreadable: %s", macro.name, contract_error)
        usable[macro.name] = UsableDeveloperMacro(macro, definition, contract, contract_error)
    return LoadedDeveloperMacros(resolver, usable)


def split_developer_block(answer: str) -> tuple[str, list[str]]:
    """planner 답에서 ```developer 블록을 떼어 낸 나머지와 그 블록의 줄들.

    나머지만 `extract_code` 에 넘긴다. 블록이 python 블록보다 앞에 오면, 떼지 않은 답에서는 그
    블록의 닫는 fence 가 python 블록의 여는 fence 로 읽힌다.
    """
    match = _DEVELOPER_BLOCK.search(answer)
    if match is None:
        return answer, []
    calls = [line.strip() for line in match.group(1).splitlines() if line.strip()]
    return answer[: match.start()] + answer[match.end() :], calls


async def bind_developer_calls(
    calls: list[str], loaded: LoadedDeveloperMacros
) -> dict[str, DeveloperAction]:
    """고른 developer macro 마다 executer 가 인자 없이 부를 action 하나. 못 하면 `MacroRejection`.

    인자를 묶은 호출은 `from macros import <name>` 과 그 호출 하나만 있는 진입점으로 감싸
    developer namespace 에서 파싱한다. 그러면 인자의 개수·타입은 macro parser 가 검사하고, 거절은
    planner 의 rewrite 로 돌아간다.
    """
    actions: dict[str, DeveloperAction] = {}
    for call in calls:
        name, statements, bound, picked = _bound_call(call, loaded)
        entry = f"{BOUND_PREFIX}{name}"
        source = f"from macros import {name}\n\n\ndef {entry}() -> None:\n" + "".join(
            f"    {statement}\n" for statement in statements
        )
        try:
            reject_unstable_selectors(source)
            definition = await loaded.resolver.parse(entry, source)
        except MacroRejection as rejection:
            raise MacroRejection(
                f"the developer macro call `{call}` is refused: {rejection.render()}"
            ) from rejection
        key = DEVELOPER_ACTION_PREFIX + name
        count = 2
        while key in actions:
            key = f"{DEVELOPER_ACTION_PREFIX}{name}#{count}"
            count += 1
        usable = loaded.usable[name]
        description = usable.definition.entry.docstring or ""
        whole, by_parameter = leading_requires(usable.macro.source, name)
        ready = await _guard(loaded, usable.macro.source, name, whole)
        pick = None
        if picked is not None:
            placeholder, names, parameter = picked
            pick = ActionPick(
                names,
                _binder(loaded, entry, source, placeholder),
                _guard_binder(loaded, usable.macro.source, name, parameter, by_parameter.get(parameter, [])),
            )
            description += (
                f"\n\nCalled in this step on one object picked each turn among those named {', '.join(names)}: "
                "each option names the object it acts on."
            )
        elif bound:
            description += f"\n\nCalled in this step as `{call}`."
        actions[key] = DeveloperAction(definition, description, pick, ready)
    return actions


_ACTION_NAME = re.compile(r"^[A-Za-z_]\w*(\(\))?$")


async def bind_guidance_calls(
    calls: list[str], loaded: LoadedDeveloperMacros
) -> tuple[dict[str, DeveloperAction], dict[str, ActionContract], DispatchBinder]:
    """행동 지침 모드의 ```developer 블록: 등록 행동의 이름만 받는다. 못 하면 `MacroRejection`.

    `(인자 없는 legacy 행동, 등록 계약 행동, 그 계약들의 binder)`. 인자·`pick` 은 받지 않는다 — 대상은 등록 계약의 `holds` 로
    executer 가 지금 화면에서 펼치고 Jev 가 고른다. 인자를 받는데 계약 없이 올라온 macro 는 대상을 펼칠 길이 없어 거절한다.
    """
    actions: dict[str, DeveloperAction] = {}
    contracts: dict[str, ActionContract] = {}
    binders: dict[str, tuple[ContractBinder, str]] = {}
    for call in calls:
        if not _ACTION_NAME.match(call):
            raise MacroRejection(
                f"`{call}` in the ```developer block: write only the names of registered actions, one per line, with "
                "no arguments. The executer finds the objects an action can act on, and the decision model picks one."
            )
        name = call.removesuffix("()")
        usable = loaded.usable.get(name)
        if usable is None:
            known = ", ".join(sorted(loaded.usable)) or "(none)"
            raise MacroRejection(f"`{name}` is not a registered action. The registered actions are: {known}.")
        key = DEVELOPER_ACTION_PREFIX + name
        if key in actions or key in contracts:
            raise MacroRejection(f"`{name}` appears twice in the ```developer block; list each action once.")
        if usable.contract_error is not None:
            # 계약이 있던 행동을 계약 없이 돌리면 대상·준비·효과를 아무도 보지 않는다.
            raise MacroRejection(
                f"`{name}` is not available: its registered contract cannot be read ({usable.contract_error}). Leave it "
                "out; the catalog needs that action fixed."
            )
        if usable.contract is not None:
            contracts[key] = replace(usable.contract, description=usable.definition.entry.docstring or "")
            binders[key] = (ContractBinder(usable.macro.source, {name: usable.contract}, loaded.resolver.parse), name)
        elif not usable.definition.entry.parameters:
            actions.update(await bind_developer_calls([f"{name}()"], loaded))
        else:
            parameters = ", ".join(parameter.name for parameter in usable.definition.entry.parameters)
            raise MacroRejection(
                f"`{name}` acts on an object ({parameters}) but was registered without a contract, so the executer "
                "cannot find the objects it may act on. Leave it out; the catalog needs a contract for it."
            )
    return actions, contracts, DispatchBinder(binders)


def leading_requires(source: str, name: str) -> tuple[list[str], dict[str, list[str]]]:
    """developer macro `name` 의 본문 앞부분(docstring 다음, 첫 다른 statement 전)에 있는 `require` 조건의 글.

    `(인자를 읽지 않는 조건, 인자 하나만 읽는 조건을 그 인자 이름별로)`. 둘 이상의 인자를 읽는 조건은 뺀다. 앞부분만 보는
    것은 첫 입력·대입 뒤의 `require` 가 그 결과를 보기 때문이다 — 고르기 전에 읽을 조건이 아니다.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], {}
    node = next((item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name), None)
    if node is None:
        return [], {}
    parameters = {argument.arg for argument in node.args.args}
    body = node.body[1:] if ast.get_docstring(node) is not None else node.body
    whole: list[str] = []
    by_parameter: dict[str, list[str]] = {}
    for statement in body:
        call = statement.value if isinstance(statement, ast.Expr) else None
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "require" and call.args):
            break
        condition = call.args[0]
        text = ast.get_source_segment(source, condition)
        if not text:
            continue
        read = {item.id for item in ast.walk(condition) if isinstance(item, ast.Name)} & parameters
        if not read:
            whole.append(text)
        elif len(read) == 1:
            by_parameter.setdefault(read.pop(), []).append(text)
    return whole, by_parameter


def _guard_source(source: str, name: str, conditions: list[str], bound: str = "") -> str:
    """조건이 모두 참이면 `READY_FLAG` 를 남기는 probe 를 그 developer macro 원문 뒤에 붙인 글. 원문의 helper 를 그대로 부른다."""
    lines = [f"def {GUARD_PREFIX}{name}() -> None:"]
    if bound:
        lines.append(f"    {bound}")
    for depth, condition in enumerate(conditions):
        lines.append("    " * (depth + 1) + f"if {condition}:")
    lines.append("    " * (len(conditions) + 1) + f'flag("{READY_FLAG}")')
    return source.rstrip("\n") + "\n\n\n" + "\n".join(lines) + "\n"


async def _guard(loaded: LoadedDeveloperMacros, source: str, name: str, conditions: list[str]) -> MacroDefinition | None:
    """인자를 읽지 않는 앞부분 `require` 의 probe. 없거나 파싱되지 않으면 `None` 이다 — 준비 조건이 없는 것으로 둔다."""
    if not conditions:
        return None
    try:
        return await loaded.resolver.parse(GUARD_PREFIX + name, _guard_source(source, name, conditions))
    except MacroRejection as rejection:
        logger.warning("[QA] developer macro %s: leading requires not readable as a ready check: %s", name, rejection.render())
        return None


def _guard_binder(loaded: LoadedDeveloperMacros, source: str, name: str, parameter: str, conditions: list[str]):
    """고른 객체를 `parameter` 에 넣은 앞부분 `require` 의 probe 를 주는 함수. 조건이 없으면 `None` 이다."""
    if not conditions:
        return None
    parsed: dict[str, MacroDefinition | None] = {}

    async def bind(path: str) -> MacroDefinition | None:
        if path not in parsed:
            bound = f"{parameter}: object = {_selector(path)}"
            try:
                parsed[path] = await loaded.resolver.parse(GUARD_PREFIX + name, _guard_source(source, name, conditions, bound))
            except MacroRejection:
                parsed[path] = None
        return parsed[path]

    return bind


def _binder(loaded: LoadedDeveloperMacros, entry: str, source: str, placeholder: str):
    """고른 객체의 path 로 그 호출을 파싱하는 함수. 같은 객체는 한 번만 파싱한다."""
    parsed: dict[str, MacroDefinition] = {}

    async def bind(path: str) -> MacroDefinition:
        if path not in parsed:
            parsed[path] = await loaded.resolver.parse(entry, source.replace(placeholder, _selector(path), 1))
        return parsed[path]

    return bind


def _selector(path: str) -> str:
    return "selector(" + json.dumps(path, ensure_ascii=False) + ")"


def _bound_call(
    call: str, loaded: LoadedDeveloperMacros
) -> tuple[str, list[str], bool, tuple[str, tuple[str, ...], str] | None]:
    """`(macro 이름, 진입점 몸통의 statement 들, 인자가 있는지, pick)`. pick 은 `pick(...)` 으로 남긴 인자의
    `(자리 표시 selector 글, 후보 이름들, parameter 이름)` 이다. 한 호출에 하나만 둘 수 있다.

    `find(...)` 는 인자 자리에 바로 쓸 수 없다(parser 가 "Bind it to a name first" 로 거절한다). 그런
    인자는 그 parameter 이름의 `object` 대입으로 앞에 꺼낸다. 나머지 인자는 쓴 글 그대로 둔다.
    """
    try:
        expression = ast.parse(call, mode="eval").body
    except SyntaxError:
        expression = None
    if isinstance(expression, ast.Name):
        name, arguments, keywords = expression.id, [], []
    elif isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name):
        name, arguments, keywords = expression.func.id, expression.args, expression.keywords
    else:
        raise MacroRejection(
            f"`{call}` in the ```developer block is not a developer macro's name or a call of one. "
            "Write one per line: the name alone, or `name(argument, ...)`."
        )
    if name not in loaded.usable:
        known = ", ".join(f"`{known}`" for known in loaded.usable) or "none"
        raise MacroRejection(
            f"`{name}` in the ```developer block is not a developer macro of this project. "
            f"The ones there: {known}."
        )
    parameters = [parameter.name for parameter in loaded.usable[name].definition.entry.parameters]
    hoisted: list[str] = []
    picks: list[tuple[str, tuple[str, ...], str]] = []

    def written(node: ast.expr, parameter: str) -> str:
        text = ast.get_source_segment(call, node) or ""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == PICK:
            names = tuple(
                argument.value for argument in node.args
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str) and argument.value.strip()
            )
            if not names or len(names) != len(node.args) or node.keywords:
                raise MacroRejection(
                    f"`{text}` in `{call}`: `pick` takes the names of the objects to pick from, as strings — "
                    'for example `pick("Enemy(Clone)", "Boss(Clone)")`.'
                )
            placeholder = _selector(names[0])
            picks.append((placeholder, names, parameter))
            hoisted.append(f"{BOUND_PREFIX}{parameter}: object = {placeholder}")
            return f"{BOUND_PREFIX}{parameter}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "find":
            hoisted.append(f"{BOUND_PREFIX}{parameter}: object = {text}")
            return f"{BOUND_PREFIX}{parameter}"
        return text

    pieces = [
        written(node, parameters[index] if index < len(parameters) else f"argument_{index}")
        for index, node in enumerate(arguments)
    ]
    pieces += [f"{keyword.arg}={written(keyword.value, keyword.arg or 'argument')}" for keyword in keywords]
    if len(picks) > 1:
        raise MacroRejection(f"`{call}` has more than one `pick(...)`; leave one argument to pick and bind the others.")
    return name, [*hoisted, f"{name}({', '.join(pieces)})"], bool(pieces), picks[0] if picks else None
