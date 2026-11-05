"""Lossless, game-agnostic projection helpers for human-facing specs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .model import Contract, Scenario


ALWAYS = {"kind": "always"}
ACTIVE_SCENE_NAME = re.compile(
    r"^(?:UnityEngine\.SceneManagement\.)?SceneManager\.GetActiveScene\(\)\.name$"
)


@dataclass(frozen=True)
class InputProjection:
    """One executable input case after preserving boolean arm relationships."""

    condition: dict[str, Any]
    alternatives: tuple[str, ...]

    @property
    def label(self) -> str:
        return " 또는 ".join(self.alternatives)


def _semantic(value: Any) -> Any:
    if isinstance(value, dict):
        kind = value.get("kind")
        items = {
            key: _semantic(item)
            for key, item in value.items()
            if key
            not in {
                "offset",
                "settledFrom",
                "projectedFromPostResume",
                "projectedDelta",
                "arithmeticDomain",
                "arithmeticProof",
            }
        }
        if kind in {"every", "either"}:
            items["parts"] = sorted(
                items.get("parts") or (),
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
            )
        return items
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    return value


def condition_key(condition: dict[str, Any]) -> str:
    return json.dumps(_semantic(condition), ensure_ascii=False, sort_keys=True)


def simplify_condition(condition: dict[str, Any]) -> dict[str, Any]:
    kind = condition.get("kind") or "unknown"
    if kind not in {"every", "either"}:
        return dict(condition)

    parts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in condition.get("parts") or ():
        part = simplify_condition(raw)
        if part.get("kind") == kind:
            candidates = part.get("parts") or ()
        else:
            candidates = (part,)
        for candidate in candidates:
            if candidate.get("kind") == "always":
                if kind == "either":
                    return dict(ALWAYS)
                continue
            signature = condition_key(candidate)
            if signature in seen:
                continue
            seen.add(signature)
            parts.append(candidate)

    if not parts:
        return dict(ALWAYS)
    if kind == "every":
        equalities = {
            (str(part.get("left") or ""), str(part.get("right") or ""))
            for part in parts
            if part.get("kind") == "test" and part.get("operator") == "=="
        }
        parts = [
            part
            for part in parts
            if not (
                part.get("kind") == "test"
                and part.get("operator") == "!="
                and any(
                    left == str(part.get("left") or "")
                    and value != str(part.get("right") or "")
                    for left, value in equalities
                )
            )
        ]
    if len(parts) == 1:
        return parts[0]
    return {"kind": kind, "parts": parts}


def normalize_arithmetic_condition(condition: dict[str, Any]) -> dict[str, Any]:
    """Collapse proven unit-step boundary relations without domain naming."""

    kind = condition.get("kind") or "unknown"
    if kind not in {"every", "either"}:
        return dict(condition)
    parts = [
        normalize_arithmetic_condition(part)
        for part in condition.get("parts") or ()
    ]
    if kind != "every":
        return simplify_condition({**condition, "parts": parts})

    consumed: set[int] = set()
    replacements: dict[int, dict[str, Any]] = {}
    for projected_index, projected in enumerate(parts):
        if projected_index in consumed:
            continue
        if projected.get("kind") != "test":
            continue
        subject = projected.get("projectedFromPostResume")
        delta = projected.get("projectedDelta")
        bound = projected.get("right")
        if (
            not subject
            or delta not in {-1, 1}
            or projected.get("arithmeticDomain") != "integer"
        ):
            continue
        for context_index, context in enumerate(parts):
            if (
                context_index == projected_index
                or context_index in consumed
                or context.get("kind") != "test"
            ):
                continue
            if context.get("left") != subject or context.get("right") != bound:
                continue
            context_operator = context.get("operator")
            projected_operator = projected.get("operator")
            replacement_operator: str | None = None
            shift_operator: str | None = None
            if delta == 1 and context_operator == "<":
                if projected_operator == ">=":
                    replacement_operator = "=="
                    shift_operator = "-"
                elif projected_operator == "<":
                    replacement_operator = "<"
                    shift_operator = "-"
            elif delta == -1 and context_operator == ">":
                if projected_operator == "<=":
                    replacement_operator = "=="
                    shift_operator = "+"
                elif projected_operator == ">":
                    replacement_operator = ">"
                    shift_operator = "+"
            if replacement_operator is None or shift_operator is None:
                continue
            replacements[min(context_index, projected_index)] = {
                "kind": "test",
                "left": subject,
                "operator": replacement_operator,
                "right": f"({bound} {shift_operator} 1)",
                "context": context.get("context"),
                "arithmeticProof": "integer-unit-step-boundary",
            }
            consumed.update({context_index, projected_index})
            break

    normalized = [
        replacements[index]
        for index in sorted(replacements)
    ]
    normalized.extend(
        part for index, part in enumerate(parts) if index not in consumed
    )
    return simplify_condition({**condition, "parts": normalized})


def absorb_active_scene_condition(
    condition: dict[str, Any],
    scene: str | None,
) -> dict[str, Any]:
    """Consume a matching active-scene equality in a human scene precondition.

    Only conjunctive predicates are absorbed. An equality inside an OR arm does
    not establish that the row's scene is active, so the OR expression remains
    intact. Raw contract conditions are never mutated.
    """

    if scene is None:
        return dict(condition)
    kind = condition.get("kind") or "unknown"
    if kind == "test":
        left = str(condition.get("left") or "").strip()
        expected = str(condition.get("right") or "").strip().strip('"')
        if (
            ACTIVE_SCENE_NAME.fullmatch(left)
            and condition.get("operator") == "=="
            and expected == scene
        ):
            return dict(ALWAYS)
        return dict(condition)
    if kind != "every":
        return dict(condition)

    parts = [
        (
            dict(part)
            if part.get("kind") == "either"
            else absorb_active_scene_condition(part, scene)
        )
        for part in condition.get("parts") or ()
    ]
    return simplify_condition({**condition, "parts": parts})


def _input_label(raw: str) -> str:
    match = re.fullmatch(r"(?:key|mouse):([^ ]+)(?: \(([^)]+)\))?", raw.strip())
    if not match:
        return raw.strip()
    control, phase = match.groups()
    return f"{control}:{phase}" if phase else control


def _dnf_input_cases(
    condition: dict[str, Any],
    *,
    max_cases: int,
) -> list[tuple[tuple[str, ...], dict[str, Any]]] | None:
    kind = condition.get("kind") or "unknown"
    if kind == "gesture":
        value = _input_label(str(condition.get("input") or ""))
        return [((value,) if value else (), dict(ALWAYS))]
    if kind not in {"every", "either"}:
        return [((), dict(condition))]

    children = [
        _dnf_input_cases(part, max_cases=max_cases)
        for part in condition.get("parts") or ()
    ]
    if any(child is None for child in children):
        return None

    if kind == "either":
        cases = [case for child in children for case in (child or ())]
        return cases if len(cases) <= max_cases else None

    cases: list[tuple[tuple[str, ...], dict[str, Any]]] = [((), dict(ALWAYS))]
    for child in children:
        combined: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        for left_inputs, left_condition in cases:
            for right_inputs, right_condition in child or ():
                inputs = tuple(dict.fromkeys((*left_inputs, *right_inputs)))
                residual = simplify_condition(
                    {"kind": "every", "parts": [left_condition, right_condition]}
                )
                combined.append((inputs, residual))
                if len(combined) > max_cases:
                    return None
        cases = combined
    return cases


def project_input_cases(
    condition: dict[str, Any],
    fallback_input: str,
    *,
    max_cases: int = 32,
) -> list[InputProjection]:
    """Remove promoted gestures without creating invalid input/guard cross-products."""

    cases = _dnf_input_cases(condition, max_cases=max_cases)
    if not cases or any(not inputs for inputs, _ in cases):
        return [InputProjection(dict(condition), (fallback_input,))]

    grouped: dict[str, tuple[dict[str, Any], list[str]]] = {}
    order: list[str] = []
    for inputs, residual in cases:
        signature = condition_key(residual)
        chord = " + ".join(inputs)
        if signature not in grouped:
            grouped[signature] = (residual, [])
            order.append(signature)
        alternatives = grouped[signature][1]
        if chord not in alternatives:
            alternatives.append(chord)
    return [
        InputProjection(grouped[key][0], tuple(grouped[key][1]))
        for key in order
    ]


def condition_leaves(condition: dict[str, Any]):
    if condition.get("kind") in {"every", "either"}:
        for part in condition.get("parts") or ():
            yield from condition_leaves(part)
    else:
        yield condition


def _same_subject(left: str, right: str) -> bool:
    if left == right:
        return True
    left_leaf = left.rsplit(".", 1)[-1]
    right_leaf = right.rsplit(".", 1)[-1]
    return left_leaf == right_leaf and ("." not in left or "." not in right)


def equality_value(condition: dict[str, Any], subject: str) -> str | None:
    values = []
    for leaf in condition_leaves(condition):
        if (
            leaf.get("kind") == "test"
            and leaf.get("operator") == "=="
            and _same_subject(str(leaf.get("left") or ""), subject)
        ):
            value = str(leaf.get("right") or "")
            if value not in values:
                values.append(value)
    return values[0] if len(values) == 1 else None


def state_transition(
    target: str,
    update: Any,
    condition: dict[str, Any],
) -> tuple[str, str]:
    """Describe a supported state write without guessing domain-specific meaning."""

    current = equality_value(condition, target)
    update_text = str(update)
    if current is not None:
        before = f"{target}={current}"
        match = re.fullmatch(r"([+-])(\d+)", update_text)
        if match and re.fullmatch(r"-?\d+", current):
            delta = int(match.group(2)) * (1 if match.group(1) == "+" else -1)
            return before, f"{target}={int(current) + delta}"
        return before, f"{target}={update_text}"

    source_value = equality_value(condition, update_text)
    if source_value is not None:
        return f"{update_text}={source_value}", f"{target}={source_value}"
    return "", f"{target}={update_text}"


def _assertion_fingerprint(scenario: Scenario) -> frozenset[tuple[Any, ...]]:
    return frozenset(
        (
            *assertion.identity,
            assertion.source.method_id,
            assertion.source.offset,
        )
        for assertion in scenario.assertions
    )


def _state_fingerprint(scenario: Scenario) -> frozenset[tuple[str, str, str, str, int | None]]:
    return frozenset(
        (
            state.target or "",
            state.operation,
            json.dumps(state.value, ensure_ascii=False, sort_keys=True),
            state.source.method_id,
            state.source.offset,
        )
        for state in scenario.supporting_state
    )


def _method_identity_from_id(method_id: str) -> tuple[str, str] | None:
    parts = method_id.split("|")
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def _method_identity_from_signature(signature: str) -> tuple[str, str] | None:
    match = re.match(r"^\S+\s+(.+?)::([^(:]+)\(", signature)
    if not match:
        return None
    return match.group(1), match.group(2)


def _supporting_state_is_covered(caller: Scenario, callee: Scenario) -> bool:
    """Accept callee state proven on every caller path through its source method."""

    caller_states = _state_fingerprint(caller)
    path_methods = [
        {
            identity
            for signature in path
            if (identity := _method_identity_from_signature(signature)) is not None
        }
        for path in caller.call_paths
    ]
    if not path_methods:
        return not callee.supporting_state
    for state in callee.supporting_state:
        fingerprint = (
            state.target or "",
            state.operation,
            json.dumps(state.value, ensure_ascii=False, sort_keys=True),
            state.source.method_id,
            state.source.offset,
        )
        if fingerprint in caller_states:
            continue
        source_method = _method_identity_from_id(state.source.method_id)
        if source_method is None or not all(source_method in methods for methods in path_methods):
            return False
    return True


def _residual_condition_keys(
    scenario: Scenario,
    contracts: dict[str, Contract],
) -> frozenset[str]:
    if not scenario.contracts:
        return frozenset()
    condition = contracts[scenario.contracts[0]].condition
    if scenario.trigger.kind != "input":
        return frozenset({condition_key(simplify_condition(condition))})
    return frozenset(
        condition_key(simplify_condition(case.condition))
        for case in project_input_cases(condition, scenario.trigger.target or "입력 미확정")
    )


def _paths_cover(actionable: Scenario, indirect: Scenario) -> bool:
    if not actionable.call_paths or not indirect.call_paths:
        return False
    covers = all(
        any(
            len(parent) >= len(child)
            and tuple(parent[-len(child):]) == tuple(child)
            for parent in actionable.call_paths
        )
        for child in indirect.call_paths
        if child
    ) and all(indirect.call_paths)
    strictly_longer = any(
        len(parent) > len(child)
        and tuple(parent[-len(child):]) == tuple(child)
        for parent in actionable.call_paths
        for child in indirect.call_paths
        if child
    )
    if covers and strictly_longer:
        return True

    # Two sibling entries can still be the same human-facing behavior when
    # they converge on the exact method/offset that produced the assertion.
    # The assertion fingerprint, scene, capture, state, and condition checks
    # in project_executable_scenarios remain mandatory; this only recognizes
    # the shared effect tail instead of requiring one whole path to be a
    # suffix of the other.
    if actionable.trigger.kind not in {"control", "input", "pointer"}:
        return False
    return all(
        any(parent[-1] == child[-1] for parent in actionable.call_paths if parent)
        for child in indirect.call_paths
        if child
    ) and all(indirect.call_paths)


def _condition_test_is_covered(
    caller_leaf: dict[str, Any],
    callee_leaf: dict[str, Any],
) -> bool:
    if caller_leaf.get("kind") != "test" or callee_leaf.get("kind") != "test":
        return condition_key(caller_leaf) == condition_key(callee_leaf)
    if (
        caller_leaf.get("operator") != callee_leaf.get("operator")
        or str(caller_leaf.get("right") or "")
        != str(callee_leaf.get("right") or "")
    ):
        return False
    caller_left = str(caller_leaf.get("left") or "")
    callee_left = str(callee_leaf.get("left") or "")
    if caller_left.casefold() == callee_left.casefold():
        return True
    if "." not in callee_left:
        return False
    # A callee field can be rebound through the caller receiver, for example
    # CombineZone.magicTypeCards.Count ->
    # DraggableCard.combineZone.magicTypeCards.Count.  Compare the property
    # tail only after the callee owner and retain operator/value equality.
    callee_tail = callee_left.split(".", 1)[1]
    return caller_left.casefold().endswith(f".{callee_tail}".casefold())


def _conjunctive_condition_is_covered(
    caller_condition: dict[str, Any],
    callee_condition: dict[str, Any],
) -> bool:
    caller = simplify_condition(caller_condition)
    callee = simplify_condition(callee_condition)
    if caller.get("kind") == "either" or callee.get("kind") == "either":
        return False
    caller_leaves = list(condition_leaves(caller))
    callee_leaves = list(condition_leaves(callee))
    return bool(callee_leaves) and all(
        any(_condition_test_is_covered(caller_leaf, callee_leaf) for caller_leaf in caller_leaves)
        for callee_leaf in callee_leaves
    )


def _callee_condition_is_covered(
    caller: Scenario,
    callee: Scenario,
    contracts: dict[str, Contract],
) -> bool:
    """Treat an unproven callee's ``always`` as missing caller context."""

    callee_conditions = _residual_condition_keys(callee, contracts)
    if callee_conditions == {condition_key({"kind": "always"})}:
        caller_conditions = _residual_condition_keys(caller, contracts)
        return (
            caller_conditions == callee_conditions
            or caller.trigger.kind == "runtime_event"
            and caller.actionability == "indirect"
        )
    caller_conditions = _residual_condition_keys(caller, contracts)
    if callee_conditions.issubset(caller_conditions):
        return True
    if caller.trigger.kind == "input" or callee.trigger.kind == "input":
        return False
    caller_condition = contracts[caller.contracts[0]].condition
    callee_condition = contracts[callee.contracts[0]].condition
    return _conjunctive_condition_is_covered(caller_condition, callee_condition)


def project_executable_scenarios(
    scenarios: list[Scenario],
    contracts: dict[str, Contract],
) -> tuple[list[Scenario], dict[str, list[Scenario]]]:
    """Suppress unbound runtime entries already covered by an executable path.

    Discovery remains lossless. This function only chooses the human-facing
    Ready/candidate projection and returns suppressed scenarios as provenance
    attached to every path of equal-or-higher quality that proves it reaches
    the same behavior. A candidate must never suppress a ready scenario.
    """

    visible_qualities = {"ready", "candidate"}
    quality_rank = {"ready": 0, "candidate": 1}
    dominators = [
        scenario
        for scenario in scenarios
        if scenario.quality in visible_qualities
        and scenario.trigger.kind
        not in {"scene_entry", "continuous", "control_check", "unreached"}
    ]
    covered_by: dict[str, list[Scenario]] = {}
    suppressed: set[str] = set()
    trigger_rank = {
        "control": 0,
        "input": 1,
        "pointer": 2,
        "collision": 3,
        "runtime_event": 4,
    }

    for indirect in scenarios:
        if not (
            indirect.quality in visible_qualities
            and indirect.trigger.kind == "runtime_event"
            and indirect.actionability == "indirect"
            and indirect.trigger.resolution == "derived"
            and not indirect.trigger.proof
        ):
            continue
        candidates = [
            candidate
            for candidate in dominators
            if candidate.id != indirect.id
            if quality_rank[candidate.quality] <= quality_rank[indirect.quality]
            if candidate.contracts
            and indirect.contracts
            and contracts[candidate.contracts[0]].capture
            == contracts[indirect.contracts[0]].capture
            and candidate.scene == indirect.scene
            and _assertion_fingerprint(candidate) == _assertion_fingerprint(indirect)
            and _callee_condition_is_covered(candidate, indirect, contracts)
            and _paths_cover(candidate, indirect)
            and _supporting_state_is_covered(candidate, indirect)
        ]
        if not candidates:
            continue
        candidates.sort(
            key=lambda candidate: (
                trigger_rank.get(candidate.trigger.kind, len(trigger_rank)),
                min((len(path) for path in candidate.call_paths), default=10**9),
                candidate.id,
            )
        )
        for candidate in candidates:
            covered = covered_by.setdefault(candidate.id, [])
            if indirect not in covered:
                covered.append(indirect)
        suppressed.add(indirect.id)

    return (
        [scenario for scenario in scenarios if scenario.id not in suppressed],
        covered_by,
    )
