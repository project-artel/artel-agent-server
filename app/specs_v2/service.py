"""Stateless application service for one SDK report at a time."""

from __future__ import annotations

from typing import Any

from app.config import get_settings

from .discovery import discover
from .graph import graph_from_report
from .render import artifact_label, project_rows
from .testcase import SCHEMA_VERSION as TEST_CASE_SCHEMA
from .testcase import test_cases


def generate_spec_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Discover and project specs from exactly one SDK capture.

    The caller is responsible for invoking this function once per Editor or
    development-build report.  No other capture, filesystem state, database,
    or model output participates in the result.
    """

    result = discover(graph_from_report(report, source="internal-request"))
    ready_rows, review_rows = project_rows(result)
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
        },
        "ready_specs": ready_rows,
        "review_specs": review_rows,
    }


def generate_test_cases(report: dict[str, Any]) -> dict[str, Any]:
    """The same discovery, reshaped as `test-case.v1` records.

    Runs the discovery rather than reshaping `generate_spec_payload`'s output:
    `used_step_indexes` and `scene_key` are read off the scenarios, and those do
    not survive projection into flat rows.

    Review and unsupported specs are included. A row that cannot be run is still
    a fact about the game, and dropping it here would hide it from whoever
    decides what to do about it — the status says which is which.
    """

    result = discover(graph_from_report(report, source="internal-request"))
    ready_rows, review_rows = project_rows(result)
    settings = get_settings()
    cases = test_cases(
        result,
        [*ready_rows, *review_rows],
        prompt_version=settings.spec_prompt_version,
        llm_model=settings.spec_model,
    )
    raw_evidence = result.build.get("evidence")

    return {
        "schema_version": TEST_CASE_SCHEMA,
        "artifact": artifact_label(result),
        "capture": result.capture,
        "build_evidence": str(raw_evidence) if raw_evidence is not None else None,
        "counts": {
            status: sum(case["spec"]["status"] == status for case in cases)
            for status in ("ready", "candidate", "review", "unsupported")
        },
        "test_cases": cases,
    }
