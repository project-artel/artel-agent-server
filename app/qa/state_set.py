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

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.agents.qa.macro.binding import PERSISTENT_SCENE
from app.agents.qa.macro.context import DEFAULT_OBJECTS_KEY, clip_value

# 같은 상한 규칙으로 자른다. 출처가 달라도 Jev 가 받는 글의 크기 규칙이 같아야 비교가 출처만 잰다.
from app.agents.qa.macro.context import _fit as fit_state
from app.agents.qa.macro.grammar import MAX_DECIDE_STATE_CHARS
from app.config import get_settings

logger = logging.getLogger(__name__)

STATE_PATH = "/internal/qa-tries/{qa_try_id}/state"
STATE_TIMEOUT_SECONDS = 5.0
PERSISTENT_SCOPE = "persistent"
# Executer 가 묻는 범위. live object 전부와 static(`runtime`). 거르는 것은 Agent 다(`StateView`) — Planner 가 화면에서
# 읽은 이름(selector, `Type.member`)이 서버의 id 꼴과 달라서다. `planned` 가 아니면 static 은 그리지 않는다.
EXECUTER_SPEC: dict[str, Any] = {
    "scopes": [PERSISTENT_SCOPE, "scene:*", "runtime"],
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


class StateStatic(_Wire):
    member_id: str
    holder_path: str | None = None
    observation: StateObservation


class StateSnapshot(_Wire):
    state_version: int
    stream_id: str | None = None
    sync: Literal["AWAITING_WHOLE", "SYNCED"]
    reading: int | None = None
    frame: int | None = None
    scene: str | None = None
    entities: list[StateEntity] = Field(default_factory=list)
    statics: list[StateStatic] = Field(default_factory=list)
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
    """Jev 에게 보일 범위(ARTEL-1298). Planner 의 ```observe 블록에서 오고, `EXPAND_STATE` 가 넓힌다.

    `paths` 는 객체 경로 접두어, `members` 는 `Type.member` 짧은 이름이다. Planner 가 화면에서 읽은 글자 그대로라 selector 의
    `[n]` 이 붙었든 안 붙었든 맞춘다. `None` 은 그 축으로 거르지 않는다는 뜻이다.

    `planned` 면 범위가 씬 객체·DontDestroyOnLoad 객체·static 에 똑같이 걸린다. static 은 `statics` 키에 실리고, 범위에
    안 든 것은 값 없이 `hidden` 에 그룹(객체는 최상위 경로마다 수, static 은 이름)으로만 적는다 — Jev 가 무엇이 가려졌는지
    알아야 `EXPAND_STATE` 를 고를 수 있다. 활성 씬의 객체를 DontDestroyOnLoad 객체보다 앞에 세운다. `planned` 가 아니면
    예전 모양(`씬/selector` 순서, static 없음)이다.
    """

    paths: tuple[str, ...] | None = None
    members: tuple[str, ...] | None = None
    planned: bool = False
    # Jev state 의 상한(글자). `None` 이면 macro `decide`·`ask` 와 같은 `MAX_DECIDE_STATE_CHARS` 다.
    limit: int | None = None
    # planner 가 고른 fact 의 source: `(scope, 이름)`. `planned` 일 때 범위와 상관없이 그 member 를 가진 객체와 그 static 을
    # 보인다. planner 가 경로를 다시 쓰지 않아도 고른 fact 가 읽히게 하려는 것이다. 객체의 다른 member 는 범위를 따른다.
    facts: tuple[tuple[str, str], ...] = ()


EVERYTHING = StateView()
_INDEX = re.compile(r"\[\d+\]")


def _plain(path: str) -> str:
    # 화면이 DontDestroyOnLoad 객체를 `DontDestroyOnLoad/@persistent/…` 로 적을 때가 있다. selector 는 `@persistent/…` 다.
    return _INDEX.sub("", path).strip("/").removeprefix(f"{PERSISTENT_SCENE}/")


def _under(entity: "StateEntity", prefixes: tuple[str, ...]) -> bool:
    candidates = {_plain(entity.selector or ""), _plain(entity.path or "")} - {""}
    for prefix in (_plain(raw) for raw in prefixes):
        if any(name == prefix or name.startswith(prefix + "/") for name in candidates):
            return True
    return False


def unmatched_paths(snapshot: "StateSnapshot", paths: tuple[str, ...] | list[str] | None) -> list[str]:
    """`paths` 중 이 snapshot 의 어떤 객체에도 맞지 않는 것. 꺼진 객체·stale 객체도 맞은 것으로 센다."""
    return [path for path in paths or () if not any(_under(entity, (path,)) for entity in snapshot.entities)]


def render_jev_state(
    snapshot: StateSnapshot,
    is_cosmetic: Any = lambda _type_key: False,
    scene: str | None = None,
    view: StateView = EVERYTHING,
) -> tuple[dict[str, Any], int]:
    """`(state, clipped)`. `planned` 가 아니면 `build_context_state(..., context=())` 와 같은 모양이다.

    `is_cosmetic` 은 `PulseMemory.is_cosmetic` 이다. relevance 판정은 Agent 가 내린 것이라 Agent 쪽에 있다.
    """
    live = [entity for entity in snapshot.entities if entity.active and entity.status == "KNOWN"]
    active = (scene or snapshot.scene or "").strip()
    if view.planned:
        return _render_planned(snapshot, live, is_cosmetic, active, view)
    if view.paths is not None:
        live = [entity for entity in live if _under(entity, view.paths)]
    wanted = set(view.members) if view.members is not None else None
    seen: set[str] = set()
    objects = [_render_entity(entity, is_cosmetic, wanted, seen) for entity in sorted(live, key=lambda e: e.address)]
    state: dict[str, Any] = {"scene": active, DEFAULT_OBJECTS_KEY: objects}
    if wanted:
        unknown = sorted(name for name in wanted if name not in seen)
        if unknown:
            state["unknown_members"] = unknown
    clipped = fit_state(state)
    return state, clipped


def _render_planned(
    snapshot: StateSnapshot, live: list[StateEntity], is_cosmetic: Any, active: str, view: StateView
) -> tuple[dict[str, Any], int]:
    """`{"scene", "objects", "statics", "hidden"?, "unknown_members"?}`. 범위는 셋에 똑같이 걸린다."""
    fact_members = {name for scope, name in view.facts if scope == "objects"}
    fact_statics = {name for scope, name in view.facts if scope == "statics"}
    in_scope = [entity for entity in live if view.paths is None or _under(entity, view.paths)]
    scoped = {id(entity) for entity in in_scope}
    # 범위 밖이지만 고른 fact 의 member 를 가진 객체. 그 fact member 만 보인다.
    for_facts = [entity for entity in live if id(entity) not in scoped and _holds(entity, fact_members)]
    extra = {id(entity) for entity in for_facts}
    hidden_objects: dict[str, int] = {}
    for entity in live:
        if id(entity) not in scoped and id(entity) not in extra:
            root = _plain(entity.path or entity.selector or "").split("/", 1)[0] or entity.entity_id
            hidden_objects[root] = hidden_objects.get(root, 0) + 1
    shown = sorted(in_scope + for_facts, key=lambda entity: (entity.scene != active, entity.address))
    wanted = set(view.members) if view.members is not None else None
    seen: set[str] = set()
    objects = [
        _render_entity(
            entity, is_cosmetic,
            fact_members if id(entity) in extra else (None if wanted is None else wanted | fact_members),
            seen,
        )
        for entity in shown
    ]
    statics, hidden_statics = _render_statics(
        snapshot.statics, is_cosmetic, None if wanted is None else wanted | fact_statics, seen
    )
    state: dict[str, Any] = {"scene": active, DEFAULT_OBJECTS_KEY: objects, "statics": statics}
    hidden: dict[str, Any] = {}
    if hidden_objects:
        hidden["objects"] = {root: hidden_objects[root] for root in sorted(hidden_objects)}
    if hidden_statics:
        hidden["statics"] = hidden_statics
    if hidden:
        state["hidden"] = hidden
    if wanted:
        unknown = sorted(name for name in wanted if name not in seen)
        if unknown:
            state["unknown_members"] = unknown
    return state, _fit_within(state, view.limit or MAX_DECIDE_STATE_CHARS)


def _holds(entity: StateEntity, names: set[str]) -> bool:
    return bool(names) and any(
        _short_name(member_id) in names and observation.status in ("KNOWN", "UNREAD")
        for member_id, observation in entity.members.items()
    )


def declared_values(
    snapshot: StateSnapshot, sources: list[tuple[str, str, str]]
) -> tuple[dict[str, Any], dict[str, str]]:
    """`(이름 → 값, 이름 → unknown 인 까닭)`. `sources` 는 `(fact 이름, scope, source 이름)` 이다.

    렌더러와 같은 짧은 이름으로 정확히 맞춘다. 읽지 못한 값(없음·`UNREAD`)은 `"unknown"` 이고, 여러 객체나 holder 가 가진
    값은 같은 값이어도 하나를 고르지 않고 `"unknown"` 이다. 0·false 로 바꾸지 않는다.
    """
    values: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    live = [entity for entity in snapshot.entities if entity.active and entity.status == "KNOWN"]
    for name, scope, member in sources:
        if scope == "objects":
            found = [
                observation for entity in live for member_id, observation in entity.members.items()
                if _short_name(member_id) == member
            ]
        else:
            found = [entry.observation for entry in snapshot.statics if _short_name(entry.member_id.split("@", 1)[0]) == member]
        if not found:
            values[name], reasons[name] = "unknown", "not reported"
        elif len(found) > 1:
            values[name], reasons[name] = "unknown", f"{len(found)} holders"
        elif found[0].status != "KNOWN":
            values[name], reasons[name] = "unknown", f"not read ({found[0].status.lower()})"
        else:
            values[name] = found[0].value
    return values, reasons


def _render_statics(
    entries: list[StateStatic], is_cosmetic: Any, wanted: set[str] | None, seen: set[str]
) -> tuple[dict[str, Any], list[str]]:
    """`(보일 static, 가린 static 이름)`. 이름은 `Type.member` 짧은 꼴이고 holder 가 여럿이면 `@순번` 이 붙는다. stale 은 뺀다."""
    shown: dict[str, Any] = {}
    hidden: list[str] = []
    for entry in entries:
        observation = entry.observation
        type_key = entry.member_id.split("@", 1)[0]
        if observation.status not in ("KNOWN", "UNREAD") or is_cosmetic(type_key):
            continue
        short = _short_name(type_key)
        suffix = "@" + entry.member_id.split("@", 1)[1] if "@" in entry.member_id else ""
        names = {short, type_key}
        if wanted is not None and not names & wanted:
            hidden.append(short + suffix)
            continue
        if wanted is not None:
            seen.update(names & wanted)
        shown[short + suffix] = clip_value(_observed(observation))
    return {name: shown[name] for name in sorted(shown)}, sorted(hidden)


def _fit_within(state: dict[str, Any], limit: int) -> int:
    """`context._fit` 과 같은 규칙에 상한만 다르다: 목록 안의 객체를 뒤에서부터 하나씩 뺀다."""
    clipped = 0
    lists = [value for value in state.values() if isinstance(value, list)]
    while len(json.dumps(state, ensure_ascii=False, default=str)) > limit:
        non_empty = [objects for objects in lists if objects]
        if not non_empty:
            break
        non_empty[-1].pop()
        clipped += 1
    return clipped


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


# 근거 값을 그릴 때의 상한(글자). 범위가 planner 의 ```check 라 작지만, path 만 적으면 그 객체의 멤버가 전부 실린다.
EVIDENCE_STATE_CHARS = 20_000
# 근거를 읽는 범위. Jev 의 범위와 달리 비활성·stale 객체도 받는다 — 근거에서는 "없다" 와 "꺼져 있다", "마지막 whole 에
# 다시 안 실렸다" 가 다른 사실이다(STATE_SET 계약 50·52 행).
EVIDENCE_SPEC: dict[str, Any] = {**EXECUTER_SPEC, "includeInactive": True, "includeStale": True}
# 근거에 적는 객체·값의 상태. 값 대신 이 글이 들어간다.
PRESENT = "(present)"
INACTIVE = "(inactive)"
STALE = "(stale)"
UNKNOWN = "(unknown)"


@dataclass(frozen=True)
class EvidenceReading:
    """근거 한 번 읽기. `values` 는 이름마다 값이나 상태 글이다. `clipped` 는 상한에 걸려 빠진 객체 수, `omitted` 는 서버가
    `maxEntities` 로 뺀 수다. 둘 중 하나라도 있으면 이 읽기는 완전하지 않아, 빠진 이름을 부재로 읽으면 안 된다."""

    values: dict[str, Any]
    unknown_members: list[str]
    clipped: int = 0
    omitted: int = 0

    @property
    def complete(self) -> bool:
        return self.clipped == 0 and self.omitted == 0


def evidence_values(
    snapshot: StateSnapshot,
    view: StateView,
    is_cosmetic: Any = lambda _type_key: False,
    scene: str | None = None,
) -> EvidenceReading:
    """Evaluator 가 판정 근거로 읽을 값을 한 줄씩 펼친다. `EVIDENCE_SPEC` 으로 읽은 snapshot 을 받는다.

    범위는 Jev 의 범위와 같은 규칙으로 거른다(path 접두어, `Type.member` 이름). 객체는 `path` 에 `(present)`·`(inactive)`·
    `(stale)`, 글자는 `path text`, 멤버는 `path Type.member`, static 은 `static Type.member` 이름으로 적는다. 값은 STATE_SET
    의 상태를 그대로 따른다: `KNOWN` 은 값, `UNREAD` 는 `(unread: 이유)`, `UNKNOWN` 은 `(unknown)`, `STALE` 은 `(stale)`.
    path 없이 멤버 이름만 적었으면 그 멤버를 가진 객체만 남긴다.
    """
    active = (scene or snapshot.scene or "").strip()
    wanted = set(view.members) if view.members is not None else None
    seen: set[str] = set()
    entities = [entity for entity in snapshot.entities if view.paths is None or _under(entity, view.paths)]
    entities.sort(key=lambda entity: (entity.scene != active, entity.address))
    limit = view.limit or EVIDENCE_STATE_CHARS
    values: dict[str, Any] = {}
    clipped = 0
    for entity in entities:
        rows, members = _evidence_entity(entity, is_cosmetic, wanted, seen)
        if view.paths is None and members == 0:
            # 멤버 이름만 적었으면 그 멤버를 가진 객체만 근거다. 나머지 객체를 다 적으면 근거가 묻힌다.
            continue
        if len(json.dumps({**values, **rows}, ensure_ascii=False, default=str)) > limit:
            clipped += 1
            continue
        values.update(rows)
    for entry in snapshot.statics:
        type_key = entry.member_id.split("@", 1)[0]
        if is_cosmetic(type_key):
            continue
        short = _short_name(type_key)
        names = {short, type_key}
        if wanted is not None and not names & wanted:
            continue
        if wanted is not None:
            seen.update(names & wanted)
        suffix = "@" + entry.member_id.split("@", 1)[1] if "@" in entry.member_id else ""
        values[f"static {short}{suffix}"] = _evidence_value(entry.observation)
    unknown = sorted(name for name in wanted if name not in seen) if wanted else []
    return EvidenceReading(values, unknown, clipped=clipped, omitted=snapshot.omitted_entities)


def _evidence_entity(
    entity: StateEntity, is_cosmetic: Any, wanted: set[str] | None, seen: set[str]
) -> tuple[dict[str, Any], int]:
    """`(rows, 맞은 멤버 수)`."""
    path = entity.selector or entity.path or entity.entity_id
    presence = STALE if entity.status == "STALE" else PRESENT if entity.active else INACTIVE
    rows: dict[str, Any] = {path: presence}
    members = 0
    text = entity.fragments.get("text")
    if text is not None and text.status == "KNOWN" and text.value is not None:
        rows[f"{path} text"] = clip_value(text.value)
    for member_id, observation in entity.members.items():
        type_key = member_id.rsplit("#", 1)[0]
        if is_cosmetic(type_key):
            continue
        short = _short_name(member_id)
        names = {short, type_key}
        if wanted is not None and not names & wanted:
            continue
        if wanted is not None:
            seen.update(names & wanted)
        among = member_id.rsplit("#", 1)[1] if "#" in member_id else "0"
        name = f"{path} {short}" if among == "0" else f"{path} {short}#{among}"
        rows[name] = _evidence_value(observation)
        members += 1
    return rows, members


def _evidence_value(observation: StateObservation) -> Any:
    if observation.status == "KNOWN":
        return clip_value(observation.value)
    if observation.status == "UNREAD":
        return f"(unread: {observation.reason})"
    if observation.status == "STALE":
        return STALE
    return UNKNOWN


# step 이 시작한 뒤 바뀐 값을 evaluator 에게 줄 때의 줄 수 상한. planner 가 이름 붙이지 않은 변화라 넉넉히 두지 않는다.
MAX_STATE_CHANGES = 40


def state_changes(
    start: StateSnapshot, end: StateSnapshot, is_cosmetic: Any = lambda _type_key: False
) -> tuple[list[str], int]:
    """`(바뀐 것 줄, 상한에 걸려 뺀 수)`. 두 snapshot(`EVIDENCE_SPEC`)의 객체와 static 을 비교해, 생기거나 사라지거나 꺼지거나
    켜진 객체와 값이 바뀐 멤버·static 을 한 줄씩 적는다. planner 의 ```check 와 상관없는 근거다(개선 계획 묶음 3) — check
    이름이 틀려도 evaluator 가 게임이 실제로 무엇을 했는지 본다. cosmetic 타입(애니메이션 등)은 뺀다.
    """
    lines: list[str] = []

    def described(entity: StateEntity) -> str:
        return entity.selector or entity.path or entity.entity_id

    before = {entity.entity_id: entity for entity in start.entities}
    after = {entity.entity_id: entity for entity in end.entities}
    for entity_id, entity in after.items():
        old = before.get(entity_id)
        if old is None:
            lines.append(f"{described(entity)}: appeared")
            continue
        if old.active != entity.active:
            lines.append(f"{described(entity)}: {'switched on' if entity.active else 'switched off'}")
        for member_id, observation in entity.members.items():
            type_key = member_id.rsplit("#", 1)[0]
            if is_cosmetic(type_key) or observation.status != "KNOWN":
                continue
            previous = old.members.get(member_id)
            if previous is not None and previous.status == "KNOWN" and previous.value != observation.value:
                lines.append(
                    f"{described(entity)} {_short_name(member_id)}: "
                    f"{json.dumps(clip_value(previous.value), ensure_ascii=False, default=str)} → "
                    f"{json.dumps(clip_value(observation.value), ensure_ascii=False, default=str)}"
                )
    complete = end.omitted_entities == 0 and start.omitted_entities == 0
    for entity_id, entity in before.items():
        if entity_id not in after:
            lines.append(f"{described(entity)}: {'gone' if complete else 'not read at the end'}")
    old_statics = {entry.member_id: entry.observation for entry in start.statics}
    for entry in end.statics:
        type_key = entry.member_id.split("@", 1)[0]
        previous = old_statics.get(entry.member_id)
        if is_cosmetic(type_key) or previous is None or entry.observation.status != "KNOWN" or previous.status != "KNOWN":
            continue
        if previous.value != entry.observation.value:
            lines.append(
                f"static {_short_name(type_key)}: "
                f"{json.dumps(clip_value(previous.value), ensure_ascii=False, default=str)} → "
                f"{json.dumps(clip_value(entry.observation.value), ensure_ascii=False, default=str)}"
            )
    return lines[:MAX_STATE_CHANGES], max(len(lines) - MAX_STATE_CHANGES, 0)
