"""Internal HTTP contract for deterministic v2 spec discovery."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.specs_v2.service import generate_spec_payload, generate_test_cases


router = APIRouter(tags=["specs-v2"])


class SpecRow(BaseModel):
    precondition: str
    test_step: str
    expected_result: str
    status: Literal["ready", "candidate", "review", "unsupported"]
    scene: str
    ui_text: str
    ui_sprite: str
    review_reason: str
    supporting_state: str
    artifact: str
    capture: str
    build_evidence: str
    spec_id: str
    covered_spec_ids: str
    evidence: str
    contract_ids: str


class SpecGenerationSummary(BaseModel):
    graph_nodes: int
    graph_edges: int
    contracts: int
    connected_scenarios: int
    ready_specs: int
    candidate_specs: int
    review_specs: int
    unsupported_specs: int


class SpecGenerationResponse(BaseModel):
    schema_version: Literal["spec-discovery.v2"]
    artifact: str
    capture: str
    build_evidence: str | None
    summary: SpecGenerationSummary
    ready_specs: list[SpecRow]
    review_specs: list[SpecRow]


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


SpecStatus = Literal["ready", "candidate", "review", "unsupported"]


class TestCaseSpec(BaseModel):
    scene: str
    precondition: str
    step: str
    expected_value: str
    status: SpecStatus


class TestCaseSource(BaseModel):
    spec_id: str
    # `null` when the scan could not place the evidence in a scene. `spec.scene`
    # says `미확정` there because a sentence needs a word; a key must not.
    scene_key: str | None
    used_step_indexes: list[int]
    evidence_gaps: list[str]


class TestCaseGeneration(BaseModel):
    build_evidence: str | None
    capture: str
    prompt_version: str | None
    llm_model: str | None


class TestCaseMetadata(BaseModel):
    source: TestCaseSource
    generation: TestCaseGeneration


class TestCase(BaseModel):
    # Repeated on the record, not only on the envelope: these are stored one per
    # row, and a row that cannot say what shape it is has to be guessed at.
    schema_version: Literal["test-case.v1"]
    spec: TestCaseSpec
    metadata: TestCaseMetadata


class TestCaseCounts(BaseModel):
    ready: int
    candidate: int
    review: int
    unsupported: int


class TestCaseResponse(BaseModel):
    schema_version: Literal["test-case.v1"]
    artifact: str
    capture: str
    build_evidence: str | None
    counts: TestCaseCounts
    test_cases: list[TestCase]


@router.post("/specs/v2/test-cases", response_model=TestCaseResponse)
async def generate_test_cases_v2(
    report: dict[str, Any] = Body(
        ...,
        description=(
            "One raw Artel SDK JSON report. Same discovery as "
            "`/specs/v2/generate`, shaped as `test-case.v1` records for storage."
        ),
    ),
) -> TestCaseResponse:
    """Every discovered spec as a `test-case.v1` record, runnable or not.

    Review and unsupported rows are included on purpose. A spec that cannot be
    run is still a fact about the game; `spec.status` says which is which and
    `metadata.source.evidence_gaps` says what stopped it.
    """

    try:
        payload = await run_in_threadpool(generate_test_cases, report)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return TestCaseResponse.model_validate(payload)
