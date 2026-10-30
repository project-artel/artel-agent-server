"""Orchestration 이 든 게임 상태(`STATE_SET`, ARTEL-1296)를 읽어 Jev 의 state 로 그린다 (ARTEL-1297).

Orchestration 은 SDK 의 `pulse` 를 Agent 에 중계하기 전에 게임 인스턴스별로 합쳐 둔다. 이 모듈은 그것을
`POST /internal/qa-tries/{qaTryId}/state` 로 읽고, Executer 가 Jev 에게 보내던 state 와 **같은 모양**으로
그린다 — 객체마다 `{"path", "text"?, "members", <adapter 조각>}`, 상한도 같다
(`app/agents/qa/macro/context.py`). 출처만 다르다. 그래야 `state_source` 두 값의 비교가 출처의 차이만 잰다.

`PulseMemory` 와 다른 점은 계약에서 온다.

- 마지막 whole 에 다시 실리지 않은 객체와 멤버는 Orchestration 에서 `STALE` 이다. 행동에 쓰지 않는 값이라
  넣지 않는다. `PulseMemory` 는 whole 이 오면 쥔 것을 통째로 갈아 같은 객체가 애초에 없다.
- 동기화 전(`AWAITING_WHOLE`)의 상태로는 그리지 않는다. 호출자가 기다리거나 멈춘다.
- 객체 수명은 `entityId` 로 잇는다. 그리는 순서는 `PulseMemory` 와 같게 `씬/selector` 순이다.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.agents.qa.macro.context import DEFAULT_OBJECTS_KEY, clip_value
# 같은 상한 규칙으로 자른다. 출처가 달라도 Jev 가 받는 글의 크기 규칙이 같아야 비교가 출처만 잰다.
from app.agents.qa.macro.context import _fit as fit_state
from app.config import get_settings

logger = logging.getLogger(__name__)

STATE_PATH = "/internal/qa-tries/{qa_try_id}/state"
STATE_TIMEOUT_SECONDS = 5.0
PERSISTENT_SCOPE = "persistent"
PERSISTENT_SCENE = "DontDestroyOnLoad"
# Executer 가 묻는 범위. live object 전부 — `PulseMemory` 에서 읽던 것과 같다. static 은 그때도 안 실었다.
EXECUTER_SPEC: dict[str, Any] = {
    "scopes": [PERSISTENT_SCOPE, "scene:*"],
    "includeInactive": False,
    "includeStale": False,
    "maxEntities": 5000,
}


class StateSetUnavailable(Exception):
    """상태를 읽지 못했다. 이유가 message 다."""


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class StateObservation(_Wire):
    status: Literal["KNOWN", "UNREAD", "UNKNOWN", "STALE"]
    value: Any = None
    count: int | None = None
    reason: str | None = None
    observed_frame: int | None = None
    source_reading: int | None = None


class StateEntity(_Wire):
    entity_id: str
    sdk_id: int | None = None
    scope: str
    path: str | None = None
    selector: str | None = None
    tag: str | None = None
    active: bool
    status: Literal["KNOWN", "STALE"]
    fragments: dict[str, StateObservation] = Field(default_factory=dict)
    members: dict[str, StateObservation] = Field(default_factory=dict)

    @property
    def scene(self) -> str:
        if self.scope == PERSISTENT_SCOPE:
            return PERSISTENT_SCENE
        return self.scope.removeprefix("scene:").rsplit("#", 1)[0]

    @property
    def address(self) -> str:
        """`PulseMemory` 가 객체를 세는 키와 같은 꼴(`씬/selector`). 그리는 순서가 이것이다."""
        return f"{self.scene}/{self.selector or self.path or ''}"


class StateSnapshot(_Wire):
    state_version: int
    stream_id: str | None = None
    sync: Literal["AWAITING_WHOLE", "SYNCED"]
    reading: int | None = None
    frame: int | None = None
    scene: str | None = None
    entities: list[StateEntity] = Field(default_factory=list)
    unobserved: list[str] = Field(default_factory=list)
    omitted_entities: int = 0

    @property
    def synced(self) -> bool:
        return self.sync == "SYNCED"


async def read_state(
    qa_try_id: int,
    spec: dict[str, Any],
    base_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> StateSnapshot:
    """try 의 게임 상태 하나. 못 읽으면 `StateSetUnavailable` 이다 — `PulseMemory` 로 되돌아가지 않는다."""
    root = base_url if base_url is not None else get_settings().orchestration_base_url
    if not root:
        raise StateSetUnavailable("no orchestration_base_url to read STATE_SET from")
    url = root.rstrip("/") + STATE_PATH.format(qa_try_id=qa_try_id)
    try:
        if client is not None:
            response = await client.post(url, json=spec)
        else:
            async with httpx.AsyncClient(timeout=STATE_TIMEOUT_SECONDS) as owned:
                response = await owned.post(url, json=spec)
        response.raise_for_status()
        return StateSnapshot.model_validate_json(response.content)
    except Exception as error:  # noqa: BLE001 - 호출자에게는 실패 타입 하나만 보인다
        raise StateSetUnavailable(f"STATE_SET read failed: {error}") from error


@dataclass(frozen=True)
class StateView:
    """Jev 에게 보일 범위(ARTEL-1295 C). Planner 의 ```observe 블록에서 오고, `EXPAND_STATE` 가 넓힌다.

    `paths` 는 객체 경로 접두어, `members` 는 `Type.member` 짧은 이름이다. Planner 가 화면에서 읽은 글자 그대로라
    selector 의 `[n]` 이 붙었든 안 붙었든 맞춘다. `None` 은 그 축으로 거르지 않는다는 뜻이다.
    `active_scene_first` 는 활성 씬의 객체를 DontDestroyOnLoad 객체보다 앞에 세운다 — 상한에 걸릴 때 지금 화면의
    객체가 먼저 남는다. `False` 는 pe9 의 `씬/selector` 순서다.
    """

    paths: tuple[str, ...] | None = None
    members: tuple[str, ...] | None = None
    active_scene_first: bool = False


EVERYTHING = StateView()
_INDEX = re.compile(r"\[\d+\]")


def _plain(path: str) -> str:
    return _INDEX.sub("", path).strip("/")


def _under(entity: "StateEntity", prefixes: tuple[str, ...]) -> bool:
    candidates = {_plain(entity.selector or ""), _plain(entity.path or "")} - {""}
    for prefix in (_plain(raw) for raw in prefixes):
        if any(name == prefix or name.startswith(prefix + "/") for name in candidates):
            return True
    return False


def render_jev_state(
    snapshot: StateSnapshot,
    is_cosmetic: Any = lambda _type_key: False,
    scene: str | None = None,
    view: StateView = EVERYTHING,
) -> tuple[dict[str, Any], int]:
    """`(state, clipped)`. `build_context_state(..., context=())` 와 같은 모양이다.

    `is_cosmetic` 은 `PulseMemory.is_cosmetic` 이다. relevance 판정은 Agent 가 내린 것이라 Agent 쪽에 있다.
    `view` 가 범위를 좁히면 고른 멤버 중 어디에도 없던 이름을 `unknown_members` 로 적는다 — 없는 값이 아니라
    이 상태에 실린 적 없는 값이다.
    """
    live = [entity for entity in snapshot.entities if entity.active and entity.status == "KNOWN"]
    if view.paths is not None:
        live = [entity for entity in live if _under(entity, view.paths)]
    active = (scene or snapshot.scene or "").strip()
    if view.active_scene_first:
        order = lambda entity: (entity.scene != active, entity.address)  # noqa: E731
    else:
        order = lambda entity: entity.address  # noqa: E731
    wanted = set(view.members) if view.members is not None else None
    seen: set[str] = set()
    objects = [_render_entity(entity, is_cosmetic, wanted, seen) for entity in sorted(live, key=order)]
    state: dict[str, Any] = {"scene": active, DEFAULT_OBJECTS_KEY: objects}
    if wanted:
        unknown = sorted(name for name in wanted if name not in seen)
        if unknown:
            state["unknown_members"] = unknown
    clipped = fit_state(state)
    return state, clipped


def _render_entity(
    entity: StateEntity, is_cosmetic: Any, wanted: set[str] | None = None, seen: set[str] | None = None
) -> dict[str, Any]:
    """`context.render_object` 와 같은 규칙: 짧은 멤버 이름, 겹치면 `#among`, cosmetic 은 뺀다.

    `wanted` 가 있으면 그 짧은 이름(`Type.member`)이나 `Type::member` 전체 이름에 맞는 멤버만 남기고, 맞은 이름을
    `seen` 에 적는다.
    """
    shown = [
        (member_id, observation)
        for member_id, observation in entity.members.items()
        if observation.status in ("KNOWN", "UNREAD") and not is_cosmetic(member_id.rsplit("#", 1)[0])
    ]
    if wanted is not None:
        kept = []
        for member_id, observation in shown:
            names = {_short_name(member_id), member_id.rsplit("#", 1)[0]} & wanted
            if names:
                kept.append((member_id, observation))
                if seen is not None:
                    seen.update(names)
        shown = kept
    short_names = [_short_name(member_id) for member_id, _ in shown]
    members: dict[str, Any] = {}
    for (member_id, observation), short in zip(shown, short_names):
        among = member_id.rsplit("#", 1)[1] if "#" in member_id else "0"
        name = f"{short}#{among}" if short_names.count(short) > 1 else short
        members[name] = clip_value(_observed(observation))
    rendered: dict[str, Any] = {"path": entity.selector or entity.path or entity.entity_id}
    text = entity.fragments.get("text")
    if text is not None and text.status == "KNOWN" and text.value is not None:
        rendered["text"] = clip_value(text.value)
    rendered["members"] = {name: members[name] for name in sorted(members)}
    for fragment_name in ("ui", "engine", "uitk"):
        fragment = entity.fragments.get(fragment_name)
        if fragment is not None and fragment.status == "KNOWN" and fragment.value is not None:
            rendered[fragment_name] = clip_value(fragment.value)
    return rendered


def _short_name(member_id: str) -> str:
    """`Battle.Player::hp#0` → `Player.hp`. `member(<target>, "Type.member")` 가 받는 꼴이다."""
    type_key = member_id.rsplit("#", 1)[0]
    declaring, _, member = type_key.partition("::")
    return f"{declaring.split('.')[-1]}.{member}"


def _observed(observation: StateObservation) -> Any:
    if observation.status == "UNREAD":
        return f"(unread: {observation.reason})"
    return observation.value
