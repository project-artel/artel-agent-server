"""TC 계약 뽑기. 세션 밖의 한 번짜리 호출이라 저작 세션을 거치지 않는다(/scenario-steps/phrase 와 같은 자리)."""

import openai
from fastapi import APIRouter, HTTPException

from app.agents.case_contract import CaseContractRequest, CaseContractResult, extract_contracts
from app.llm.usage import set_usage_scope

router = APIRouter(tags=["scenario"])


class CaseContractBody(CaseContractRequest):
    # 이 호출의 LLM 비용을 어디에 달지. /knowledge-queries 와 같다.
    project_id: int | None = None


@router.post("/case-contracts", response_model=CaseContractResult)
async def case_contracts(payload: CaseContractBody) -> CaseContractResult:
    set_usage_scope("SCENARIO", None)
    try:
        return await extract_contracts(payload)
    except openai.APIError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
