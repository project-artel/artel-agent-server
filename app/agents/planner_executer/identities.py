"""게임 값의 뜻을 개발자가 선언한 것(identity 선언). 대상 해석(`target.TargetResolver`)과 step 을 시작할 때 확인할 값을
고르는 `target.observe` 가 짐작 대신 읽는다.

해석은 값의 이름과 지금 값만 본다. 이름이 비슷한 값이 둘이면(지금 들어간 스테이지 `StageDataSingleton.stagePosition`,
첫 클리어에 1 오르는 가장 먼 스테이지 `MapMove.StagePosition`) 같은 입력에서도 런마다 다른 값을 골랐다(런 233·234). 값의
뜻은 게임 코드에만 있다. 그래서 게임 코드를 아는 쪽(개발자의 코딩 에이전트)이 MCP(`publish_identities`)로 선언하고,
orchestration이 build별 revision을 고정하고 QA 시작에 snapshot을 보낸다. 새 세션은 그 snapshot을 사용하고,
이전 프로토콜에서 snapshot이 빠진 경우에만 해당 build 목록을 읽는다. 선언에는 게임 소스의 위치를 싣지 않는다 —
게임 정보는 우리 서버에 최소로 둔다(`code_ref` 는 두지 않는다).
"""

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.config import get_settings

logger = logging.getLogger(__name__)

GAME_IDENTITIES_PATH = "/internal/projects/{project_id}/game-identities"
GAME_IDENTITIES_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class GameIdentity:
    name: str
    kind: str
    base: int | None
    meaning: str
    changes_when: str
    # 이 값이 가려내는 것("stage")과 그 역할. `current` 는 지금 들어간·고른 것, `progress` 는 어디까지 왔는지, `other` 는 그 밖이다.
    # 대상을 확인하는 데는 `current` 만 쓴다(`target.observe`).
    identifies: str
    role: str

    def line(self) -> str:
        counted = "" if self.base is None else f", counting from {self.base}"
        return f"- {self.name} ({self.role} {self.identifies}; {self.kind}{counted}): {self.meaning} It changes {self.changes_when}."


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class _Identity(_Wire):
    name: str
    kind: str
    base: int | None = None
    meaning: str
    changes_when: str
    identifies: str
    role: str


class GameIdentitySnapshot(_Identity):
    """orchestration이 QA 시작에 고정한 선언. 출처와 revision도 세션 기록에 보존한다."""

    revision_id: str
    game_build_id: str
    version: int = Field(ge=1)
    source: str
    id: str | None = None
    deleted: bool = False
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def identity(self) -> GameIdentity:
        return GameIdentity(
            self.name, self.kind, self.base, self.meaning, self.changes_when, self.identifies, self.role
        )


class _IdentityList(_Wire):
    identities: list[_Identity] = []


async def fetch_game_identities(
    project_id: int | None,
    base_url: str | None = None,
    client: httpx.AsyncClient | None = None,
    *,
    game_build_id: int | None = None,
) -> tuple[GameIdentity, ...]:
    """이전 프로토콜의 build별 선언 조회. build가 없거나 조회에 실패하면 빈 tuple을 반환한다."""
    if project_id is None or game_build_id is None:
        return ()
    root = base_url if base_url is not None else get_settings().orchestration_base_url
    if not root:
        return ()
    url = root.rstrip("/") + GAME_IDENTITIES_PATH.format(project_id=project_id)
    try:
        if client is not None:
            response = await client.get(url, params={"gameBuildId": game_build_id})
        else:
            async with httpx.AsyncClient(timeout=GAME_IDENTITIES_TIMEOUT_SECONDS) as owned:
                response = await owned.get(url, params={"gameBuildId": game_build_id})
        response.raise_for_status()
        body = _IdentityList.model_validate_json(response.content)
    except Exception as error:  # noqa: BLE001 - 조회 하나로 런이 시작하지 못하면 안 된다
        logger.warning("[QA] game identities unavailable, resolving targets without them: %s", error)
        return ()
    return tuple(
        GameIdentity(item.name, item.kind, item.base, item.meaning, item.changes_when, item.identifies, item.role)
        for item in body.identities
    )
