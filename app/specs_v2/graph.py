"""Load one SDK report into a small, typed evidence graph.

The graph mirrors relationships the report actually carries.  It deliberately
does not recreate IL basic blocks or infer edges from similar names.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .model import ProofEdge, Resolution, SourceRef


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "\x1f".join(json.dumps(part, ensure_ascii=False, sort_keys=True) for part in parts)
    return f"{prefix}:{hashlib.sha1(raw.encode()).hexdigest()[:12]}"


def type_leaf(name: str | None) -> str:
    return (name or "").rsplit(".", 1)[-1]


def member_from_id(member_id: str | None) -> tuple[str, str]:
    parts = (member_id or "").split("|")
    return (parts[1], parts[2]) if len(parts) >= 3 else ("", "")


def member_from_signature(signature: str | None) -> tuple[str, str]:
    head, sep, tail = (signature or "").partition("::")
    if not sep:
        return "", ""
    declaring = head.rsplit(" ", 1)[-1]
    return declaring, tail.split("(", 1)[0]


def condition_signature(condition: dict[str, Any]) -> str:
    return json.dumps(condition or {"kind": "always"}, ensure_ascii=False, sort_keys=True)


def condition_leaves(condition: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if condition.get("kind") in {"every", "either"}:
        for part in condition.get("parts") or ():
            yield from condition_leaves(part)
    else:
        yield condition


@dataclass(frozen=True)
class ControlFact:
    id: str
    object_id: str
    scene: str | None
    path: str
    selector: str | None
    active: bool | None
    label: str | None
    sprite: str | None
    target_type: str
    target_method: str
    event: str


@dataclass(frozen=True)
class PlacementFact:
    id: str
    scene: str | None
    path: str
    selector: str | None
    component_type: str
    persistent: bool


@dataclass(frozen=True)
class RefFact:
    id: str
    scene: str | None
    owner_type: str
    field: str
    name: str | None
    path: str | None
    asset: bool
    carries: tuple[str, ...]


@dataclass(frozen=True)
class PathFact:
    id: str
    record_id: str
    owner: str
    origin: str
    entry_id: str
    entry_type: str
    entry_method: str
    method_id: str
    source_signature: str
    call_path: tuple[str, ...]
    condition: dict[str, Any]
    inputs: tuple[dict[str, Any], ...]
    effects: tuple[dict[str, Any], ...]
    calls: tuple[dict[str, Any], ...]
    gaps: tuple[str, ...]
    confidence: str
    created_by: tuple[str, ...]
    called_by: tuple[str, ...]
    folded: bool
    handed_over_at: int | None
    handed_over_to: str | None

    def source_ref(self, offset: int | None = None) -> SourceRef:
        return SourceRef(self.id, self.entry_id, self.method_id, offset)


@dataclass
class EvidenceGraph:
    source: str
    report: dict[str, Any]
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[ProofEdge] = field(default_factory=list)
    paths: list[PathFact] = field(default_factory=list)
    controls: list[ControlFact] = field(default_factory=list)
    placements: list[PlacementFact] = field(default_factory=list)
    refs: list[RefFact] = field(default_factory=list)
    controls_by_handler: dict[tuple[str, str], list[ControlFact]] = field(
        default_factory=lambda: defaultdict(list)
    )
    placements_by_type: dict[str, list[PlacementFact]] = field(
        default_factory=lambda: defaultdict(list)
    )
    refs_by_field: dict[tuple[str, str], list[RefFact]] = field(
        default_factory=lambda: defaultdict(list)
    )
    object_visuals: dict[
        tuple[str | None, str], tuple[str | None, str | None]
    ] = field(default_factory=dict)

    def add_node(self, node_id: str, node_kind: str, **data: Any) -> None:
        self.nodes.setdefault(node_id, {"kind": node_kind, **data})

    def add_edge(
        self,
        source: str,
        relation: str,
        target: str,
        resolution: Resolution = "exact",
        rule: str = "sdk-field",
    ) -> ProofEdge:
        edge = ProofEdge(source, relation, target, resolution, rule)
        if edge not in self.edges:
            self.edges.append(edge)
        return edge

    @property
    def capture(self) -> str:
        return self.report.get("capture") or "unknown"

    @property
    def build(self) -> dict[str, Any]:
        return self.report.get("build") or {}

    def stats(self) -> dict[str, Any]:
        kinds = Counter(node["kind"] for node in self.nodes.values())
        relations = Counter(edge.relation for edge in self.edges)
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "paths": len(self.paths),
            "node_kinds": dict(sorted(kinds.items())),
            "relations": dict(sorted(relations.items())),
        }

    def placements_for(self, type_name: str, scene: str | None = None) -> list[PlacementFact]:
        candidates = self.placements_by_type.get(type_name, [])
        if not candidates:
            candidates = self.placements_by_type.get(type_leaf(type_name), [])
        if scene is not None:
            same_scene = [item for item in candidates if item.scene == scene]
            if same_scene:
                return same_scene
        return list(candidates)

    def refs_for(self, owner: str, field_name: str, scene: str | None) -> list[RefFact]:
        candidates = self.refs_by_field.get((owner, field_name), [])
        if not candidates:
            candidates = self.refs_by_field.get((type_leaf(owner), field_name), [])
        if scene is not None:
            same_scene = [item for item in candidates if item.scene == scene]
            if same_scene:
                return same_scene
        return list(candidates)

    def visual_for(
        self,
        path: str | None,
        scene: str | None,
    ) -> tuple[str | None, str | None]:
        """Return visual metadata only when one captured object is identifiable."""

        if not path:
            return None, None
        exact = self.object_visuals.get((scene, path))
        if exact is not None:
            return exact
        candidates = {
            visual
            for (candidate_scene, candidate_path), visual in self.object_visuals.items()
            if candidate_path == path
            and (scene is None or candidate_scene == scene)
        }
        return next(iter(candidates)) if len(candidates) == 1 else (None, None)

    def resolve_created_scene(self, created_by: tuple[str, ...]) -> tuple[str | None, list[ProofEdge]]:
        scenes: dict[str, list[ProofEdge]] = defaultdict(list)
        for creator in created_by:
            declaring, _, _field = creator.rpartition(".")
            for placement in self.placements_for(declaring):
                scenes[placement.scene or ""].append(
                    ProofEdge(placement.id, "creates", creator, "derived", "createdBy-owner-placement")
                )
        if len(scenes) == 1:
            named = next(iter(scenes))
            return named or None, scenes[named]
        return None, [edge for group in scenes.values() for edge in group]


def _visual_name(obj: dict[str, Any]) -> tuple[str | None, str | None]:
    caption = None
    sprite = obj.get("sprite")
    for visual in obj.get("visuals") or ():
        if visual.get("from") != obj.get("path"):
            continue
        if visual.get("role") == "control-caption":
            caption = visual.get("value")
        elif visual.get("role") == "sprite" and not sprite:
            sprite = visual.get("value")
    return caption or obj.get("label"), sprite


def _load_objects(graph: EvidenceGraph) -> None:
    for scene in graph.report.get("scenes") or ():
        scene_id = f"scene:{scene}"
        graph.add_node(scene_id, "scene", name=scene)

    groups = [
        (obj, False) for obj in graph.report.get("objects") or ()
    ] + [(obj, True) for obj in graph.report.get("persistentObjects") or ()]
    for obj, persistent in groups:
        scene = obj.get("scene")
        object_id = stable_id("object", scene, obj.get("selector") or obj.get("path"))
        graph.add_node(
            object_id,
            "object",
            scene=scene,
            path=obj.get("path"),
            selector=obj.get("selector"),
            active=obj.get("active"),
            persistent=persistent,
        )
        if scene:
            graph.add_edge(f"scene:{scene}", "contains", object_id)

        label, sprite = _visual_name(obj)
        if obj.get("path"):
            graph.object_visuals[(scene, obj["path"])] = (label, sprite)
        graph.nodes[object_id].update(label=label, sprite=sprite)
        for index, visual in enumerate(obj.get("visuals") or ()):
            visual_id = stable_id("visual", object_id, index, visual)
            graph.add_node(visual_id, "visual", **visual)
            graph.add_edge(object_id, "observed_as", visual_id)

        for index, component in enumerate(obj.get("components") or ()):
            component_type = component.get("type") or ""
            component_id = stable_id("component", object_id, index, component_type)
            graph.add_node(component_id, "component", type=component_type)
            graph.add_edge(object_id, "contains", component_id)
            placement = PlacementFact(
                component_id,
                scene,
                obj.get("path") or "",
                obj.get("selector"),
                component_type,
                persistent,
            )
            graph.placements.append(placement)
            for key in {component_type, type_leaf(component_type)}:
                graph.placements_by_type[key].append(placement)

            for call_index, call in enumerate(component.get("calls") or ()):
                control_id = stable_id("control", object_id, call_index, call)
                control = ControlFact(
                    control_id,
                    object_id,
                    scene,
                    obj.get("path") or "",
                    obj.get("selector"),
                    obj.get("active") if isinstance(obj.get("active"), bool) else None,
                    label,
                    sprite,
                    call.get("targetType") or "",
                    call.get("method") or "",
                    call.get("event") or "unity-event",
                )
                graph.controls.append(control)
                graph.add_node(control_id, "control", scene=scene, path=control.path, event=control.event)
                handler_id = f"method:{control.target_type}|{control.target_method}"
                graph.add_node(handler_id, "method", type=control.target_type, method=control.target_method)
                graph.add_edge(control_id, "wired_to", handler_id)
                for key in {
                    (control.target_type, control.target_method),
                    (type_leaf(control.target_type), control.target_method),
                }:
                    graph.controls_by_handler[key].append(control)

            for ref_index, ref in enumerate(component.get("refs") or ()):
                ref_id = stable_id("ref", component_id, ref_index, ref)
                fact = RefFact(
                    ref_id,
                    scene,
                    component_type,
                    ref.get("field") or "",
                    ref.get("name"),
                    ref.get("path"),
                    bool(ref.get("asset")),
                    tuple(ref.get("carries") or ()),
                )
                graph.refs.append(fact)
                graph.add_node(ref_id, "reference", field=fact.field, name=fact.name, path=fact.path)
                graph.add_edge(component_id, "references", ref_id)
                for key in {
                    (component_type, fact.field),
                    (type_leaf(component_type), fact.field),
                }:
                    graph.refs_by_field[key].append(fact)
                for carried in fact.carries:
                    carried_id = f"type:{carried}"
                    graph.add_node(carried_id, "type", name=carried)
                    graph.add_edge(ref_id, "carries", carried_id)


def _record_groups(report: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any], str, tuple[str, ...]]]:
    for owner, records in (report.get("types") or {}).items():
        for record in records:
            yield owner, record, "placed", ()
    for owner, group in (report.get("unplaced") or {}).items():
        created_by = tuple(group.get("createdBy") or ())
        for record in group.get("evidence") or ():
            yield owner, record, "unplaced", created_by


def _load_records(graph: EvidenceGraph) -> None:
    for owner, record, origin, created_by in _record_groups(graph.report):
        source_signature = record.get("source") or record.get("entry") or ""
        method_id = record.get("methodId") or source_signature
        fingerprint = (
            owner,
            record.get("entryId"),
            method_id,
            record.get("condition"),
            record.get("effects"),
            record.get("calls"),
        )
        record_id = stable_id("record", *fingerprint)
        graph.add_node(record_id, "record", owner=owner, origin=origin, confidence=record.get("confidence"))

        variants = [
            (
                record.get("entryId") or record.get("entry") or "",
                record.get("entry") or "",
                tuple(record.get("callPath") or (record.get("entry") or "",)),
                False,
            )
        ]
        variants.extend(
            (
                also.get("entryId") or also.get("entry") or "",
                also.get("entry") or "",
                tuple(also.get("callPath") or (also.get("entry") or "",)),
                True,
            )
            for also in record.get("alsoReachedBy") or ()
        )

        for entry_id, entry_signature, call_path, folded in variants:
            entry_type, entry_method = member_from_id(entry_id)
            if not entry_type:
                entry_type, entry_method = member_from_signature(entry_signature)
            path_id = stable_id("path", record_id, entry_id, call_path)
            path = PathFact(
                path_id,
                record_id,
                owner,
                origin,
                entry_id,
                entry_type,
                entry_method,
                method_id,
                source_signature,
                call_path,
                record.get("condition") or {"kind": "always"},
                tuple(record.get("inputs") or ()),
                tuple(record.get("effects") or ()),
                tuple(record.get("calls") or ()),
                tuple(record.get("gaps") or ()),
                record.get("confidence") or "partial",
                created_by,
                tuple(record.get("calledBy") or ()),
                folded,
                record.get("handedOverAt"),
                record.get("handedOverTo"),
            )
            graph.paths.append(path)
            graph.add_node(path_id, "path", entry_id=entry_id, folded=folded)
            graph.add_edge(record_id, "reached_by", path_id, "derived" if folded else "exact", "alsoReachedBy" if folded else "entry")

            previous = None
            for signature in call_path:
                method_node = f"signature:{signature}"
                declaring, method = member_from_signature(signature)
                graph.add_node(method_node, "method", signature=signature, type=declaring, method=method)
                if previous is None:
                    graph.add_edge(path_id, "starts_at", method_node)
                else:
                    graph.add_edge(previous, "calls", method_node, "exact", "callPath")
                previous = method_node

            condition_id = stable_id("condition", condition_signature(path.condition))
            graph.add_node(condition_id, "condition", value=path.condition)
            graph.add_edge(path_id, "guarded_by", condition_id)

            for index, effect in enumerate(path.effects):
                effect_id = stable_id("effect", path_id, index, effect)
                graph.add_node(effect_id, "effect", **effect)
                graph.add_edge(path_id, "produces", effect_id)
            for index, call in enumerate(path.calls):
                call_id = stable_id("call", path_id, index, call)
                graph.add_node(call_id, "call", **call)
                graph.add_edge(path_id, "contains_call", call_id)
                target = call.get("targetId") or call.get("target")
                if target:
                    target_id = f"method-id:{target}"
                    graph.add_node(target_id, "method", member_id=target)
                    graph.add_edge(call_id, "calls", target_id)


def graph_from_report(
    report: dict[str, Any],
    *,
    source: str = "sdk-report",
) -> EvidenceGraph:
    """Build a graph from one in-memory SDK report.

    The internal API uses this path so a request remains stateless and never
    needs to materialize a caller payload on disk.
    """

    if report.get("schema") not in {5, 6}:
        raise ValueError(f"unsupported SDK schema: {report.get('schema')}")
    graph = EvidenceGraph(source=source, report=report)
    _load_objects(graph)
    _load_records(graph)
    return graph


def load_graph(path: str | Path) -> EvidenceGraph:
    source = str(Path(path).resolve())
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    return graph_from_report(report, source=source)


_LIST_ITEM = re.compile(r"\.Item\[[^\]]*\]")


def target_parts(target: str | None) -> tuple[str, str] | None:
    """Return the conservative `Component.field` prefix of an SDK target."""
    if not target:
        return None
    cleaned = _LIST_ITEM.sub("", target)
    first, dot, rest = cleaned.partition(".")
    if not dot or not first or not first[0].isupper():
        return None
    return first, rest.split(".", 1)[0]
