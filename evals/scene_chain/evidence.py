"""근거 두 벌을 같은 어휘로 읽는다 — content_map 의 기능 행과, 캡처의 레코드.

두 벌이 필요한 이유는 둘이 같은 것을 담고 있지 않기 때문이다. 의사 C# 렌더는 캡처에서
나오므로 content_map 이 버린 사실을 들고 있다(예: `GameClearController.ShowGettedCard` 가
`StageDataSingleton.stagePosition` 을 읽는다). content_map 하나로만 인용을 대조하면 그
사실을 인용한 arm 이 "지어냄"으로 떨어진다 — 실제로는 근거를 댄 것인데도.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# 점이 하나 이상 들어간 식별자만 상태로 본다. `stagePosition` 같은 맨 이름은 어느 타입의
# 것인지 정해지지 않아 인용의 대상이 될 수 없다 — 이름만 같고 다른 것을 잇는 실패가
# 바로 그 자리에서 생긴다.
DOTTED_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")

# 'System.Void Map.MapMove::CharacterMove()' 에서 'Map.MapMove' 와 'CharacterMove'.
SIGNATURE = re.compile(r"^[^ ]+ (?P<decl>[^:]+)::(?P<name>[^(]+)\(")
# 컴파일러가 만든 코루틴 상태 기계: 'Map.MapMove/<WaveEndSensor>d__6' -> 'WaveEndSensor'.
STATE_MACHINE = re.compile(r"^(?P<owner>[^/]+)/<(?P<outer>[^>]+)>d__\d+$")


def dotted_identifiers(value: Any) -> set[str]:
    """중첩된 dict/list/str 어디에 있든 점 찍힌 식별자를 전부 모은다."""
    found: set[str] = set()
    _collect(value, found)
    return found


def _collect(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _collect(item, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect(item, found)
    elif isinstance(value, str):
        found.update(DOTTED_IDENTIFIER.findall(value))


def names_a_state(term: str, state: str) -> bool:
    """`term` 이 `state` 를 가리키는가.

    정확히 같거나, `state` 의 멤버다(`CombineButton.combineZone` 을 쓴 것과
    `CombineButton.combineZone.activeSelf` 를 읽은 것은 같은 상태다). 반대 방향
    — 더 좁은 것을 쓰고 더 넓은 것을 읽은 것 — 도 같은 상태로 본다.
    """
    return term == state or term.startswith(state + ".") or state.startswith(term + ".")


@dataclass(frozen=True)
class Capability:
    """content_map 의 기능 행 하나."""

    capability_id: int
    scene: str
    owner_type: str
    method: str
    writes: frozenset[str]
    reads: frozenset[str]

    @property
    def unit(self) -> str:
        return f"{self.owner_type}.{self.method}"


class ContentMap:
    """기능 행을 id 로도 `Owner.Method` 로도 찾을 수 있게 색인한 content_map."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.by_id: dict[int, Capability] = {}
        self._by_unit: dict[str, list[Capability]] = {}
        for scene in document.get("scenes", []):
            for row in scene.get("capabilities", []):
                capability = _read_capability(row, scene.get("name", ""))
                self.by_id[capability.capability_id] = capability
                key = unit_key(capability.owner_type, capability.method)
                if key:
                    self._by_unit.setdefault(key, []).append(capability)

    @classmethod
    def load(cls, path: Path) -> "ContentMap":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def __len__(self) -> int:
        return len(self.by_id)

    @property
    def capabilities(self) -> list[Capability]:
        return [self.by_id[key] for key in sorted(self.by_id)]

    def matching_unit(self, unit: str) -> list[Capability]:
        """`Map.MapMove.CharacterMove` 또는 `MapMove.CharacterMove` 로 찾는다.

        한 메서드가 여러 기능 행으로 갈린 곳이 있으므로 후보가 여럿일 수 있다.
        """
        return list(self._by_unit.get(normalize_unit(unit), []))


def _read_capability(row: dict[str, Any], scene: str) -> Capability:
    evidence = row.get("evidence") or {}
    return Capability(
        capability_id=int(row["capabilityId"]),
        scene=scene,
        owner_type=evidence.get("ownerType") or "",
        method=_plain_method(evidence.get("method") or ""),
        writes=frozenset(
            effect["target"] for effect in row.get("then", []) if effect.get("target")
        ),
        reads=frozenset(dotted_identifiers(row.get("given"))),
    )


def _plain_method(method: str) -> str:
    """`<WaveEndSensor>d__6::MoveNext` 같은 상태 기계 이름을 원래 메서드로 되돌린다."""
    match = re.match(r"^<(?P<outer>[^>]+)>d__\d+::MoveNext$", method)
    return match.group("outer") if match else method


def normalize_unit(unit: str) -> str:
    """네임스페이스를 떼어 `MapMove.CharacterMove` 한 가지 모양으로 만든다."""
    parts = [part for part in unit.replace("::", ".").split(".") if part]
    return ".".join(parts[-2:]) if len(parts) >= 2 else unit


def unit_key(owner_type: str, method: str) -> str | None:
    if not owner_type or not method:
        return None
    return normalize_unit(f"{owner_type}.{method}")


@dataclass(frozen=True)
class CaptureUnit:
    """캡처가 한 메서드에 대해 관측한 것 전부를 모은 것."""

    unit: str
    writes: frozenset[str]
    reads: frozenset[str]


class Capture:
    """`Owner.Method` 로 색인한 근거 캡처.

    content_map 이 답을 못 하는 인용만 여기로 온다. 기능 행이 아니라 관측 레코드이므로
    `capabilityId` 도 `status` 도 없다 — 있는지 없는지만 답할 수 있다.
    """

    def __init__(self, document: dict[str, Any]) -> None:
        writes: dict[str, set[str]] = {}
        reads: dict[str, set[str]] = {}
        for record in _capture_records(document):
            unit = _record_unit(record)
            if unit is None:
                continue
            writes.setdefault(unit, set()).update(
                effect["target"] for effect in record.get("effects", []) if effect.get("target")
            )
            reads.setdefault(unit, set()).update(dotted_identifiers(record.get("condition")))
        self.by_unit = {
            unit: CaptureUnit(unit, frozenset(writes.get(unit, ())), frozenset(reads.get(unit, ())))
            for unit in set(writes) | set(reads)
        }

    @classmethod
    def load(cls, path: Path) -> "Capture":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def matching_unit(self, unit: str) -> CaptureUnit | None:
        return self.by_unit.get(normalize_unit(unit))


def _capture_records(document: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for records in document.get("types", {}).values():
        yield from records
    for blob in document.get("unplaced", {}).values():
        yield from blob.get("evidence", [])


def _record_unit(record: dict[str, Any]) -> str | None:
    signature = record.get("source") or record.get("entry")
    match = SIGNATURE.match(signature or "")
    if not match:
        return None
    declaring, name = match.group("decl"), match.group("name")
    machine = STATE_MACHINE.match(declaring)
    if machine:
        # 코루틴 본문은 `Owner/<M>d__N::MoveNext` 로 잡힌다. 사람이 인용할 이름은 `Owner.M` 이다.
        return normalize_unit(f"{machine.group('owner')}.{machine.group('outer')}")
    return normalize_unit(f"{declaring}.{name}")


@dataclass(frozen=True)
class JoinedPair:
    """기계가 문자열 대조만으로 닿는 (쓰는 기능, 읽는 기능, 상태)."""

    writer: int
    reader: int
    state: str

    @property
    def is_self_pair(self) -> bool:
        return self.writer == self.reader


def mechanical_join(content_map: ContentMap) -> list[JoinedPair]:
    """한 기능이 쓴 상태를 다른 기능이 읽는 모든 쌍.

    과소연결의 분모다 — 추론 없이 문자열만 맞춰도 닿는 곳을 에이전트가 못 이었다면
    그것은 근거 부족이 아니라 못 읽은 것이다.
    """
    pairs: list[JoinedPair] = []
    for writer in content_map.capabilities:
        for reader in content_map.capabilities:
            for state in sorted(writer.writes):
                if any(names_a_state(term, state) for term in reader.reads):
                    pairs.append(JoinedPair(writer.capability_id, reader.capability_id, state))
    return pairs


@dataclass(frozen=True)
class JoinedLink:
    """조인 쌍을 `(쓰는 메서드, 읽는 메서드, 상태)` 로 접은 것.

    과소연결의 분모는 쌍이 아니라 이것이다. `MapMove.CharacterMove` 하나가 기능 행 셋으로
    갈려 있어 쌍으로 세면 같은 사실이 여섯 번 세어지고, `unit` 으로 인용하는 arm 은 한 번에
    여섯을 덮는 반면 `capabilityId` 로 정확히 인용하는 arm 은 다섯 번 놓친 것으로 기록된다.
    거꾸로다.
    """

    writer_unit: str
    reader_unit: str
    state: str


def join_links(content_map: ContentMap, pairs: list[JoinedPair] | None = None) -> list[JoinedLink]:
    pairs = mechanical_join(content_map) if pairs is None else pairs
    links = {
        JoinedLink(
            content_map.by_id[pair.writer].unit,
            content_map.by_id[pair.reader].unit,
            pair.state,
        )
        for pair in pairs
    }
    return sorted(links, key=lambda link: (link.writer_unit, link.reader_unit, link.state))


def capability_ids_for_link(
    content_map: ContentMap, link: JoinedLink, pairs: list[JoinedPair]
) -> tuple[frozenset[int], frozenset[int]]:
    """그 링크를 낳은 기능 행들. 어느 행을 인용해도 그 링크를 덮은 것으로 센다."""

    def belongs(pair: JoinedPair) -> bool:
        return (
            content_map.by_id[pair.writer].unit == link.writer_unit
            and content_map.by_id[pair.reader].unit == link.reader_unit
            and pair.state == link.state
        )

    members = [pair for pair in pairs if belongs(pair)]
    return (
        frozenset(pair.writer for pair in members),
        frozenset(pair.reader for pair in members),
    )
