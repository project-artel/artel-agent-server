"""자연어 TC 의 계약 — 용어집과 TC 마다 필요한 것 · 남기는 것.

TC 가 사람 말로만 적혀 있으면 저작이 순서를 세울 근거가 글뿐이다. 지도가 주던 조건식을 대신해,
용어집을 먼저 만들고 그 이름으로만 계약을 적는다. 측정(등불 골짜기)에서 용어집 없이 뽑은 계약은
이름이 엇갈려(무기 보유 ↔ 검 보유) 답을 받아도 다른 TC 와 이어지지 않았다.

이 모듈은 뽑기만 한다. 언제 뽑을지(계약이 없거나 문구가 바뀐 TC 만)와 저장은 오케가 정한다.
"""

from app.agents.case_contract.workflow import (
    CaseContract,
    CaseContractRequest,
    CaseContractResult,
    ContractCase,
    Term,
    extract_contracts,
    to_wire,
)

__all__ = [
    "CaseContract",
    "CaseContractRequest",
    "CaseContractResult",
    "ContractCase",
    "Term",
    "extract_contracts",
    "to_wire",
]
