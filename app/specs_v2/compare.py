"""Optional capture-difference diagnostics; never a final spec generator."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .discovery import discover
from .graph import load_graph
from .model import Contract, DiscoveryResult
from .render import assertion_text, condition_text, trigger_text


def _semantic(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _semantic(item) for key, item in sorted(value.items()) if key not in {"offset", "settledFrom"}}
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    return value


def contract_key(contract: Contract) -> str:
    payload = {
        "scene": contract.scene,
        "trigger": contract.trigger.identity,
        "condition": _semantic(contract.condition),
        "assertions": [item.identity for item in contract.assertions],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def compare(results: list[DiscoveryResult]) -> dict[str, Any]:
    groups: dict[str, list[tuple[DiscoveryResult, Contract]]] = defaultdict(list)
    for result in results:
        for contract in result.contracts:
            groups[contract_key(contract)].append((result, contract))
    captures = sorted({result.capture for result in results})
    rows = []
    distribution = Counter()
    for key, members in groups.items():
        seen = sorted({result.capture for result, _ in members})
        scope = "both" if seen == captures and len(captures) > 1 else "+".join(seen)
        distribution[scope] += 1
        exemplar = members[0][1]
        rows.append(
            {
                "scope": scope,
                "captures": seen,
                "scene": exemplar.scene,
                "trigger": trigger_text(exemplar.trigger),
                "condition": condition_text(exemplar.condition),
                "assertions": [assertion_text(item) for item in exemplar.assertions],
                "qualities": {result.capture: contract.quality for result, contract in members},
                "contracts": {result.capture: contract.id for result, contract in members},
            }
        )
    rows.sort(
        key=lambda row: (
            row["scope"] != "both",
            not all(value == "ready" for value in row["qualities"].values()),
            row["scene"] is None,
            -len(row["assertions"]),
            row["scene"] or "",
            row["trigger"],
        )
    )
    return {
        "sources": [
            {"capture": result.capture, "evidence": result.build.get("evidence"), "source": result.source}
            for result in results
        ],
        "counts": dict(distribution),
        "contracts": rows,
    }


def markdown(data: dict[str, Any], limit: int = 40) -> str:
    lines = [
        "# specs-v2 — capture difference diagnostics (명세 산출물 아님)",
        "",
        "이 파일은 Editor와 DevBuild의 차이를 조사하기 위한 선택적 진단 결과다. 개별 명세를 합치거나, 공통 여부로 명세를 포함·제외하지 않는다. 최종 명세는 각 입력의 `*.specs.csv`에서 독립적으로 생성된다.",
        "",
        "## 요약",
        "",
    ]
    for scope, count in data["counts"].items():
        lines.append(f"- `{scope}`: {count}")
    lines += ["", "## 대표 계약", ""]
    for row in data["contracts"][:limit]:
        lines += [
            f"### [{row['scope']}] {row['trigger']}",
            "",
            f"- 조건: {row['condition']}",
            *[f"- 판정: {item}" for item in row["assertions"]],
            f"- capture별 품질: `{row['qualities']}`",
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose semantic differences across SDK captures; does not generate specs")
    parser.add_argument("reports", nargs="+", help="Two or more SDK JSON paths")
    parser.add_argument("--out", default="out/specs-v2/cross-capture", help="Output prefix")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    results = [discover(load_graph(path)) for path in args.reports]
    data = compare(results)
    prefix = Path(args.out)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    prefix.with_suffix(".md").write_text(markdown(data, args.limit), encoding="utf-8")
    print(data["counts"])
    print(prefix.with_suffix(".json"))
    print(prefix.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
