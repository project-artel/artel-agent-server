"""Internal HTTP contract for deterministic v2 spec discovery."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.specs_v2.service import generate_spec_payload


router = APIRouter(tags=["specs-v2"])


class SpecRow(BaseModel):
    precondition: str
    test_step: str
    expected_result: str
    status: Literal["ready", "candidate", "review", "unsupported"]
    scene: str
    ui_text: str
    ui_sprite: str
    flow_role: str
    state_before: str
    state_after: str
    flow_id: str
    review_reason: str
    supporting_state: str
    artifact: str
    capture: str
    build_evidence: str
    spec_id: str
    covered_spec_ids: str
    evidence: str
    contract_ids: str


class ConnectedFlowRow(BaseModel):
    precondition: str
    test_step: str
    expected_result: str
    flow_role: str
    state_before: str
    state_after: str
    scene: str
    ui_text: str
    ui_sprite: str
    artifact: str
    capture: str
    build_evidence: str
    flow_id: str
    spec_id: str
    contract_ids: str
    evidence: str


class SpecGenerationSummary(BaseModel):
    graph_nodes: int
    graph_edges: int
    contracts: int
    connected_scenarios: int
    ready_specs: int
    candidate_specs: int
    review_specs: int
    unsupported_specs: int
    connected_flows: int


class SpecGenerationResponse(BaseModel):
    schema_version: Literal["spec-discovery.v2"]
    artifact: str
    capture: str
    build_evidence: str | None
    summary: SpecGenerationSummary
    ready_specs: list[SpecRow]
    review_specs: list[SpecRow]
    connected_flows: list[ConnectedFlowRow]


@router.post("/specs/v2/generate", response_model=SpecGenerationResponse)
async def generate_specs_v2(
    report: dict[str, Any] = Body(
        ...,
        description=(
            "One raw Artel SDK JSON report. Send Editor and development-build "
            "captures in separate requests; this endpoint never merges them."
        ),
    ),
) -> SpecGenerationResponse:
    """Generate Ready, Review, and connected-flow projections for one report."""

    try:
        payload = await run_in_threadpool(generate_spec_payload, report)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return SpecGenerationResponse.model_validate(payload)
