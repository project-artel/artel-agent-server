"""CLI: python -m app.specs_v2.cli <report.json> [--out prefix]."""

from __future__ import annotations

import argparse
from pathlib import Path

from .discovery import discover
from .graph import load_graph
from .render import artifact_label, write_outputs


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Discover connected specs from an Artel SDK report")
    value.add_argument("report", help="SDK JSON path")
    value.add_argument("--out", help="Output prefix without extension")
    value.add_argument("--limit", type=int, default=30, help="Maximum scenarios shown in Markdown")
    return value


def main() -> int:
    args = parser().parse_args()
    graph = load_graph(args.report)
    result = discover(graph)
    prefix = args.out or str(Path("out/specs-v2") / Path(args.report).stem)
    json_path, md_path, specs_path, review_path, flows_path = write_outputs(
        result,
        prefix,
        limit=args.limit,
    )
    coverage = result.coverage
    print(f"artifact={artifact_label(result)} capture={result.capture} evidence={result.build.get('evidence')}")
    print(f"graph={result.graph_stats['nodes']} nodes/{result.graph_stats['edges']} edges")
    print(f"contracts={coverage['contracts']} {coverage['contract_quality']}")
    print(f"families={coverage['branch_families']} scenarios={coverage['connected_scenarios']} {coverage['scenario_quality']}")
    print(json_path)
    print(md_path)
    print(specs_path)
    print(review_path)
    print(flows_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
