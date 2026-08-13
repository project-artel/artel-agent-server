"""Deterministic JSON and Markdown projection for discovered contracts."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import observable
from .model import Assertion, Contract, DiscoveryResult, Scenario, SupportingState, Trigger
from .projection import (
    absorb_active_scene_condition,
    normalize_arithmetic_condition,
    project_executable_scenarios,
    project_input_cases,
    state_transition,
)


SPEC_FIELDNAMES = [
    "precondition",
    "test_step",
    "expected_result",
    "status",
    "scene",
    "ui_text",
    "ui_sprite",
    "review_reason",
    "supporting_state",
    "artifact",
    "capture",
    "build_evidence",
    "spec_id",
    "covered_spec_ids",
    "evidence",
    "contract_ids",
]


def artifact_label(result: DiscoveryResult) -> str:
    """Human-facing artifact kind without rewriting SDK capture provenance."""
    platform = str(result.build.get("platform") or "").lower()
    if result.capture == "editor":
        return "editor"
    if result.build.get("development") is True and platform.endswith("player"):
        return "devbuild"
    return result.capture


def condition_text(node: dict[str, Any]) -> str:
    return _condition_text(normalize_arithmetic_condition(node))


def human_input_label(raw: str, input_kind: str | None = None) -> str:
    """Hide SDK key phases while retaining the physical control name."""

    value = str(raw or "")
    is_key = input_kind == "key" or bool(re.search(r"\bkey:", value))
    value = re.sub(r"\bkey:", "", value)
    if is_key:
        value = re.sub(r"(?P<control>[^\s/]+):down\b", r"\g<control>", value)
        value = re.sub(r"\s*\(down\)", "", value)
    return value.replace("/", " 또는 ")


def ui_target_text(target: str | None) -> str:
    """Render only the stable hierarchy path in executable prose."""

    return f"`{target or '대상 미확정'}`"


def _ui_metadata(trigger: Trigger, assertions: list[Assertion]) -> tuple[str, str]:
    """Keep captured captions/sprites in separate target-qualified columns."""

    texts: list[str] = []
    sprites: list[str] = []
    items = [
        (trigger.target, trigger.target_label, trigger.target_sprite),
        *((item.target, item.target_label, item.target_sprite) for item in assertions),
    ]
    for target, label, sprite in items:
        if not target:
            continue
        if label:
            value = f"{target} = {label}"
            if value not in texts:
                texts.append(value)
        if sprite:
            value = f"{target} = {sprite}"
            if value not in sprites:
                sprites.append(value)
    return " / ".join(texts), " / ".join(sprites)


def _projected_supporting_states(
    scenario: Scenario,
    covered_scenarios: list[Scenario] | None = None,
) -> list[SupportingState]:
    """Return all state proven for this row, including covered callees."""

    projected = list(scenario.supporting_state)
    state_keys = {
        (
            item.target,
            item.operation,
            json.dumps(item.value, ensure_ascii=False, sort_keys=True),
            item.source.method_id,
            item.source.offset,
        )
        for item in projected
    }
    for covered_scenario in covered_scenarios or ():
        for item in covered_scenario.supporting_state:
            key = (
                item.target,
                item.operation,
                json.dumps(item.value, ensure_ascii=False, sort_keys=True),
                item.source.method_id,
                item.source.offset,
            )
            if key not in state_keys:
                projected.append(item)
                state_keys.add(key)
    return projected


def _shown(node: dict[str, Any], side: str) -> str:
    """조건 한 항을 사람이 읽을 모양으로.

    프레임 이름(`Story/<Tell>d__1.MoveNext.i`)은 같은 이름의 두 지역 변수를
    가르려고 붙인 기계용 이름이다. 계약을 구별하는 데는 필요하지만 사람이 읽는
    열에 나가면 무엇을 보라는 말인지 알 수 없는 글자가 된다. 되돌려서 코드에 적힌
    이름 그대로 보인다.

    읽을 수 없다는 사실은 여기서 말하지 않는다. `review_reason` 이 이미
    `precondition_not_observable` 로 말하고 있고, 항마다 덧붙이면 조건이 길어지기만
    한다.
    """
    value = str(node.get(side) or "?")
    frame = (node.get("localFrames") or {}).get(side)
    if frame and value.startswith(f"{frame}."):
        return value[len(frame) + 1 :]
    return value


def _condition_text(node: dict[str, Any]) -> str:
    kind = node.get("kind") or "unknown"
    if kind == "always":
        return "추가 조건 없음"
    if kind == "unknown":
        return f"SDK가 읽지 못한 조건({node.get('reason') or 'unknown'})"
    if kind == "gesture":
        return human_input_label(node.get("input") or "입력 미확정")
    if kind == "test":
        left = _shown(node, "left")
        operator = str(node.get("operator") or "?")
        right = _shown(node, "right")
        compared_tag = re.fullmatch(r"(.+)\.CompareTag\((.+)\)", left)
        if compared_tag and right in {"0", "false"} and operator in {"==", "!="}:
            semantic_operator = "!=" if operator == "==" else "=="
            return f"{compared_tag.group(1)}.tag {semantic_operator} {compared_tag.group(2)}"
        return f"{left} {operator} {right}"
    joiner = " 그리고 " if kind == "every" else " 또는 "
    parts = list(node.get("parts") or ())
    if kind == "every":
        parts.sort(
            key=lambda part: 0
            if part.get("kind") == "test" and str(part.get("right")) == "null"
            else 1
        )
    return "(" + joiner.join(_condition_text(part) for part in parts) + ")"


def _call_path_entry_type(call_paths: list[tuple[str, ...]]) -> str | None:
    owners: list[str] = []
    for path in call_paths:
        if not path:
            continue
        match = re.match(r"^\S+\s+(.+?)::[^(:]+\(", path[0])
        if match and match.group(1) not in owners:
            owners.append(match.group(1))
    if not owners:
        return None
    leaves = [owner.rsplit(".", 1)[-1].rsplit("/", 1)[-1] for owner in owners]
    labels = [
        owner if leaves.count(leaf) > 1 else leaf
        for owner, leaf in zip(owners, leaves)
    ]
    return " 또는 ".join(labels)


def trigger_text(trigger: Trigger, event_origin: str | None = None) -> str:
    if trigger.kind == "control":
        target = ui_target_text(trigger.target)
        if trigger.event == "m_OnClick":
            return f"{trigger.scene}에서 {target}를 클릭한다"
        return f"{trigger.scene}에서 {target}를 조작한다 ({trigger.event})"
    if trigger.kind == "control_check":
        target = ui_target_text(trigger.target)
        return f"{trigger.scene}에서 {target}의 표시 상태를 확인한다"
    if trigger.kind == "input":
        return f"{trigger.scene}에서 {human_input_label(trigger.target or '', trigger.input_kind)} 입력을 한다"
    if trigger.kind == "scene_entry":
        if trigger.event == "OnEnable":
            return f"{trigger.scene}에서 대상이 활성화될 때 관찰한다"
        return f"{trigger.scene}에 진입해 관찰한다"
    if trigger.kind == "pointer":
        if trigger.event == "OnEndDrag":
            return f"{trigger.scene}에서 드래그를 끝낸다"
        return f"{trigger.scene}에서 {trigger.event} 포인터 동작이 발생한다"
    if trigger.kind == "collision":
        if event_origin:
            return f"{trigger.scene}의 {event_origin}에서 {trigger.event} 충돌이 발생한다"
        return f"{trigger.scene}에서 {trigger.event} 충돌이 발생한다"
    if trigger.kind == "continuous":
        return f"{trigger.scene}에 머무르며 관찰한다"
    return f"{trigger.scene or '미확정 화면'}에서 `{trigger.event}` 이벤트 이후 관찰한다"


# What a value reads as when the evidence could not settle it. Printing `None`
# put the word into test steps as if it were the thing to look for.
UNSETTLED = "값 미확정"


# 코드 안에 그대로 적힌 값. 이것이 아니면 값 자리에 온 것은 다른 무언가의 이름이다.
_WRITTEN_DOWN = re.compile(r'^(?:-?\d+(?:\.\d+)?|".*"|true|false|null)$', re.IGNORECASE)


def _names_something(value: Any) -> bool:
    """값 자리에 온 것이 값이 아니라 다른 것의 이름인가.

    해석이 끝난 대상은 `Canvas/ChatWindow.streamingText` 처럼 경로와 멤버가
    섞이므로 모양으로 알아보기 어렵다. 리터럴인지만 보면 충분하다 — 코드에 그대로
    적힌 값이 아니면 무언가를 가리키는 이름이다.
    """
    text = str(value or "").strip()
    if not text or text == UNSETTLED:
        return False
    return not _WRITTEN_DOWN.match(text)


def assertion_text(
    assertion: Assertion,
    mode: str = "change",
    automatic: bool = False,
) -> str:
    target = ui_target_text(assertion.target)
    shown = UNSETTLED if assertion.value is None else assertion.value
    if assertion.operation == "transition":
        if automatic:
            return f"별도 입력 없이 `{assertion.value}` 화면으로 자동 전환된다"
        return f"`{assertion.value}` 화면으로 전환된다"
    if assertion.operation == "quit":
        if automatic:
            return "별도 입력 없이 게임이 자동 종료된다"
        return "게임이 종료된다"
    if assertion.operation in {"enable", "disable"}:
        if mode == "state":
            return f"{target}가 {'활성/표시' if assertion.operation == 'enable' else '비활성/숨김'} 상태다"
        return f"{target}가 {'활성/표시' if assertion.operation == 'enable' else '비활성/숨김'} 상태가 된다"
    if assertion.operation == "create":
        value = assertion.value or assertion.target or "대상 미확정"
        return f"`{value}`가 생성된다"
    if assertion.operation == "destroy":
        return f"{target}가 제거된다"
    if assertion.operation == "animate":
        match = re.fullmatch(r'SetTrigger\(\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*\)', str(assertion.value))
        if match:
            return f"{target}에서 `{match.group(1)}` 애니메이션 트리거가 실행된다"
        return f"{target}의 애니메이션이 `{shown}`가 된다"
    if assertion.operation == "display":
        # 값 자리에 필드 이름이 온 경우가 있다. `chatText 의 표시 값이
        # streamingText 로 갱신된다` 는 `streamingText` 라는 글자가 화면에
        # 나온다는 말로 읽히지만, 뜻은 두 값이 같아진다는 것이다. 값이 아니라
        # 관계이므로 관계로 쓴다.
        if _names_something(shown):
            if mode == "state":
                return f"{target}의 표시 값이 `{shown}`와 같다"
            return f"{target}의 표시 값이 `{shown}`와 같아진다"
        if mode == "state":
            return f"{target}의 표시 값이 `{shown}`로 출력되어 있다"
        return f"{target}의 표시 값이 `{shown}`로 갱신된다"
    if assertion.operation == "transform":
        if _names_something(shown):
            if mode == "state":
                return f"{target}가 `{shown}`와 같은 위치/형태에 있다"
            return f"{target}가 `{shown}`와 같은 위치/형태가 된다"
        if mode == "state":
            return f"{target}가 `{shown}` 위치/형태에 있다"
        return f"{target}가 `{shown}` 위치/형태로 바뀐다"
    if assertion.operation == "play":
        return f"{target}에서 `{shown}` 오디오가 재생된다"
    return f"{target}에 {assertion.operation} `{shown}`가 적용된다"


def state_text(state: SupportingState) -> str:
    return f"`{state.target or '대상 미확정'}` {state.operation} `{state.value}`"


def precondition_text(
    scene: str | None,
    condition: dict[str, Any],
    entering_scene: bool = False,
) -> str:
    scene_state = f"{scene} 화면인 상태" if scene else "화면 미확정 상태"
    projected = absorb_active_scene_condition(condition, scene)
    if entering_scene:
        if projected.get("kind") == "always":
            return "추가 사전 조건 없음"
        return condition_text(projected)
    if projected.get("kind") == "always":
        return scene_state
    return f"{scene_state} / {condition_text(projected)}"


def is_direct_lifecycle_action(
    trigger: Trigger,
    assertions: list[Assertion],
) -> bool:
    """Whether Start/Awake directly performs an automatic transition or quit."""

    if trigger.kind != "scene_entry" or trigger.event not in {"Start", "Awake"}:
        return False
    actions = [
        assertion
        for assertion in assertions
        if assertion.operation in {"transition", "quit"}
    ]
    if not actions:
        return False
    return all(
        len(assertion.source.method_id.split("|")) >= 3
        and assertion.source.method_id.split("|")[2] == trigger.event
        for assertion in actions
    )


def test_step_text(
    trigger: Trigger,
    assertions: list[Assertion],
    input_target: str | None = None,
    automatic_lifecycle: bool = False,
    event_origin: str | None = None,
) -> str:
    if automatic_lifecycle:
        return f"{trigger.scene or '미확정 화면'}에 진입한다"
    if (
        trigger.kind == "runtime_event"
        and any("|MoveNext|" in item.source.method_id for item in assertions)
    ):
        return f"{trigger.scene or '미확정 화면'}에 머무르며 진행 결과를 관찰한다"
    if (
        trigger.kind in {"scene_entry", "continuous", "control_check"}
        and assertions
        and all(item.operation in {"enable", "disable"} for item in assertions)
    ):
        targets = " / ".join(
            ui_target_text(item.target)
            for item in assertions
        )
        return f"{targets} 표시 상태를 확인한다"
    if trigger.kind == "input" and input_target is not None:
        return f"{trigger.scene}에서 {human_input_label(input_target, trigger.input_kind)} 입력을 한다"
    return trigger_text(trigger, event_origin)


def _scenario_lines(scenario: Scenario, contracts: dict[str, Contract]) -> list[str]:
    first = contracts[scenario.contracts[0]]
    automatic_lifecycle = is_direct_lifecycle_action(
        scenario.trigger,
        scenario.assertions,
    )
    lines = [
        f"### {scenario.title}",
        "",
        f"- 품질: `{scenario.quality}`",
        f"- 실행 축: actionability=`{scenario.actionability}` · observability=`{scenario.observability}` · applicability=`{scenario.applicability}`",
        f"- 사전 조건: {precondition_text(scenario.scene, first.condition, automatic_lifecycle)}",
        f"- 시작: {test_step_text(scenario.trigger, scenario.assertions, automatic_lifecycle=automatic_lifecycle, event_origin=_call_path_entry_type(scenario.call_paths))}",
    ]
    if scenario.supporting_state:
        lines.append("- 연결 상태:")
        lines.extend(f"  - {state_text(item)}" for item in scenario.supporting_state)
    lines.append("- 판정:")
    lines.extend(
        f"  - {assertion_text(item, automatic=automatic_lifecycle)} (`{item.resolution}`, `{item.source.method_id}`@{item.source.offset})"
        for item in scenario.assertions
    )
    lines.append("- 호출 경로:")
    for path in scenario.call_paths:
        lines.append("  - " + " → ".join(f"`{part.split('::')[-1]}`" for part in path))
    if scenario.issues:
        lines.append("- 검토 사유: " + ", ".join(f"`{item}`" for item in scenario.issues))
    lines.append("")
    return lines


def _scenario_row(
    result: DiscoveryResult,
    scenario: Scenario,
    contracts: dict[str, Contract],
    assertions: list[Assertion] | None = None,
    row_suffix: str | None = None,
    condition_override: dict[str, Any] | None = None,
    input_target: str | None = None,
    covered_scenarios: list[Scenario] | None = None,
) -> dict[str, str]:
    first = contracts[scenario.contracts[0]]
    selected = assertions or scenario.assertions
    covered = covered_scenarios or []
    projected_states = _projected_supporting_states(scenario, covered)
    projected_contract_ids = list(
        dict.fromkeys(
            [
                *scenario.contracts,
                *(contract_id for item in covered for contract_id in item.contracts),
            ]
        )
    )
    if assertions is not None:
        source_refs = list(
            dict.fromkeys(
                f"{item.source.method_id}@{item.source.offset if item.source.offset is not None else '?'}"
                for item in selected
            )
        )
    else:
        source_refs = list(
            dict.fromkeys(
                f"{ref.method_id}@{ref.offset if ref.offset is not None else '?'}"
                for contract_id in projected_contract_ids
                for ref in contracts[contract_id].source_refs
            )
        )
    assertion_mode = (
        "state"
        if scenario.trigger.kind in {"scene_entry", "continuous", "control_check"}
        else "change"
    )
    automatic_lifecycle = is_direct_lifecycle_action(scenario.trigger, selected)
    metadata_assertions = [
        *selected,
        *(assertion for item in covered for assertion in item.assertions),
    ]
    ui_text, ui_sprite = _ui_metadata(scenario.trigger, metadata_assertions)
    return {
        "artifact": artifact_label(result),
        "capture": result.capture,
        "build_evidence": str(result.build.get("evidence") or ""),
        "spec_id": scenario.id if row_suffix is None else f"{scenario.id}:{row_suffix}",
        "status": scenario.quality,
        "scene": scenario.scene or "미확정",
        "ui_text": ui_text,
        "ui_sprite": ui_sprite,
        "precondition": precondition_text(
            scenario.scene,
            first.condition if condition_override is None else condition_override,
            automatic_lifecycle,
        ),
        "test_step": test_step_text(
            scenario.trigger,
            selected,
            input_target,
            automatic_lifecycle,
            _call_path_entry_type(scenario.call_paths),
        ),
        "expected_result": " / ".join(
            assertion_text(item, assertion_mode, automatic_lifecycle)
            for item in selected
        ),
        "supporting_state": " / ".join(state_text(item) for item in projected_states),
        "evidence": " / ".join(source_refs),
        "review_reason": " / ".join(scenario.issues),
        "covered_spec_ids": " / ".join(item.id for item in covered),
        "contract_ids": " / ".join(projected_contract_ids),
    }


def _scenario_rows(
    result: DiscoveryResult,
    scenario: Scenario,
    contracts: dict[str, Contract],
    covered_scenarios: list[Scenario] | None = None,
) -> list[dict[str, str]]:
    if (
        scenario.trigger.kind in {"scene_entry", "continuous", "control_check"}
        and scenario.assertions
        and all(item.operation in {"enable", "disable"} for item in scenario.assertions)
    ):
        return [
            _scenario_row(
                result,
                scenario,
                contracts,
                [assertion],
                str(index + 1),
                covered_scenarios=covered_scenarios,
            )
            for index, assertion in enumerate(scenario.assertions)
        ]
    if scenario.trigger.kind == "input":
        first = contracts[scenario.contracts[0]]
        projections = project_input_cases(first.condition, scenario.trigger.target or "입력 미확정")
        return [
            _scenario_row(
                result,
                scenario,
                contracts,
                row_suffix=f"input-{index + 1}" if len(projections) > 1 else None,
                condition_override=projection.condition,
                input_target=projection.label,
                covered_scenarios=covered_scenarios,
            )
            for index, projection in enumerate(projections)
        ]
    return [
        _scenario_row(
            result,
            scenario,
            contracts,
            covered_scenarios=covered_scenarios,
        )
    ]


def _base_spec_id(spec_id: str) -> str:
    parts = spec_id.split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else spec_id


def _spec_rows(
    result: DiscoveryResult,
    scenarios: list[Scenario],
    contracts: dict[str, Contract],
    covered_by: dict[str, list[Scenario]] | None = None,
) -> list[dict[str, str]]:
    covered_by = covered_by or {}
    scene_rank = {scene: index for index, scene in enumerate(result.scenes)}
    scenario_rank = {scenario.id: index for index, scenario in enumerate(scenarios)}

    status_rank = {"ready": 0, "candidate": 1, "review": 2, "unsupported": 3}

    def sort_key(scenario: Scenario) -> tuple[int, str, int, int, int]:
        observation_first = 0 if scenario.trigger.kind in {
            "scene_entry",
            "continuous",
            "control_check",
        } else 1
        scene = scenario.scene or "미확정"
        return (
            scene_rank.get(scene, len(scene_rank)),
            scene,
            status_rank[scenario.quality],
            observation_first,
            scenario_rank[scenario.id],
        )

    ordered_rows = [
        row
        for scenario in sorted(scenarios, key=sort_key)
        for row in _scenario_rows(
            result,
            scenario,
            contracts,
            covered_scenarios=covered_by.get(scenario.id),
        )
    ]
    original_rank = {id(row): index for index, row in enumerate(ordered_rows)}
    ordered_rows.sort(
        key=lambda row: (
            scene_rank.get(row["scene"], len(scene_rank)),
            row["scene"],
            status_rank[row["status"]],
            original_rank[id(row)],
        )
    )
    return ordered_rows


def _write_spec_csv(
    result: DiscoveryResult,
    path: Path,
    scenarios: list[Scenario],
    contracts: dict[str, Contract],
    covered_by: dict[str, list[Scenario]] | None = None,
) -> None:
    rows = _spec_rows(
        result,
        scenarios,
        contracts,
        covered_by,
    )
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SPEC_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def markdown(result: DiscoveryResult, limit: int = 30) -> str:
    quality = result.coverage.get("contract_quality") or {}
    scenario_quality = result.coverage.get("scenario_quality") or {}
    lines = [
        f"# specs-v2 — {Path(result.source).name}",
        "",
        f"> 이 문서는 `{Path(result.source).name}` 하나만 분석해 생성한 `{artifact_label(result)}` 전용 명세다. 다른 capture의 공통 여부나 결과를 품질 판정에 사용하지 않는다.",
        "",
        f"- artifact: `{artifact_label(result)}`",
        f"- capture: `{result.capture}`",
        f"- build evidence: `{result.build.get('evidence')}`",
        f"- graph: {result.graph_stats['nodes']} nodes / {result.graph_stats['edges']} edges / {result.graph_stats['paths']} expanded paths",
        f"- contracts: {result.coverage['contracts']} (ready {quality.get('ready', 0)} / candidate {quality.get('candidate', 0)} / review {quality.get('review', 0)} / unsupported {quality.get('unsupported', 0)})",
        f"- branch families: {result.coverage['branch_families']}",
        f"- connected scenarios: {result.coverage['connected_scenarios']} (ready {scenario_quality.get('ready', 0)} / candidate {scenario_quality.get('candidate', 0)} / review {scenario_quality.get('review', 0)} / unsupported {scenario_quality.get('unsupported', 0)})",
        "",
        "## 발견 방식",
        "",
        "단일 레코드를 문장으로 옮기지 않는다. 화면 배선/입력에서 순방향으로, 관찰 가능한 effect에서 역방향으로 따라가 같은 entry와 call path에서 만난 근거를 contract로 만든다. 여러 contract는 조건 arm을 보존한 채 branch family 또는 connected scenario로만 연결한다.",
        "",
        "## 연결 시나리오",
        "",
    ]
    contract_index = {item.id: item for item in result.contracts}
    shown = result.scenarios[:limit]
    for scenario in shown:
        lines.extend(_scenario_lines(scenario, contract_index))
    if len(result.scenarios) > len(shown):
        lines += [f"_나머지 {len(result.scenarios) - len(shown)}개 시나리오는 JSON에 보존됨._", ""]

    lines += ["## 분기 family", "", "| 화면 | 시작 | 기능 | arm | 품질 |", "|---|---|---|---:|---|"]
    for family in result.branch_families[:limit]:
        lines.append(
            f"| {family.scene or '미확정'} | {family.trigger.label} | `{family.feature}` | {len(family.arms)} | {family.quality} |"
        )
    lines += ["", "## 주요 검토 사유", ""]
    for issue, count in list((result.coverage.get("issues") or {}).items())[:20]:
        lines.append(f"- `{issue}`: {count}")
    lines.append("")
    return "\n".join(lines)


def project_rows(
    result: DiscoveryResult,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Project one discovery result without filesystem side effects."""

    contracts = {item.id: item for item in result.contracts}
    visible_scenarios, covered_by = project_executable_scenarios(
        [item for item in result.scenarios if item.quality in {"ready", "candidate"}],
        contracts,
    )
    ready_rows = _spec_rows(
        result,
        visible_scenarios,
        contracts,
        covered_by,
    )
    review_rows = _spec_rows(
        result,
        [item for item in result.scenarios if item.quality not in {"ready", "candidate"}],
        contracts,
    )
    return ready_rows, review_rows


def write_outputs(
    result: DiscoveryResult,
    prefix: str | Path,
    limit: int = 30,
) -> tuple[Path, Path, Path, Path, Path]:
    base = Path(prefix)
    base.parent.mkdir(parents=True, exist_ok=True)
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    specs_path = base.with_suffix(".specs.csv")
    review_path = base.with_suffix(".review.csv")
    json_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown(result, limit=limit), encoding="utf-8")
    ready_rows, review_rows = project_rows(result)
    with specs_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SPEC_FIELDNAMES)
        writer.writeheader()
        writer.writerows(ready_rows)
    with review_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SPEC_FIELDNAMES)
        writer.writeheader()
        writer.writerows(review_rows)
    return json_path, md_path, specs_path, review_path
