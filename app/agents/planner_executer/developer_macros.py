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
import logging
import re
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.agents.planner_executer.macros import DeveloperAction, DeveloperMacros, first_sentence
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


@dataclass(frozen=True)
class UsableDeveloperMacro:
    """파싱을 통과한 developer macro 와 그 정의. planner 가 읽는 목록의 한 줄이다."""

    macro: DeveloperMacro
    definition: MacroDefinition


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
        usable[macro.name] = UsableDeveloperMacro(macro, definition)
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
    for line in calls:
        call, label = split_comment(line)
        name, statements, bound = _bound_call(call, loaded)
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
        description = loaded.usable[name].definition.entry.docstring or ""
        if bound:
            description += f"\n\nCalled in this step as `{call}`."
        actions[key] = DeveloperAction(definition, description, label or first_sentence(description))
    return actions


def split_comment(line: str) -> tuple[str, str]:
    """`(호출, 줄 끝 주석)`. 따옴표 안의 `#` 은 주석이 아니다. 주석은 planner 가 그 action 에 붙인 이름이다."""
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#":
            return line[:index].strip(), line[index + 1 :].strip()
    return line.strip(), ""


def _bound_call(call: str, loaded: LoadedDeveloperMacros) -> tuple[str, list[str], bool]:
    """`(macro 이름, 진입점 몸통의 statement 들, 인자가 있는지)`.

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

    def written(node: ast.expr, parameter: str) -> str:
        text = ast.get_source_segment(call, node) or ""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "find":
            hoisted.append(f"{BOUND_PREFIX}{parameter}: object = {text}")
            return f"{BOUND_PREFIX}{parameter}"
        return text

    pieces = [
        written(node, parameters[index] if index < len(parameters) else f"argument_{index}")
        for index, node in enumerate(arguments)
    ]
    pieces += [f"{keyword.arg}={written(keyword.value, keyword.arg or 'argument')}" for keyword in keywords]
    return name, [*hoisted, f"{name}({', '.join(pieces)})"], bool(pieces)
