"""Typed output contracts for the v2 evidence discovery prototype."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Resolution = Literal["exact", "derived", "ambiguous", "unresolved"]
Quality = Literal["ready", "candidate", "review", "unsupported"]


@dataclass(frozen=True)
class SourceRef:
    record_id: str
    entry_id: str
    method_id: str
    offset: int | None = None


@dataclass(frozen=True)
class ProofEdge:
    source: str
    relation: str
    target: str
    resolution: Resolution
    rule: str


@dataclass(frozen=True)
class Trigger:
    kind: str
    scene: str | None
    label: str
    target: str | None
    event: str | None
    resolution: Resolution
    proof: tuple[ProofEdge, ...] = ()
    target_label: str | None = None
    target_sprite: str | None = None
    input_kind: str | None = None

    @property
    def identity(self) -> tuple[str, str | None, str | None, str | None]:
        return self.kind, self.scene, self.target, self.event


@dataclass(frozen=True)
class Assertion:
    kind: str
    target: str | None
    operation: str
    value: Any
    category: str
    resolution: Resolution
    observable_by: str
    source: SourceRef
    proof: tuple[ProofEdge, ...] = ()
    target_label: str | None = None
    target_sprite: str | None = None

    @property
    def identity(self) -> tuple[Any, ...]:
        value = tuple(self.value) if isinstance(self.value, list) else self.value
        return self.kind, self.target, self.operation, value


@dataclass(frozen=True)
class SupportingState:
    target: str | None
    operation: str
    value: Any
    source: SourceRef


@dataclass
class Contract:
    id: str
    capture: str
    scene: str | None
    trigger: Trigger
    condition: dict[str, Any]
    assertions: list[Assertion]
    supporting_state: list[SupportingState]
    call_path: tuple[str, ...]
    source_refs: list[SourceRef]
    quality: Quality
    actionability: str
    observability: str
    applicability: str
    issues: list[str] = field(default_factory=list)
    folded_path: bool = False

    @property
    def feature_key(self) -> tuple[Any, ...]:
        first_hop = self.call_path[1] if len(self.call_path) > 1 else self.call_path[0]
        return self.trigger.identity, first_hop


@dataclass
class BranchFamily:
    id: str
    scene: str | None
    trigger: Trigger
    feature: str
    arms: list[str]
    condition_kinds: list[str]
    quality: Quality
    issues: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    id: str
    scene: str | None
    trigger: Trigger
    title: str
    contracts: list[str]
    assertions: list[Assertion]
    supporting_state: list[SupportingState]
    call_paths: list[tuple[str, ...]]
    quality: Quality
    actionability: str
    observability: str
    applicability: str
    issues: list[str] = field(default_factory=list)


@dataclass
class DiscoveryResult:
    source: str
    capture: str
    build: dict[str, Any]
    scenes: list[str]
    graph_stats: dict[str, Any]
    contracts: list[Contract]
    branch_families: list[BranchFamily]
    scenarios: list[Scenario]
    coverage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
