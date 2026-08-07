"""Stateless application service for one SDK report at a time."""

from __future__ import annotations

from typing import Any

from .discovery import discover
from .graph import graph_from_report
from .render import artifact_label, project_rows


def generate_spec_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Discover and project specs from exactly one SDK capture.

    The caller is responsible for invoking this function once per Editor or
    development-build report.  No other capture, filesystem state, database,
    or model output participates in the result.
    """

    result = discover(graph_from_report(report, source="internal-request"))
    ready_rows, review_rows, flow_rows = project_rows(result)
    raw_evidence = result.build.get("evidence")

    return {
        "schema_version": "spec-discovery.v2",
        "artifact": artifact_label(result),
        "capture": result.capture,
        "build_evidence": str(raw_evidence) if raw_evidence is not None else None,
        "summary": {
            "graph_nodes": result.graph_stats["nodes"],
            "graph_edges": result.graph_stats["edges"],
            "contracts": result.coverage["contracts"],
            "connected_scenarios": result.coverage["connected_scenarios"],
            "ready_specs": sum(row["status"] == "ready" for row in ready_rows),
            "candidate_specs": sum(
                row["status"] == "candidate" for row in ready_rows
            ),
            "review_specs": sum(row["status"] == "review" for row in review_rows),
            "unsupported_specs": sum(
                row["status"] == "unsupported" for row in review_rows
            ),
            "connected_flows": len(flow_rows),
        },
        "ready_specs": ready_rows,
        "review_specs": review_rows,
        "connected_flows": flow_rows,
    }
