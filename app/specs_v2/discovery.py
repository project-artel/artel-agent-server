"""Discover atomic contracts and connected scenarios from an EvidenceGraph."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from . import answers, observable
from .answers import Answers
from .graph import (
    EvidenceGraph,
    PathFact,
    condition_leaves,
    condition_signature,
    member_from_signature,
    stable_id,
    target_parts,
    type_leaf,
)
from .model import (
    Assertion,
    BranchFamily,
    Contract,
    DiscoveryResult,
    ProofEdge,
    Quality,
    Scenario,
    SourceRef,
    SupportingState,
    Trigger,
)

ASSERTABLE = {"observable", "availability"}
BLOCKING_GAPS = {"unread-condition", "callee-condition-not-composed"}
POINTER_METHODS = {
    "OnMouseDown", "OnMouseUp", "OnMouseUpAsButton", "OnMouseEnter", "OnMouseExit",
    "OnPointerDown", "OnPointerUp", "OnPointerClick", "OnPointerEnter", "OnPointerExit",
    "OnBeginDrag", "OnDrag", "OnEndDrag", "OnDrop",
}
COLLISION_METHODS = {
    "OnTriggerEnter", "OnTriggerExit", "OnTriggerEnter2D", "OnTriggerExit2D",
    "OnCollisionEnter", "OnCollisionExit", "OnCollisionEnter2D", "OnCollisionExit2D",
}
ENTRY_METHODS = {"Start", "Awake", "OnEnable"}
CONTINUOUS_METHODS = {"Update", "FixedUpdate", "LateUpdate"}
LITERAL = re.compile(r'^(?:-?\d+(?:\.\d+)?|true|false|null|".*")$', re.IGNORECASE)
PREFS_READ = re.compile(r'PlayerPrefs\.Get\w+\(\s*"([^"]+)"')
SENTINELS = {"(not a simple receiver)", "(not a simple target)", "(not a literal)", "_"}
# Recorded so a reader can see the premise is not what the code says, but not a
# defect: both mean the row became answerable, not that something is missing.
# Left in `issues` and out of the grade, because those answer different
# questions — what happened to this row, and whether it can be run.
DERIVATION_NOTES = set(answers.NOTES)

CANDIDATE_ISSUES = {
    observable.PARTLY,
    "ambiguous_expected_value",
    "evidence_gap:callee-condition-not-composed",
    "folded_path_condition_not_recomputed",
}


@dataclass(frozen=True)
class Pin:
    key: str
    value: str
    source_path: PathFact
    effect: dict[str, Any]
    readers: tuple[str, ...] = ()


def _method_name_from_call(call: dict[str, Any]) -> str:
    target_id = call.get("targetId")
    if target_id:
        parts = target_id.split("|")
        if len(parts) >= 3:
            return parts[2]
    return member_from_signature(call.get("target"))[1]


def _persisted_pins(graph: EvidenceGraph) -> dict[str, list[Pin]]:
    """Values written by an earlier sibling call and read by a later branch.

    Example: InitPlayerData calls InitPlayData at offset 5, which stores
    StagePosition=-1, then LoadStoryScene at offset 11, whose condition calls
    LoadPlayData.  The call order plus the shared PlayerPrefs key settles that
    branch without inventing domain meaning for -1.
    """
    call_order: dict[str, dict[str, int]] = defaultdict(dict)
    for path in graph.paths:
        if path.folded or len(path.call_path) != 1:
            continue
        for call in path.calls:
            method = _method_name_from_call(call)
            offset = call.get("offset")
            if method and isinstance(offset, int):
                call_order[path.entry_id][method] = min(
                    offset, call_order[path.entry_id].get(method, offset)
                )

    writes: dict[tuple[str, str], list[Pin]] = defaultdict(list)
    readers: dict[str, set[str]] = defaultdict(set)
    for path in graph.paths:
        source_method = member_from_signature(path.source_signature)[1]
        hop_method = member_from_signature(path.call_path[1])[1] if len(path.call_path) > 1 else source_method
        for effect in path.effects:
            if effect.get("category") != "state":
                continue
            detail = effect.get("detail")
            if effect.get("kind") == "saved" and effect.get("target") and detail is not None and LITERAL.match(str(detail)):
                writes[(path.entry_id, hop_method)].append(
                    Pin(str(effect["target"]), str(detail), path, effect)
                )
            found = PREFS_READ.search(str(detail or ""))
            if found and source_method:
                readers[source_method].add(found.group(1))

    result: dict[str, list[Pin]] = defaultdict(list)
    for path in graph.paths:
        if len(path.call_path) < 2:
            continue
        current_method = member_from_signature(path.call_path[1])[1]
        current_offset = call_order.get(path.entry_id, {}).get(current_method)
        if current_offset is None:
            continue
        condition_methods = {
            match.group(1)
            for leaf in condition_leaves(path.condition)
            for match in [re.search(r"([A-Za-z_][A-Za-z0-9_]*)\(\)", str(leaf.get("left") or ""))]
            if match
        }
        read_keys = {key for method in condition_methods for key in readers.get(method, set())}
        if not read_keys:
            continue
        for earlier_method, earlier_offset in call_order.get(path.entry_id, {}).items():
            if earlier_offset >= current_offset:
                continue
            for pin in writes.get((path.entry_id, earlier_method), []):
                if pin.key in read_keys and pin not in result[path.id]:
                    result[path.id].append(
                        Pin(pin.key, pin.value, pin.source_path, pin.effect, tuple(sorted(condition_methods)))
                    )
    return result


def _literal_compare(left: str, operator: str, right: str) -> bool | None:
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    try:
        a, b = float(left), float(right)
    except ValueError:
        return None
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}.get(operator)


def _settled_by_pins(condition: dict[str, Any], pins: list[Pin]) -> bool | None:
    kind = condition.get("kind")
    if kind == "always":
        return True
    if kind == "test":
        left = str(condition.get("left") or "")
        matching = [
            pin
            for pin in pins
            if pin.key in left or any(f"{reader}()" in left for reader in pin.readers)
        ]
        if len(matching) != 1:
            return None
        return _literal_compare(
            matching[0].value,
            str(condition.get("operator") or ""),
            str(condition.get("right") or ""),
        )
    if kind not in {"every", "either"}:
        return None
    values = [_settled_by_pins(part, pins) for part in condition.get("parts") or ()]
    if kind == "every":
        if False in values:
            return False
        return True if values and all(value is True for value in values) else None
    if True in values:
        return True
    return False if values and all(value is False for value in values) else None


def _gesture_inputs(condition: dict[str, Any]) -> list[str]:
    """The inputs the condition tree actually gates on, kept apart from `inputs[]`.

    `inputs[]` lists every input the analyser saw inside the method. The tree
    says which of them a branch waits for, and the two differ whenever one is
    there to be excluded: `anyKeyDown && !GetMouseButtonDown(2)` lists the mouse
    button and gates on the key alone.

    Reading the list instead of the tree is how a step came to say `any 또는 2`,
    asking a tester to press the button the code refuses.
    """
    values: list[str] = []
    for leaf in condition_leaves(condition):
        if leaf.get("kind") != "gesture":
            continue
        raw = leaf.get("input") or ""
        # `key:Space:down` and `key:any (down)` are the same shape said two ways.
        # Only the control belongs here; the phase is added back once, so what
        # comes out matches what `inputs[]` calls the same button.
        found = re.search(r"(?:key|mouse):([^\s:]+)", raw)
        value = found.group(1) if found else raw
        if value and value not in values:
            values.append(value)
    return values


def _alternatives(condition: dict[str, Any]) -> bool:
    """Whether the tree offers the gated inputs as a choice.

    `either(every(Right, …), every(Up, …))` is a choice and reads as `또는`.
    `every(…, key:any)` is not, whatever else the method mentions. The word is
    the tree's to give; inventing it puts a disjunction in the sheet the code
    never wrote.
    """
    if condition.get("kind") == "either":
        return True
    return any(
        _alternatives(part)
        for part in condition.get("parts") or ()
        if isinstance(part, dict)
    )


def _input_label(path: PathFact) -> tuple[str, str, str | None] | None:
    """What a tester presses — and nothing the code presses back against.

    Taken from the condition tree rather than from `inputs[]`. The list is read
    only for the kind, which the tree does not carry, and only where every gated
    input agrees on one.
    """
    gated = _gesture_inputs(path.condition)
    if not gated:
        # The tree names no input at all. Then it is not disagreeing with the
        # list, it is silent, and the list is all there is. Only where the tree
        # does speak is it the one to believe — that is where an input appears
        # in the list to be excluded rather than waited for.
        gated = [
            str(item.get("control"))
            for item in path.inputs
            if item.get("control") and not item.get("absent")
        ]
        gated = list(dict.fromkeys(gated))
    if not gated:
        return None
    kinds = {
        item.get("kind")
        for item in path.inputs
        if item.get("control") in gated and item.get("kind")
    }
    separator = "/" if _alternatives(path.condition) else " 그리고 "
    joined = separator.join(
        value if ":" in value else f"{value}:down" for value in gated
    )
    return joined, "input-expression", kinds.pop() if len(kinds) == 1 else None


def _candidate_scenes(graph: EvidenceGraph, path: PathFact) -> list[str | None]:
    placements = graph.placements_for(path.entry_type) or graph.placements_for(path.owner)
    scenes = list(dict.fromkeys(item.scene for item in placements))
    if not scenes and path.created_by:
        scene, _ = graph.resolve_created_scene(path.created_by)
        scenes = [scene]
    return scenes or [None]


def _triggers(graph: EvidenceGraph, path: PathFact) -> list[Trigger]:
    controls = graph.controls_by_handler.get((path.entry_type, path.entry_method), [])
    if not controls:
        controls = graph.controls_by_handler.get((type_leaf(path.entry_type), path.entry_method), [])
    if controls:
        out = []
        for control in controls:
            display = control.label or control.path.rsplit("/", 1)[-1]
            edge = ProofEdge(control.id, "wired_to", path.id, "exact", "objects.components.calls")
            out.append(
                Trigger(
                    "control",
                    control.scene,
                    f"{display} 조작",
                    control.path,
                    control.event,
                    "exact",
                    (edge,),
                    control.label,
                    control.sprite,
                )
            )
        return out

    input_value = _input_label(path)
    if input_value:
        value, rule, input_kind = input_value
        return [
            Trigger("input", scene, f"{value} 입력", value, path.entry_method, "exact", (
                ProofEdge(path.id, "contains_input", f"input:{value}", "exact", rule),
            ), None, None, input_kind)
            for scene in _candidate_scenes(graph, path)
        ]

    scenes = _candidate_scenes(graph, path)
    if path.entry_method in ENTRY_METHODS:
        return [Trigger("scene_entry", scene, f"{scene or '미확정 화면'} 진입", scene, path.entry_method, "derived") for scene in scenes]
    if path.entry_method in POINTER_METHODS:
        return [Trigger("pointer", scene, path.entry_method, path.owner, path.entry_method, "derived") for scene in scenes]
    if path.entry_method in COLLISION_METHODS:
        return [Trigger("collision", scene, path.entry_method, path.owner, path.entry_method, "derived") for scene in scenes]
    if path.entry_method in CONTINUOUS_METHODS:
        return [Trigger("continuous", scene, f"{scene or '미확정 화면'}에서 관찰", scene, path.entry_method, "derived") for scene in scenes]
    kind = "unreached" if path.origin == "unplaced" else "runtime_event"
    return [Trigger(kind, scene, path.entry_method, path.owner, path.entry_method, "derived") for scene in scenes]


def _pick_ref_label(ref) -> str | None:
    return ref.path or ref.name


def _resolve_code_target(
    graph: EvidenceGraph,
    path: PathFact,
    raw: str | None,
    scene: str | None,
) -> tuple[str | None, str, list[ProofEdge]]:
    if not raw or raw.strip() in SENTINELS:
        return None, "unresolved", []
    if raw in {"this", "Component.gameObject", "GameObject"} or raw.startswith("this."):
        placements = graph.placements_for(path.owner, scene)
        labels = list(dict.fromkeys(item.path for item in placements))
        if len(labels) == 1:
            return labels[0], "derived", [
                ProofEdge(path.id, "targets", placements[0].id, "derived", "owner-placement")
            ]
        if labels:
            return " | ".join(labels), "ambiguous", []

    parsed = target_parts(raw)
    if parsed:
        owner, field_name = parsed
        refs = graph.refs_for(owner, field_name, scene)
        labels = list(dict.fromkeys(label for item in refs if (label := _pick_ref_label(item))))
        if len(labels) == 1:
            return labels[0], "exact", [
                ProofEdge(path.id, "targets", refs[0].id, "exact", "component-ref-field")
            ]
        if labels:
            return " | ".join(labels), "ambiguous", [
                ProofEdge(path.id, "targets", item.id, "ambiguous", "component-ref-list")
                for item in refs
            ]
        if owner in {type_leaf(path.owner), type_leaf(path.entry_type)}:
            placements = graph.placements_for(path.owner, scene)
            labels = list(dict.fromkeys(item.path for item in placements))
            if len(labels) == 1:
                return f"{labels[0]}.{field_name}", "derived", [
                    ProofEdge(path.id, "targets", placements[0].id, "derived", "owner-field")
                ]

    return raw, "unresolved", []


def _resolve_value(
    graph: EvidenceGraph,
    path: PathFact,
    effect: dict[str, Any],
    scene: str | None,
) -> tuple[Any, str, list[ProofEdge]]:
    kind = effect.get("kind")
    # The API's own second argument is not part of the value. Left in, a masked
    # parameter reads as the value `_, true` and goes out as something to check.
    detail = observable.value_of(effect.get("detail")) if effect.get("detail") is not None else None
    # 값이 매개변수라 못 읽힌 자리는 호출부가 답한다.
    # `SetPromptVisible(true)` 를 부르는 경로에서는 그 효과의 값이 `true` 다.
    if detail in answers.PARAMETER and graph.answers is not None:
        passed = graph.answers.parameter_value(path)
        if passed is not None:
            detail = passed
    if kind == "scene":
        return effect.get("target"), "exact", []
    if kind in {"quit", "destroy"}:
        return None, "exact", []
    if kind in {"active-state", "interactable", "component-enabled"}:
        lowered = str(detail).lower()
        if lowered in {"true", "false"}:
            return lowered == "true", "exact", []
    candidates = effect.get("targetCandidates") or ()
    if candidates:
        return list(candidates), "ambiguous", []
    if detail is None:
        return None, "unresolved", []
    if str(detail).strip() in SENTINELS:
        return None, "unresolved", []
    if kind == "animation" and re.fullmatch(
        r'(?:SetTrigger|ResetTrigger|Play|CrossFade|CrossFadeInFixedTime)\(\s*"(?:[^"\\]|\\.)*"(?:\s*,\s*-?\d+(?:\.\d+)?[fF]?)*\s*\)',
        str(detail).strip(),
    ):
        return detail, "exact", []
    resolved, resolution, proof = _resolve_code_target(graph, path, str(detail), scene)
    if resolution in {"exact", "derived"}:
        return resolved, resolution, proof
    if LITERAL.match(str(detail)):
        return detail, "exact", []
    # Keep the SDK expression verbatim, but do not pretend it is a concrete oracle.
    return detail, "ambiguous" if any(ch in str(detail) for ch in "().,") else "derived", []


def _operation(kind: str, value: Any) -> str:
    if kind == "scene":
        return "transition"
    if kind == "quit":
        return "quit"
    if kind in {"active-state", "interactable", "component-enabled"}:
        return "enable" if value is True else "disable" if value is False else "set"
    return {
        "ui-value": "display",
        "animation": "animate",
        "audio": "play",
        "instantiate": "create",
        "destroy": "destroy",
        "transform": "transform",
        "write": "write",
        "saved": "save",
    }.get(kind, "set")


def _assertion(
    graph: EvidenceGraph,
    path: PathFact,
    effect: dict[str, Any],
    scene: str | None,
) -> tuple[Assertion, list[str]]:
    kind = effect.get("kind") or "unknown"
    raw_target = effect.get("target")
    if kind == "scene":
        target, target_resolution, target_proof = raw_target, "exact", []
    elif kind == "quit":
        target, target_resolution, target_proof = "game", "exact", []
    else:
        target, target_resolution, target_proof = _resolve_code_target(graph, path, raw_target, scene)
    value, value_resolution, value_proof = _resolve_value(graph, path, effect, scene)
    # Instantiate already states its observable result through the resolved
    # prefab/object target. A separate return value is not part of the human
    # oracle and must not demote an otherwise exact creation contract.
    if kind == "instantiate" and target_resolution in {"exact", "derived"}:
        value, value_resolution, value_proof = None, "exact", []
    resolution = target_resolution
    if value_resolution in {"unresolved", "ambiguous"}:
        resolution = value_resolution
    elif resolution == "exact" and value_resolution == "derived":
        resolution = "derived"
    source = path.source_ref(effect.get("offset"))
    observable_by = "audio" if kind == "audio" else "scene"
    target_label, target_sprite = graph.visual_for(target, scene)
    assertion = Assertion(
        kind,
        target,
        _operation(kind, value),
        value,
        effect.get("category") or "observable",
        resolution,
        observable_by,
        source,
        tuple(target_proof + value_proof),
        target_label,
        target_sprite,
    )
    issues = []
    if target_resolution in {"unresolved", "ambiguous"}:
        issues.append(f"{target_resolution}_target")
    if value_resolution in {"unresolved", "ambiguous"} and kind not in {"destroy", "quit"}:
        issues.append(f"{value_resolution}_expected_value")
    if kind == "audio":
        issues.append("observation_unsupported:audio")
    return assertion, issues


def _state_target(path: PathFact, target: Any) -> Any:
    """Disambiguate compiler state-machine fields that share short names."""

    if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target):
        return target
    declaring, _ = member_from_signature(path.source_signature)
    match = re.search(r"(.+)/<([^>]+)>d__\d+$", declaring)
    if not match:
        return target
    owner = type_leaf(match.group(1))
    return f"{owner}.{match.group(2)}.{target}"


def _supporting_state(path: PathFact, effect: dict[str, Any]) -> SupportingState:
    kind = effect.get("kind") or "write"
    value = effect.get("detail")
    return SupportingState(
        _state_target(path, effect.get("target")),
        _operation(kind, value),
        value,
        path.source_ref(effect.get("offset")),
    )


def _condition_issues(condition: dict[str, Any]) -> list[str]:
    issues = []
    leaves = list(condition_leaves(condition))
    if len(leaves) > 8:
        issues.append("condition_too_complex")
    for leaf in leaves:
        if leaf.get("kind") == "unknown":
            issues.append("unread_condition")
        if leaf.get("kind") == "test" and leaf.get("context") is None:
            issues.append("condition_subject_unresolved")
        if leaf.get("subjectLost"):
            issues.append("condition_subject_lost")
        if leaf.get("unread"):
            issues.append("condition_operand_unread")
    return list(dict.fromkeys(issues))


def _active_scene_verdict(condition: dict[str, Any], scene: str | None) -> bool | None:
    """Evaluate only the scene predicate the capture itself settles."""
    if scene is None:
        return None
    kind = condition.get("kind")
    if kind == "always":
        return True
    if kind == "test" and condition.get("left") == "SceneManager.GetActiveScene().name":
        expected = str(condition.get("right") or "").strip('"')
        operator = condition.get("operator")
        if operator == "==":
            return scene == expected
        if operator == "!=":
            return scene != expected
        return None
    if kind not in {"every", "either"}:
        return None
    values = [_active_scene_verdict(part, scene) for part in condition.get("parts") or ()]
    if kind == "every":
        if False in values:
            return False
        return True if values and all(value is True for value in values) else None
    if True in values:
        return True
    return False if values and all(value is False for value in values) else None


FLIP = {"==": "!=", "!=": "==", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}

# 가드와 효과가 동시에 참인 순간이 없는 레코드. 상태가 아니라 전이이므로, 상태로
# 읽는 트리거(진입해 관찰)와 짝지으면 어느 순간에도 참이 아닌 행이 된다.
MISTIMED = "trigger_reads_a_transition_as_a_state"


def _contradictory(condition: dict[str, Any]) -> bool:
    """한 조건 안에 서로 반대인 항이 함께 있는가.

    코루틴의 루프 조건과 탈출 조건이 한 경로에 합성되면
    `i < 총개수 그리고 i >= 총개수` 가 된다. 둘 다 참인 순간은 없으므로 그 행은
    일어나지 않는다. 읽는 사람이 만들 수 없는 전제를 주는 것보다 안 내는 것이 낫다.

    `every` 안에서만 본다. `either` 는 서로 반대인 갈래를 담는 것이 정상이다.
    """
    if condition.get("kind") != "every":
        return any(
            _contradictory(part)
            for part in condition.get("parts") or ()
            if isinstance(part, dict)
        )
    seen: set[tuple[str, str, str]] = set()
    for part in condition.get("parts") or ():
        if not isinstance(part, dict):
            continue
        if _contradictory(part):
            return True
        if part.get("kind") != "test":
            continue
        left, operator, right = (
            str(part.get("left")),
            str(part.get("operator")),
            str(part.get("right")),
        )
        if (left, FLIP.get(operator, operator), right) in seen:
            return True
        seen.add((left, operator, right))
    return False

def _quality(trigger: Trigger, assertions: list[Assertion], issues: list[str]) -> Quality:
    issues = [issue for issue in issues if issue not in DERIVATION_NOTES]
    if any(issue.startswith("observation_unsupported") for issue in issues):
        return "unsupported"
    # A trigger named after the method that carries it — `CompleteStream` — says
    # the evidence did not find a way to cause this. A step nobody can carry out
    # is not the top grade, whatever else is exact about the row.
    if trigger.kind in {"runtime_event", "unreached"}:
        return "review"
    if trigger.scene is None or trigger.resolution in {"ambiguous", "unresolved"}:
        return "review"
    if any(item.resolution == "unresolved" or not item.target for item in assertions):
        return "review"
    if issues or any(item.resolution == "ambiguous" for item in assertions):
        return (
            "candidate"
            if set(issues).issubset(CANDIDATE_ISSUES)
            else "review"
        )
    return "ready"


def _folded_condition_unproven(graph: EvidenceGraph, path: PathFact) -> bool:
    """Whether an alternate entry lost an upstream guard we cannot compose.

    A direct alternate entry into the source method keeps the source method's
    own condition. A longer route is also safe when every preceding method is
    present for the same entry with an unconditional path. The SDK callPath is
    still the causal edge; this check only decides whether an omitted upstream
    guard remains possible.
    """
    if not path.folded:
        return False
    if len(path.call_path) == 1 and path.call_path[0] == path.source_signature:
        return False
    for signature in path.call_path[:-1]:
        prefixes = [
            item
            for item in graph.paths
            if not item.folded
            and item.entry_id == path.entry_id
            and item.source_signature == signature
        ]
        if not prefixes or any(item.condition.get("kind") != "always" for item in prefixes):
            return True
    return False


TRIGGER_NOT_ACTIONABLE = "trigger_not_actionable"


def _execution_axes(path: PathFact, trigger: Trigger, assertions: list[Assertion]) -> tuple[str, str, str]:
    if trigger.kind in {"control", "input", "pointer"}:
        actionability = "direct"
    elif trigger.kind in {"scene_entry", "continuous"}:
        actionability = "observation_only"
    elif trigger.kind == "unreached":
        actionability = "blocked"
    else:
        actionability = "indirect"
    observability = (
        "unsupported"
        if any(item.observable_by == "audio" for item in assertions)
        else "direct"
        if assertions
        else "unknown"
    )
    applicability = (
        "unknown"
        if trigger.scene is None or path.origin == "unplaced"
        else "capture_confirmed"
    )
    return actionability, observability, applicability


def _condition_offsets(condition: dict[str, Any]) -> list[int]:
    return [
        int(leaf["offset"])
        for leaf in condition_leaves(condition)
        if isinstance(leaf.get("offset"), int)
    ]


def _combine_conditions(*conditions: dict[str, Any]) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for condition in conditions:
        if not condition or condition.get("kind") == "always":
            continue
        candidates = (
            condition.get("parts") or ()
            if condition.get("kind") == "every"
            else (condition,)
        )
        for candidate in candidates:
            signature = condition_signature(candidate)
            if signature not in seen:
                seen.add(signature)
                parts.append(candidate)
    if not parts:
        return {"kind": "always"}
    if len(parts) == 1:
        return parts[0]
    return {"kind": "every", "parts": parts}


def _call_matches_signature(call: dict[str, Any], signature: str) -> bool:
    target = call.get("target")
    if target == signature:
        return True
    target_id = call.get("targetId")
    declaring, method = member_from_signature(signature)
    return bool(
        target_id
        and target_id.split("|")[1:3] == [declaring, method]
    )


def _delta_updates(paths: list[PathFact], handoff: int) -> dict[str, int]:
    updates: dict[str, int] = {}
    for path in paths:
        for effect in path.effects:
            target = effect.get("target")
            detail = str(effect.get("detail") or "")
            offset = effect.get("offset")
            if not isinstance(target, str) or not isinstance(offset, int) or offset <= handoff:
                continue
            match = re.fullmatch(
                rf"\(?{re.escape(target)}\s*([+-])\s*(\d+)\)?",
                detail,
            )
            if match:
                magnitude = int(match.group(2))
                updates[target] = magnitude if match.group(1) == "+" else -magnitude
    return updates


def _top_level_args(raw: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(raw):
        if char in "([<":
            depth += 1
        elif char in ")]>":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(raw[start:index].strip())
            start = index + 1
    tail = raw[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _integer_arithmetic_evidence(
    paths: list[PathFact],
) -> tuple[set[str], set[str]]:
    """Find integer cursor and bound expressions from typed call signatures."""

    targets: set[str] = set()
    expressions: set[str] = set()
    for path in paths:
        for call in path.calls:
            signature = str(call.get("target") or "")
            _head, separator, raw_parameters = signature.rpartition("(")
            if not separator:
                continue
            parameters = _top_level_args(raw_parameters.removesuffix(")"))
            arguments = _top_level_args(str(call.get("args") or ""))
            for parameter, argument in zip(parameters, arguments):
                if parameter == "System.Int32" and re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", argument
                ):
                    targets.add(argument)
            if signature.startswith("System.Int32 ") and not parameters:
                _, method = member_from_signature(signature)
                receiver = str(call.get("receiver") or "")
                expressions.add(f"{receiver + '.' if receiver else ''}{method}()")
    return targets, expressions


def _condition_before_updates(
    condition: dict[str, Any],
    updates: dict[str, int],
    integer_targets: set[str],
    integer_expressions: set[str],
) -> dict[str, Any]:
    """Express a post-resume branch guard in terms of the pre-input state."""

    kind = condition.get("kind")
    if kind in {"every", "either"}:
        return {
            **condition,
            "parts": [
                _condition_before_updates(
                    part,
                    updates,
                    integer_targets,
                    integer_expressions,
                )
                for part in condition.get("parts") or ()
            ],
        }
    if kind != "test":
        return condition
    left = condition.get("left")
    if left not in updates:
        return condition
    delta = updates[str(left)]
    operator = "+" if delta >= 0 else "-"
    projected = {
        **condition,
        "left": f"({left} {operator} {abs(delta)})",
        "projectedFromPostResume": left,
        "projectedDelta": delta,
    }
    if str(left) in integer_targets and str(condition.get("right")) in integer_expressions:
        projected["arithmeticDomain"] = "integer"
    return projected


def _gates(graph: EvidenceGraph) -> list[PathFact]:
    """입력으로 열리는 대기 지점. 자기 입력을 들고 있지 않으면 한 홉 옆에서 빌린다.

    코루틴의 대기는 대리자로 넘겨진 조각이고, 그 조각이 키를 직접 읽으면 `inputs` 가
    채워진다. 직접 읽지 않고 **읽는 메서드를 부르면** 비어 있다 — 입력이 없는 것이
    아니라 한 홉 옆에 있는 것이다. 근거는 그것을 `calls` 에 적어 두었다.

    빈 것을 없는 것으로 세면 대기가 둘인데 하나만 보이고, **두 대기 사이에서 일어난
    일이 뒤쪽 대기의 결과로 붙는다.** 첫 대기에서 눌러 일어난 변화가 두 번째 입력의
    기대 결과가 되어, 어느 순간에도 참이 아닌 행이 된다.

    빌릴 때 그 조각의 가드도 함께 산다. 키를 읽는 가지가 조건부면 그 조건이 곧 "이
    상태에서 눌러야 열린다" 이고, 그것이 사전 조건이다.
    """

    reads_input: dict[str, list[PathFact]] = {}
    for path in graph.paths:
        if path.inputs:
            reads_input.setdefault(path.source_signature, []).append(path)

    gates: list[PathFact] = []
    for path in graph.paths:
        if (
            path.handed_over_at is None
            or not path.handed_over_to
            or "reached-through-delegate" not in path.gaps
            or len(path.call_path) < 2
        ):
            continue
        if path.inputs:
            gates.append(path)
            continue
        # 같은 진입점에서 온 것만 빌린다. 다른 진입점의 같은 메서드는 같은 키를 읽되
        # 다른 조건 아래서 읽고, 그 조건은 이 대기의 것이 아니다.
        borrowed = [
            found
            for call in path.calls
            for found in reads_input.get(str(call.get("target")), ())
            if found.entry_id == path.entry_id
        ]
        # 부르는 곳이 여럿이면 어느 입력이 이 대기를 여는지 근거가 말하지 않는다.
        if len(borrowed) != 1:
            continue
        gates.append(
            replace(
                path,
                id=f"{path.id}~gate",
                inputs=borrowed[0].inputs,
                condition=_combine_conditions(path.condition, borrowed[0].condition),
            )
        )
    return gates


def _coroutine_resume_contracts(
    graph: EvidenceGraph,
    contracts: list[Contract],
) -> tuple[list[Contract], set[str]]:
    """Project WaitUntil-style delegate input onto resumed coroutine effects.

    The SDK keeps the input in a handed-over delegate and the observable effect
    in separate state-machine records.  Handoff offset plus the same entry and
    state-machine call path provide the causal edge without naming a particular
    key, coroutine, or game type.
    """

    waits = _gates(graph)
    resumed: list[Contract] = []
    superseded: set[str] = set()
    for wait in waits:
        coroutine_signature = wait.call_path[-2]
        handoff = int(wait.handed_over_at)
        coroutine_paths = [
            path
            for path in graph.paths
            if not path.folded
            and path.entry_id == wait.entry_id
            and path.source_signature == coroutine_signature
        ]
        updates = _delta_updates(coroutine_paths, handoff)
        integer_targets, integer_expressions = _integer_arithmetic_evidence(
            coroutine_paths
        )
        wait_context_conditions = [
            path.condition
            for path in coroutine_paths
            if path.condition.get("kind") != "always"
            and any(offset > handoff for offset in _condition_offsets(path.condition))
            and path.calls
            and max(
                (
                    call.get("offset")
                    for call in path.calls
                    if isinstance(call.get("offset"), int)
                ),
                default=handoff,
            )
            < handoff
        ]
        input_offset = next(
            (
                item.get("offset")
                for item in wait.inputs
                if isinstance(item.get("offset"), int)
            ),
            None,
        )
        post_resume_states = [
            _supporting_state(path, effect)
            for path in coroutine_paths
            for effect in path.effects
            if effect.get("category") == "state"
            and isinstance(effect.get("offset"), int)
            and effect["offset"] > handoff
        ]

        for contract in contracts:
            if not contract.call_path or coroutine_signature not in contract.call_path:
                continue
            if contract.source_refs[0].entry_id != wait.entry_id:
                continue
            coroutine_index = contract.call_path.index(coroutine_signature)
            if coroutine_index + 1 >= len(contract.call_path):
                continue
            next_signature = contract.call_path[coroutine_index + 1]
            upstream_paths = [
                path
                for path in coroutine_paths
                # 호출 자체가 대기보다 뒤여야 한다. 같은 구간의 가드가 뒤에서
                # 평가된다는 것은 **가드**가 뒤라는 말이지 호출이 뒤라는 말이 아니다.
                # 앞의 호출까지 받아 주면 루프를 한 바퀴 돌아 닿는 결과가 이번 입력의
                # 기대가 되어, 같은 기대를 정반대 전제로 두 번 내게 된다.
                if any(
                    _call_matches_signature(call, next_signature)
                    and isinstance(call.get("offset"), int)
                    and call["offset"] > handoff
                    for call in path.calls
                )
            ]
            contract_after_handoff = any(
                offset > handoff for offset in _condition_offsets(contract.condition)
            )
            if not upstream_paths and not contract_after_handoff:
                continue

            upstream_conditions = [path.condition for path in upstream_paths]
            upstream_signatures = {
                condition_signature(condition) for condition in upstream_conditions
            }
            projected_upstream = [
                _condition_before_updates(
                    condition,
                    updates,
                    integer_targets,
                    integer_expressions,
                )
                for condition in upstream_conditions
            ]
            retained_contract_condition = (
                {"kind": "always"}
                if condition_signature(contract.condition) in upstream_signatures
                else _condition_before_updates(
                    contract.condition,
                    updates,
                    integer_targets,
                    integer_expressions,
                )
                if contract_after_handoff and not upstream_conditions
                else contract.condition
            )
            effective_condition = _combine_conditions(
                *wait_context_conditions,
                *projected_upstream,
                retained_contract_condition,
            )
            composed_callee_guard = any(
                condition.get("kind") != "always"
                and condition_signature(condition) != condition_signature(contract.condition)
                for condition in upstream_conditions
            )
            issues = [
                issue
                for issue in contract.issues
                if not (
                    composed_callee_guard
                    and issue == "evidence_gap:callee-condition-not-composed"
                )
            ]
            states = list(contract.supporting_state)
            for state in post_resume_states:
                if state not in states:
                    states.append(state)

            for trigger in _triggers(graph, wait):
                if trigger.scene != contract.scene:
                    continue
                proof = tuple(trigger.proof) + (
                    ProofEdge(
                        wait.id,
                        "resumes_to",
                        contract.source_refs[0].record_id,
                        "derived",
                        "delegate-handoff-order",
                    ),
                )
                resumed_trigger = Trigger(
                    trigger.kind,
                    trigger.scene,
                    trigger.label,
                    trigger.target,
                    trigger.event,
                    "derived",
                    proof,
                    trigger.target_label,
                    trigger.target_sprite,
                    trigger.input_kind,
                )
                source_refs = list(
                    dict.fromkeys(
                        [wait.source_ref(input_offset), *contract.source_refs]
                    )
                )
                contract_id = stable_id(
                    "contract",
                    "coroutine-resume",
                    wait.id,
                    contract.id,
                    resumed_trigger.identity,
                    condition_signature(effective_condition),
                )
                superseded.add(contract.id)
                resumed.append(
                    Contract(
                        contract_id,
                        contract.capture,
                        contract.scene,
                        resumed_trigger,
                        effective_condition,
                        list(contract.assertions),
                        states,
                        contract.call_path,
                        source_refs,
                        _quality(resumed_trigger, contract.assertions, issues),
                        "direct",
                        contract.observability,
                        contract.applicability,
                        issues,
                        contract.folded_path,
                    )
                )
    unique: dict[str, Contract] = {contract.id: contract for contract in resumed}
    return list(unique.values()), superseded


def _contracts(graph: EvidenceGraph, persisted_pins: dict[str, list[Pin]]) -> list[Contract]:
    contracts: list[Contract] = []
    for path in graph.paths:
        if "singleton-plumbing" in path.gaps:
            continue
        raw_assertions = [effect for effect in path.effects if effect.get("category") in ASSERTABLE]
        if not raw_assertions:
            continue
        raw_state = [effect for effect in path.effects if effect.get("category") == "state"]
        pins = persisted_pins.get(path.id, [])
        pin_verdict = _settled_by_pins(path.condition, pins) if pins else None
        if pin_verdict is False:
            continue
        effective_condition = (
            {"kind": "always", "settledFrom": path.condition}
            if pin_verdict is True and path.condition.get("kind") != "always"
            else path.condition
        )
        for trigger in _triggers(graph, path):
            if _active_scene_verdict(path.condition, trigger.scene) is False:
                continue
            assertions: list[Assertion] = []
            issues = _condition_issues(effective_condition)
            for effect in raw_assertions:
                assertion, found = _assertion(graph, path, effect, trigger.scene)
                assertions.append(assertion)
                issues.extend(found)
            issues.extend(f"evidence_gap:{gap}" for gap in path.gaps if gap in BLOCKING_GAPS)
            if "reached-through-delegate" in path.gaps and path.handed_over_at is None:
                issues.append("causal_path_unproven:delegate")
            if _folded_condition_unproven(graph, path):
                issues.append("folded_path_condition_not_recomputed")
            if trigger.scene is None:
                issues.append("application_scene_unknown")
            if trigger.kind == "unreached":
                issues.append("runtime_instance_unobserved")
            issues = list(dict.fromkeys(issues))
            contract_id = stable_id(
                "contract",
                path.id,
                trigger.identity,
                condition_signature(effective_condition),
                [item.identity for item in assertions],
            )
            states = [_supporting_state(path, effect) for effect in raw_state]
            states.extend(_supporting_state(pin.source_path, pin.effect) for pin in pins)
            unique_states = []
            seen_states = set()
            for state in states:
                mark = (state.target, state.operation, str(state.value), state.source)
                if mark not in seen_states:
                    seen_states.add(mark)
                    unique_states.append(state)
            source_refs = list(dict.fromkeys(item.source for item in assertions))
            source_refs.extend(
                state.source for state in unique_states if state.source not in source_refs
            )
            actionability, observability, applicability = _execution_axes(path, trigger, assertions)
            contracts.append(
                Contract(
                    contract_id,
                    graph.capture,
                    trigger.scene,
                    trigger,
                    effective_condition,
                    assertions,
                    unique_states,
                    path.call_path,
                    source_refs,
                    _quality(trigger, assertions, issues),
                    actionability,
                    observability,
                    applicability,
                    issues,
                    path.folded,
                )
            )
    contracts.sort(key=lambda item: (item.scene or "", item.trigger.label, item.id))
    return contracts


def _drop_unavailable_control_contracts(contracts: list[Contract]) -> list[Contract]:
    """Remove click paths whose own precondition makes the control unavailable."""
    def guard_key(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: guard_key(item)
                for key, item in sorted(value.items())
                if key not in {"offset", "settledFrom"}
            }
        if isinstance(value, list):
            return [guard_key(item) for item in value]
        return value

    def signature(condition: dict[str, Any]) -> str:
        return json.dumps(guard_key(condition), ensure_ascii=False, sort_keys=True)

    disabled = {
        (contract.scene, assertion.target, signature(contract.condition))
        for contract in contracts
        for assertion in contract.assertions
        if assertion.operation == "disable"
    }
    return [
        contract
        for contract in contracts
        if not (
            contract.trigger.kind == "control"
            and (
                contract.scene,
                contract.trigger.target,
                signature(contract.condition),
            )
            in disabled
        )
    ]


def _inventory_contracts(graph: EvidenceGraph, code_contracts: list[Contract]) -> list[Contract]:
    """Create explicit UI-state checks from object state plus control wiring.

    A raw snapshot is not emitted for a control whose active state is changed
    by code in the same scene. In that case the guarded code contract is the
    stronger specification (for example, Continue hidden when no save exists).
    """
    mutated = {
        (contract.scene, assertion.target)
        for contract in code_contracts
        for assertion in contract.assertions
        if assertion.kind == "active-state"
    }
    contracts: list[Contract] = []
    seen: set[tuple[str | None, str]] = set()
    for control in graph.controls:
        key = (control.scene, control.path)
        if key in seen or key in mutated or control.active is None:
            continue
        seen.add(key)
        source = SourceRef(
            control.object_id,
            f"scene:{control.scene or 'unknown'}",
            f"object:{control.selector or control.path}",
            None,
        )
        trigger = Trigger(
            "control_check",
            control.scene,
            f"{control.path} 표시 상태 확인",
            control.path,
            None,
            "exact",
            (ProofEdge(control.object_id, "contains_control", control.id, "exact", "objects.components.calls"),),
            control.label,
            control.sprite,
        )
        assertion = Assertion(
            "active-state",
            control.path,
            "enable" if control.active else "disable",
            control.active,
            "availability",
            "exact",
            "scene",
            source,
            (ProofEdge(control.object_id, "snapshot_state", str(control.active).lower(), "exact", "objects.active"),),
            control.label,
            control.sprite,
        )
        condition = {"kind": "always"}
        contract_id = stable_id("contract", control.object_id, "control-check", control.active)
        contracts.append(
            Contract(
                contract_id,
                graph.capture,
                control.scene,
                trigger,
                condition,
                [assertion],
                [],
                (f"SDKObject::{control.path}",),
                [source],
                "ready" if control.scene else "review",
                "observation_only",
                "direct",
                "capture_confirmed" if control.scene else "unknown",
                [] if control.scene else ["application_scene_unknown"],
                False,
            )
        )
    return contracts


def _feature_name(contract: Contract) -> str:
    signature = contract.call_path[1] if len(contract.call_path) > 1 else contract.call_path[0]
    declaring, method = member_from_signature(signature)
    return f"{type_leaf(declaring)}.{method}" if method else signature


def _branch_families(contracts: list[Contract]) -> list[BranchFamily]:
    groups: dict[tuple[Any, ...], list[Contract]] = defaultdict(list)
    for contract in contracts:
        groups[contract.feature_key].append(contract)
    families = []
    for key, group in groups.items():
        conditions = {condition_signature(item.condition) for item in group}
        if len(conditions) < 2:
            continue
        # A family is about the same result shape under different guards.  Do not
        # fuse arms; the family only references their immutable contract ids.
        result_shapes = {
            tuple((a.kind, a.target, a.operation) for a in item.assertions)
            for item in group
        }
        if len(result_shapes) > max(3, len(group) // 2):
            continue
        issues = sorted({issue for item in group for issue in item.issues})
        qualities = {item.quality for item in group}
        quality: Quality
        if qualities == {"ready"}:
            quality = "ready"
        elif qualities.issubset({"ready", "candidate"}):
            quality = "candidate"
        else:
            quality = "review"
        families.append(
            BranchFamily(
                stable_id("family", key, sorted(conditions)),
                group[0].scene,
                group[0].trigger,
                _feature_name(group[0]),
                [item.id for item in group],
                sorted({item.condition.get("kind") or "unknown" for item in group}),
                quality,
                issues,
            )
        )
    families.sort(key=lambda item: (item.scene or "", item.feature, item.id))
    return families


def _state_paths_by_entry(graph: EvidenceGraph) -> dict[str, list[PathFact]]:
    result: dict[str, list[PathFact]] = defaultdict(list)
    for path in graph.paths:
        if any(effect.get("category") == "state" for effect in path.effects):
            result[path.entry_id].append(path)
    return result


def _compatible_state(contract: Contract, path: PathFact) -> bool:
    own = condition_signature(contract.condition)
    other = condition_signature(path.condition)
    if other != condition_signature({"kind": "always"}) and other != own:
        return False
    if not contract.call_path or not path.call_path:
        return False
    shortest = min(len(contract.call_path), len(path.call_path))
    shared = 0
    for index in range(shortest):
        if contract.call_path[index] != path.call_path[index]:
            break
        shared += 1
    # One route must be an actual prefix of the other. Merely sharing an entry
    # point is not enough: sibling calls under one Update/Start are not causal.
    return shared == shortest


def _scenarios(graph: EvidenceGraph, contracts: list[Contract]) -> list[Scenario]:
    groups: dict[tuple[Any, ...], list[Contract]] = defaultdict(list)
    for contract in contracts:
        entry_id = contract.source_refs[0].entry_id
        groups[(contract.trigger.identity, entry_id, condition_signature(contract.condition))].append(contract)

    state_paths = _state_paths_by_entry(graph)
    scenarios: list[Scenario] = []
    for key, group in groups.items():
        entry_id = key[1]
        assertions: list[Assertion] = []
        states: list[SupportingState] = []
        seen_assertions = set()
        seen_states = set()
        for contract in group:
            for assertion in contract.assertions:
                if assertion.identity not in seen_assertions:
                    seen_assertions.add(assertion.identity)
                    assertions.append(assertion)
            for state in contract.supporting_state:
                mark = (state.target, state.operation, str(state.value), state.source)
                if mark not in seen_states:
                    seen_states.add(mark)
                    states.append(state)
        is_coroutine_resume = any(
            edge.rule == "delegate-handoff-order"
            for edge in group[0].trigger.proof
        )
        if not is_coroutine_resume:
            for path in state_paths.get(entry_id, []):
                if not any(_compatible_state(contract, path) for contract in group):
                    continue
                for effect in path.effects:
                    if effect.get("category") != "state":
                        continue
                    state = _supporting_state(path, effect)
                    mark = (state.target, state.operation, str(state.value), state.source)
                    if mark not in seen_states:
                        seen_states.add(mark)
                        states.append(state)

        paths = list(dict.fromkeys(contract.call_path for contract in group))
        connected = (
            len(group) > 1
            or len(assertions) > 1
            or bool(states)
            or any(len(path) > 1 for path in paths)
            or group[0].trigger.kind == "control_check"
        )
        if not connected:
            continue
        issues = sorted({issue for item in group for issue in item.issues})
        # A derivation note is not a reason to distrust the scenario; the
        # contracts it came from already took it into account.
        defects = [issue for issue in issues if issue not in DERIVATION_NOTES]
        quality: Quality
        if any(item.quality == "unsupported" for item in group):
            quality = "unsupported"
        elif any(item.quality == "review" for item in group):
            quality = "review"
        elif any(item.quality == "candidate" for item in group):
            quality = "candidate"
        elif all(item.quality == "ready" for item in group) and not defects:
            quality = "ready"
        else:
            quality = "review"
        title = f"{group[0].trigger.label} → {len(assertions)}개 관찰 결과"
        scenarios.append(
            Scenario(
                stable_id("scenario", key, [item.id for item in group], [item.identity for item in assertions]),
                group[0].scene,
                group[0].trigger,
                title,
                [item.id for item in group],
                assertions,
                states,
                paths,
                quality,
                group[0].actionability,
                "unsupported" if any(item.observability == "unsupported" for item in group) else "direct",
                "unknown" if any(item.applicability == "unknown" for item in group) else "capture_confirmed",
                issues,
            )
        )
    quality_rank = {"ready": 0, "candidate": 1, "review": 2, "unsupported": 3}
    scenarios.sort(
        key=lambda item: (
            quality_rank[item.quality],
            -len(item.assertions),
            -max((len(path) for path in item.call_paths), default=0),
            -len(item.supporting_state),
            item.scene or "",
            item.id,
        )
    )
    return scenarios


def _rewrite_unreadable_premises(graph: EvidenceGraph, contracts: list[Contract]) -> None:
    """Say each premise as something the running game reports, where it can be.

    A premise naming a local or a call cannot be checked while playing, so a row
    carrying one either proves nothing or is confirmed by the very observation it
    expects. Where the guarded branch assigns one readable field from another,
    that assignment is the same fact in a form the reader publishes.
    """
    # No early return when the table is empty. Restating a premise and judging
    # whether one is answerable are separate questions, and a report where
    # nothing could be restated is exactly where the judging matters.
    known = graph.answers or Answers.of(
        [path for path in graph.paths if not path.folded]
    )
    # 되돌아가는 구간이 이름 붙인 프레임. 계약이 아니라 **항**을 이것으로 가른다 —
    # 루프는 부르는 쪽에 있고 조건은 불리는 쪽 계약까지 따라간다.
    looping_frames = {
        ".".join(part for part in member_from_signature(path.source_signature) if part)
        for path in graph.paths
        if path.loops_back_to is not None
    }
    for contract in contracts:
        asserted = {item.target for item in contract.assertions if item.target}
        # 언제나 다시 담는다. `notes` 는 **유도**가 있었는지만 말하고, 같은 것을
        # 바르게 읽는 고침(참조의 `!= 0` → `!= null`)은 유도가 아니라 남길 note 가
        # 없다. note 가 있을 때만 담으면 그런 고침이 조용히 버려진다.
        contract.condition, notes = known.resolve(
            contract.condition, asserted, tuple(contract.call_path)
        )
        for note in notes:
            if note not in contract.issues:
                contract.issues.append(note)
        # 가드를 스스로 뒤집는 레코드를 상태로 읽고 있으면 그 행은 트리거가 틀렸다.
        if contract.trigger.kind in {"scene_entry", "continuous", "control_check"}:
            # `SourceRef.record_id` 는 경로 id 를 담는다. 이름과 내용이 어긋난
            # 자리라 둘 다로 찾는다.
            wanted = {ref.record_id for ref in contract.source_refs}
            source = next(
                (
                    path
                    for path in graph.paths
                    if path.id in wanted or path.record_id in wanted
                ),
                None,
            )
            if source is not None and answers.negates_own_guard(
                contract.condition, source.effects
            ):
                if MISTIMED not in contract.issues:
                    contract.issues.append(MISTIMED)
                contract.quality = _quality(
                    contract.trigger, contract.assertions, contract.issues
                )

        # 루프의 살림은 전제가 아니다.
        trimmed, dropped = observable.drop_loop_bookkeeping(
            contract.condition, looping_frames
        )
        if dropped:
            contract.condition = trimmed
            if observable.LOOP_COUNTER not in contract.issues:
                contract.issues.append(observable.LOOP_COUNTER)


        # What no branch could restate stays unreadable, and a row resting on it
        # cannot be set up or confirmed. Said rather than dropped: the behaviour
        # is real and someone may still write the premise another way.
        if observable.unreadable_atoms(contract.condition):
            # Some of it answerable is not the same as none of it. A row whose
            # other premise is a field can be set up from that field and the rest
            # gauged from the screen; a row where nothing is readable leaves the
            # tester without a place to start.
            issue = (
                observable.PARTLY
                if observable.readable_atoms(contract.condition)
                else observable.UNCHECKABLE
            )
            if issue not in contract.issues:
                contract.issues.append(issue)
            contract.quality = _quality(
                contract.trigger, contract.assertions, contract.issues
            )


def discover(graph: EvidenceGraph) -> DiscoveryResult:
    graph.answers = Answers.of([path for path in graph.paths if not path.folded])
    persisted_pins = _persisted_pins(graph)
    code_contracts = _contracts(graph, persisted_pins)
    resumed, superseded = _coroutine_resume_contracts(graph, code_contracts)
    code_contracts = [item for item in code_contracts if item.id not in superseded]
    code_contracts.extend(resumed)
    contracts = _drop_unavailable_control_contracts(code_contracts)
    contracts.extend(_inventory_contracts(graph, code_contracts))
    _rewrite_unreadable_premises(graph, contracts)
    contracts = [item for item in contracts if not _contradictory(item.condition)]
    contracts.sort(key=lambda item: (item.scene or "", item.trigger.label, item.id))
    families = _branch_families(contracts)
    scenarios = _scenarios(graph, contracts)
    all_effects = [effect for path in graph.paths if not path.folded for effect in path.effects]
    assertable_effects = [effect for effect in all_effects if effect.get("category") in ASSERTABLE]
    issue_counts = Counter(issue for contract in contracts for issue in contract.issues)
    coverage = {
        "source_records": len({path.record_id for path in graph.paths}),
        "expanded_paths": len(graph.paths),
        "effects": len(all_effects),
        "assertable_effects": len(assertable_effects),
        "contracts": len(contracts),
        "contract_quality": dict(Counter(item.quality for item in contracts)),
        "branch_families": len(families),
        "connected_scenarios": len(scenarios),
        "scenario_quality": dict(Counter(item.quality for item in scenarios)),
        "issues": dict(issue_counts.most_common()),
    }
    return DiscoveryResult(
        graph.source,
        graph.capture,
        graph.build,
        list(graph.report.get("scenes") or ()),
        graph.stats(),
        contracts,
        families,
        scenarios,
        coverage,
    )
