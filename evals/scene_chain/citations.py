"""에이전트가 뱉은 인용을 기계가 대조한다.

이 하네스의 전부가 여기에 있다. 체인을 잇는 것 자체는 식별자가 원문 그대로 남아 있어
문자열 대조에 가깝다 — 어려운 것은 **대지 못할 근거를 댄 것**을 골라내는 일이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from evals.scene_chain.evidence import Capture, ContentMap, names_a_state


class Role(StrEnum):
    writes = "writes"
    reads = "reads"


class Verdict(StrEnum):
    #: 되찾은 기능 행이 `via` 를 실제로 갖는다.
    in_map = "in-map"
    #: 맵에는 없고 캡처 레코드에만 있다. 근거를 댄 것은 맞지만 content_map 이 버린 사실이다.
    in_capture = "in-capture"
    #: 어디에도 없다. 지어냄.
    unverified = "unverified"


@dataclass(frozen=True)
class Citation:
    capability_id: int | None
    unit: str | None
    role: Role
    via: str


@dataclass(frozen=True)
class CitationCheck:
    citation: Citation
    verdict: Verdict
    #: 인용이 가리키는 기능 행. 골든 적중 판정도 이 집합을 쓴다.
    capability_ids: frozenset[int]
    reason: str

    @property
    def passed(self) -> bool:
        return self.verdict is not Verdict.unverified


@dataclass(frozen=True)
class AgentChain:
    summary: str
    citations: tuple[Citation, ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class ChainCheck:
    chain: AgentChain
    checks: tuple[CitationCheck, ...]

    @property
    def names_one_state(self) -> bool:
        """체인 안의 인용이 전부 같은 상태를 가리키는가.

        인용 하나씩만 보면 `WaveEndSensor writes MapMove.StagePosition` 도
        `CharacterMove reads MapMove.position` 도 각자 참이다. 그 둘을 한 체인으로
        묶은 것이 바로 이름 기반 오분류인데, 인용별 대조로는 잡히지 않는다.
        체인 하나는 상태 하나라는 규칙을 프롬프트에도 적고 여기서도 지킨다.
        """
        vias = [check.citation.via for check in self.checks]
        head = vias[0] if vias else ""
        return all(names_a_state(via, head) for via in vias)

    @property
    def citations_hold(self) -> bool:
        """인용이 전부 대조를 통과했는가. 지어냄 비율은 이것만 센다."""
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def passed(self) -> bool:
        return self.citations_hold and self.names_one_state

    @property
    def fabricated(self) -> bool:
        """인용을 못 댄 것. 상태를 섞은 것은 여기 들어가지 않는다 — 다른 실패다."""
        return not self.citations_hold

    @property
    def mixed_state(self) -> bool:
        return self.citations_hold and not self.names_one_state

    def resolved(self, role: Role, state: str) -> frozenset[int]:
        """`state` 를 그 역할로 인용한 기능 행 전부. 골든 적중 판정이 쓴다."""
        found: set[int] = set()
        for check in self.checks:
            if check.citation.role is role and names_a_state(check.citation.via, state):
                found.update(check.capability_ids)
        return frozenset(found)


class MalformedOutput(ValueError):
    """스키마를 지키지 않은 응답. 모델의 실패이지 하네스의 실패가 아니다."""


def parse_chains(payload: str | dict[str, Any]) -> list[AgentChain]:
    """모델 응답을 체인 목록으로. 스키마를 어긴 체인은 `MalformedOutput` 을 낸다."""
    document = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(document, dict) or not isinstance(document.get("chains"), list):
        raise MalformedOutput("top-level object must carry a `chains` list")
    return [_read_chain(entry, index) for index, entry in enumerate(document["chains"])]


def _read_chain(entry: Any, index: int) -> AgentChain:
    if not isinstance(entry, dict) or not isinstance(entry.get("chain"), list):
        raise MalformedOutput(f"chains[{index}] must carry a `chain` list")
    if not entry["chain"]:
        raise MalformedOutput(f"chains[{index}].chain is empty")
    return AgentChain(
        summary=str(entry.get("summary") or ""),
        citations=tuple(_read_citation(item, index, at) for at, item in enumerate(entry["chain"])),
        raw=entry,
    )


def _read_citation(item: Any, chain_index: int, at: int) -> Citation:
    where = f"chains[{chain_index}].chain[{at}]"
    if not isinstance(item, dict):
        raise MalformedOutput(f"{where} must be an object")
    role = item.get("role")
    if role not in tuple(Role):
        raise MalformedOutput(f"{where}.role must be one of {[r.value for r in Role]}")
    via = item.get("via")
    if not isinstance(via, str) or not via.strip():
        raise MalformedOutput(f"{where}.via must be a non-empty string")
    capability_id = item.get("capabilityId")
    if capability_id is not None and not isinstance(capability_id, int):
        raise MalformedOutput(f"{where}.capabilityId must be an integer or null")
    unit = item.get("unit")
    if unit is not None and not isinstance(unit, str):
        raise MalformedOutput(f"{where}.unit must be a string or null")
    return Citation(capability_id, unit or None, Role(role), via.strip())


def resolve(citation: Citation, content_map: ContentMap) -> frozenset[int]:
    """인용이 가리키는 기능 행. 인용 검증과 골든 적중 판정이 함께 쓰는 유일한 원시 연산.

    없는 `capabilityId` 는 빈 집합이다 — `unit` 으로 구제하지 않는다. 실재하지 않는 행을
    댄 것은 그 자체로 지어냄이고, 다른 칸이 우연히 맞는다고 근거가 되지는 않는다.
    """
    if citation.capability_id is not None:
        row = content_map.by_id.get(citation.capability_id)
        return frozenset({row.capability_id}) if row else frozenset()
    if citation.unit:
        return frozenset(row.capability_id for row in content_map.matching_unit(citation.unit))
    return frozenset()


def verify(citation: Citation, content_map: ContentMap, capture: Capture | None) -> CitationCheck:
    capability_ids = resolve(citation, content_map)
    for capability_id in sorted(capability_ids):
        row = content_map.by_id[capability_id]
        terms = row.writes if citation.role is Role.writes else row.reads
        if any(names_a_state(term, citation.via) for term in terms):
            return CitationCheck(citation, Verdict.in_map, capability_ids, "content_map 의 그 행에 있다")

    # 없는 id 를 댄 인용은 캡처 단으로 넘기지 않는다. `unit` 이 우연히 진짜 메서드를
    # 가리켜도 실재하지 않는 행을 댄 것은 그 자체로 지어냄이고, arm (c) 는 두 칸을 모두
    # 채우라고 지시받으므로 여기서 새면 지어냄이 통째로 안 세어진다.
    invented_id = citation.capability_id is not None and not capability_ids
    if capture is not None and not invented_id:
        unit = citation.unit or _unit_of(capability_ids, content_map)
        record = capture.matching_unit(unit) if unit else None
        if record is not None:
            terms = record.writes if citation.role is Role.writes else record.reads
            if any(names_a_state(term, citation.via) for term in terms):
                return CitationCheck(
                    citation,
                    Verdict.in_capture,
                    capability_ids,
                    "content_map 에는 없고 캡처 레코드에 있다",
                )

    reason = (
        "인용한 기능 행이 실재하지 않는다"
        if not capability_ids
        else "그 행의 effect/condition 어디에도 via 가 없다"
    )
    return CitationCheck(citation, Verdict.unverified, capability_ids, reason)


def _unit_of(capability_ids: frozenset[int], content_map: ContentMap) -> str | None:
    units = {content_map.by_id[key].unit for key in capability_ids}
    return units.pop() if len(units) == 1 else None


def check_chain(
    chain: AgentChain, content_map: ContentMap, capture: Capture | None
) -> ChainCheck:
    return ChainCheck(chain, tuple(verify(c, content_map, capture) for c in chain.citations))
