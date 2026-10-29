"""용어집을 키우고 그 이름으로 계약을 뽑는다.

용어집은 묶음 순서대로 키운다 — 뒤 묶음이 앞에서 정한 이름을 다시 써야 하기 때문이다. 계약은 묶음끼리
기다릴 것이 없어 한꺼번에 돌린다. TC 294 건을 차례로 돌린 측정은 12분이었고, 그 대부분이 계약이었다.
"""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from pydantic import BaseModel, Field

from app.llm.chat_model import structured
from app.llm.models import DEFAULT_MODEL
from app.prompts.loader import load_prompt

PROMPT_AGENT = "case_contract"
CHUNK = 40


class Term(BaseModel):
    name: str = Field(description="Short Korean name, the word the TCs use.")
    kind: Literal["has", "value", "counter"] = Field(
        description="has: present or absent (an item, a quest taken, a door unlocked). value: always exactly one of options (time of day, a lamp on/off). counter: an amount that adds up (coins)."
    )
    meaning: str
    aliases: list[str] = Field(default_factory=list, description="Other words the TCs use for this same thing.")
    options: list[str] = Field(default_factory=list, description="Only for kind=value.")
    start: str = Field(
        description="At the start of a new game: for has '있음'/'없음', for value one option, for counter a number; 'unknown' if neither the text nor plain default tells."
    )
    start_basis: Literal["text", "default", "unknown"] = "unknown"
    not_same_as: list[str] = Field(default_factory=list, description="Close terms this must NOT be merged with, each with why.")


class _Glossary(BaseModel):
    terms: list[Term]


class _Cond(BaseModel):
    state: str
    present: bool = Field(description="true: must hold. false: must NOT hold.")


class _Value(BaseModel):
    name: str
    value: str


class _CounterNeed(BaseModel):
    counter: str
    op: Literal[">=", "<"]
    value: int


class _CounterAdd(BaseModel):
    counter: str
    amount: int


class _Group(BaseModel):
    conds: list[_Cond]


class _Effect(BaseModel):
    state: str


class _CaseStates(BaseModel):
    id: int
    requires: list[_Cond] = Field(default_factory=list)
    requires_any: list[_Group] = Field(default_factory=list)
    requires_values: list[_Value] = Field(default_factory=list)
    counters_need: list[_CounterNeed] = Field(default_factory=list)
    assumed_requires: list[_Cond] = Field(default_factory=list)
    produces: list[_Effect] = Field(default_factory=list)
    removes: list[str] = Field(default_factory=list)
    sets_values: list[_Value] = Field(default_factory=list)
    counters_add: list[_CounterAdd] = Field(default_factory=list)


class _Contracts(BaseModel):
    cases: list[_CaseStates]


class ContractCase(BaseModel):
    id: int
    scene: str = ""
    precondition: str = ""
    step: str
    expected_value: str = ""


class CaseContractRequest(BaseModel):
    cases: list[ContractCase]
    # 이미 있는 용어집. 새 TC 는 이 이름을 다시 쓰고, 없는 것만 더한다.
    terms: list[Term] = Field(default_factory=list)
    about: str = ""
    model: str = DEFAULT_MODEL


class CaseContract(BaseModel):
    """오케가 저장하는 모양 그대로. `condition` 은 지도 조건과 같은 트리라 같은 읽기 경로를 탄다."""

    id: int
    condition: dict | None = None
    state_after: str = ""
    assumed_before: str = ""


class CaseContractResult(BaseModel):
    terms: list[Term]
    start_state: dict[str, str]
    contracts: list[CaseContract]


def _fmt_cases(cases: list[ContractCase]) -> str:
    return "\n".join(f"{c.id} | {c.scene} | {c.precondition or '-'} | {c.step} | {c.expected_value}" for c in cases)


def _fmt_terms(terms: list[Term]) -> str:
    return "\n".join(
        f"{t.name} | {t.kind} | {'/'.join(t.options) or '-'} | {t.meaning} | {', '.join(t.aliases) or '-'}" for t in terms
    )


def _name(text: str) -> str:
    return text.strip().replace(" ", "_")


def _test(var: str, op: str, value: object) -> dict:
    return {"kind": "test", "left": var, "operator": op, "right": str(value), "context": "static", "offset": 0, "subjectLost": None}


def _group(kind: str, parts: list[dict]) -> dict:
    return parts[0] if len(parts) == 1 else {"kind": kind, "parts": parts}


def _cond(c: _Cond) -> dict:
    # 값 이름을 상태처럼 적은 것(`낮밤=밤`)은 값 비교로 읽는다.
    if "=" in c.state:
        name, value = c.state.split("=", 1)
        return _test(_name(name), "==" if c.present else "!=", _name(value))
    return _test(_name(c.state), "==", 1 if c.present else 0)


def to_wire(case: _CaseStates) -> CaseContract:
    parts = [_cond(x) for x in case.requires]
    parts += [_test(_name(v.name), "==", _name(v.value)) for v in case.requires_values]
    parts += [_test(_name(n.counter), n.op, n.value) for n in case.counters_need]
    if case.requires_any:
        parts.append({"kind": "either", "parts": [_group("every", [_cond(x) for x in g.conds]) for g in case.requires_any]})
    after = [f"{_name(e.state)}=1" for e in case.produces] + [f"{_name(r)}=0" for r in case.removes]
    after += [f"{_name(v.name)}={_name(v.value)}" for v in case.sets_values]
    after += [f"{_name(a.counter)}={a.amount:+d}" for a in case.counters_add]
    assumed = [_cond(x) for x in case.assumed_requires]
    return CaseContract(
        id=case.id,
        condition=_group("every", parts) if parts else None,
        state_after="; ".join(after),
        assumed_before="; ".join(f"{t['left']} {t['operator']} {t['right']}" for t in assumed),
    )


def start_state(terms: list[Term]) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in terms:
        if t.start == "unknown":
            continue
        value = {"있음": "1", "없음": "0"}.get(t.start, t.start) if t.kind == "has" else t.start
        out[_name(t.name)] = _name(value)
    return out


async def extract_contracts(request: CaseContractRequest) -> CaseContractResult:
    glossary = load_prompt(PROMPT_AGENT, "glossary").body
    contract = load_prompt(PROMPT_AGENT, "contract").body
    terms: dict[str, Term] = {t.name: t for t in request.terms}
    chunks = [request.cases[i:i + CHUNK] for i in range(0, len(request.cases), CHUNK)]
    for chunk in chunks:
        existing = (
            "\nThe glossary so far (from earlier TCs) is below. Keep every term as it is; reuse these names whenever they fit; "
            "add only genuinely new terms, and you may add aliases or not_same_as to existing ones by repeating them in full.\n"
            + "\n".join(json.dumps(t.model_dump(), ensure_ascii=False) for t in terms.values()) + "\n"
            if terms else ""
        )
        out: _Glossary = await structured(request.model, _Glossary).ainvoke(
            glossary.format(existing=existing, about=request.about or "-", cases=_fmt_cases(chunk))
        )
        for t in out.terms:
            old = terms.get(t.name)
            terms[t.name] = t if old is None else old.model_copy(update={
                "aliases": sorted(set(old.aliases + t.aliases)),
                "not_same_as": sorted(set(old.not_same_as + t.not_same_as)),
            })
    listed = list(terms.values())
    answers = await asyncio.gather(*(
        structured(request.model, _Contracts).ainvoke(contract.format(terms=_fmt_terms(listed), cases=_fmt_cases(chunk)))
        for chunk in chunks
    ))
    asked = {c.id for c in request.cases}
    contracts = [to_wire(c) for a in answers for c in a.cases if c.id in asked]
    return CaseContractResult(terms=listed, start_state=start_state(listed), contracts=contracts)


class Patch(BaseModel):
    tc_id: int | None = None
    op: Literal["needs", "not_needs", "leaves", "start"]
    name: str
    value: str


class _Patches(BaseModel):
    patches: list[Patch]


class StatementRequest(BaseModel):
    said: str
    question: str = ""
    terms: list[Term] = Field(default_factory=list)
    start_state: dict[str, str] = Field(default_factory=dict)
    # 줄마다 `id | 행동 → 기대 || 필요 | 남김`. 오케가 지금 계약으로 만들어 보낸다.
    cases: list[str] = Field(default_factory=list)
    model: str = DEFAULT_MODEL


async def statement_patches(request: StatementRequest) -> list[Patch]:
    """사용자 말에서 게임 사실만 계약 수정으로. 사실이 아니면 빈 목록이다."""
    prompt = load_prompt(PROMPT_AGENT, "statement").body.format(
        question=request.question or "-",
        said=request.said,
        terms="\n".join(f"{t.name} | {t.kind} | {'/'.join(t.options) or '-'} | {t.meaning}" for t in request.terms) or "-",
        start=", ".join(f"{k}={v}" for k, v in request.start_state.items()) or "-",
        cases="\n".join(request.cases),
    )
    out: _Patches = await structured(request.model, _Patches).ainvoke(prompt)
    return [p.model_copy(update={"name": _name(p.name), "value": _name(p.value)}) for p in out.patches]


class ObservedRequest(BaseModel):
    terms: list[Term] = Field(default_factory=list)
    start_state: dict[str, str] = Field(default_factory=dict)
    # 줄마다 `id | 행동 → 기대 || 필요 | 남김`. 통과한 TC 만이다.
    cases: list[str] = Field(default_factory=list)
    # 줄마다 `id: 코드이름 앞→뒤, …`.
    observed: list[str] = Field(default_factory=list)
    model: str = DEFAULT_MODEL


async def observed_patches(request: ObservedRequest) -> list[Patch]:
    """QA 가 본 값의 변화에서 계약이 빠뜨린 '남기는 것' 만. 다른 수정은 받아도 버린다."""
    if not request.observed or not request.terms:
        return []
    known = {_name(t.name) for t in request.terms}
    prompt = load_prompt(PROMPT_AGENT, "observed").body.format(
        terms="\n".join(f"{t.name} | {t.kind} | {'/'.join(t.options) or '-'} | {t.meaning}" for t in request.terms),
        start=", ".join(f"{k}={v}" for k, v in request.start_state.items()) or "-",
        cases="\n".join(request.cases),
        observed="\n".join(request.observed),
    )
    out: _Patches = await structured(request.model, _Patches).ainvoke(prompt)
    return [
        p.model_copy(update={"name": _name(p.name), "value": _name(p.value)})
        for p in out.patches
        if p.op == "leaves" and p.tc_id is not None and _name(p.name) in known
    ]
