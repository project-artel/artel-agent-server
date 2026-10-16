"""프로젝트의 developer macro 를 planner–executer 런에 싣는다 (ARTEL-1101).

게임 개발자가 MCP 로 등록한 developer macro 를 런 시작에 orchestration 의
`GET /internal/projects/{projectId}/developer-macros` 로 한 번 읽는다. build 로 거르지 않는다 —
프로젝트의 것을 전부 싣고, planner 에게는 각 macro 가 어느 build 를 기준으로 쓰였는지와 이 런의
build 와 다른지를 보인다.

조회는 런을 멈추지 않는다. `project_id` 가 없거나, orchestration 이 없거나, 응답이 실패하거나
읽히지 않으면 log 를 남기고 빈 목록으로 돈다. 파싱되지 않는 macro 하나는 log 를 남기고 뺀다.
파싱은 `RegisteredMacros` 가 한다 — planner 의 `from macros import` 를 푸는 곳이 그곳이라, 여기서
통과한 macro 는 planner 가 가져올 때도 같은 정의로 풀린다.
"""

import logging
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.agents.planner_executer.macros import RegisteredMacros
from app.agents.qa.macro.model import MacroDefinition
from app.config import get_settings

logger = logging.getLogger(__name__)

DEVELOPER_MACROS_PATH = "/internal/projects/{project_id}/developer-macros"

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


async def usable_developer_macros(
    macros: tuple[DeveloperMacro, ...], registered: RegisteredMacros
) -> list[UsableDeveloperMacro]:
    """`registered` 로 파싱되는 developer macro 만, 받은 순서대로. 안 되는 것은 log 를 남기고 뺀다."""
    usable = []
    for macro in macros:
        definition, problem = await registered.definition(macro.name)
        if definition is None:
            logger.warning("[QA] developer macro %s skipped: %s", macro.name, problem)
            continue
        usable.append(UsableDeveloperMacro(macro, definition))
    return usable
