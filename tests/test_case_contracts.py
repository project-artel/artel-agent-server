# -*- coding: utf-8 -*-
"""TC 계약 뽑기: 모델 답을 오케가 저장하는 모양으로 바꾸는 규칙과, 용어집을 이어 키우는 흐름.

모델은 부르지 않는다. structured 를 가짜로 바꿔 묶음마다 정해 둔 답을 돌려준다.
"""

import asyncio

import app.agents.case_contract.workflow as wf
from app.agents.case_contract import CaseContractRequest, ContractCase, Term


def test_a_contract_goes_out_in_the_condition_tree_shape() -> None:
    case = wf._CaseStates(
        id=7,
        requires=[wf._Cond(state="늑대 사냥 의뢰", present=True), wf._Cond(state="낮밤=밤", present=True)],
        counters_need=[wf._CounterNeed(counter="동전", op=">=", value=50)],
        assumed_requires=[wf._Cond(state="검", present=True)],
        produces=[wf._Effect(state="늑대 처치")],
        removes=["고양이 동행"],
        sets_values=[wf._Value(name="등불 상태", value="꺼짐")],
        counters_add=[wf._CounterAdd(counter="동전", amount=30)],
    )

    wire = wf.to_wire(case)

    assert wire.condition == {"kind": "every", "parts": [
        wf._test("늑대_사냥_의뢰", "==", 1), wf._test("낮밤", "==", "밤"), wf._test("동전", ">=", 50),
    ]}
    assert wire.state_after == "늑대_처치=1; 고양이_동행=0; 등불_상태=꺼짐; 동전=+30"
    assert wire.assumed_before == "검 == 1"


def test_a_tc_that_changes_nothing_has_no_condition() -> None:
    assert wf.to_wire(wf._CaseStates(id=1)).condition is None


def test_start_state_skips_what_nobody_knows() -> None:
    terms = [
        Term(name="낮밤", kind="value", meaning="", options=["낮", "밤"], start="낮", start_basis="text"),
        Term(name="등불", kind="has", meaning="", start="없음", start_basis="default"),
        Term(name="등불 상태", kind="value", meaning="", options=["켜짐", "꺼짐"], start="unknown"),
    ]
    assert wf.start_state(terms) == {"낮밤": "낮", "등불": "0"}


def test_the_glossary_grows_chunk_by_chunk_and_contracts_use_it(monkeypatch) -> None:
    seen: list[str] = []

    class _Fake:
        def __init__(self, schema):
            self.schema = schema

        async def ainvoke(self, prompt):
            seen.append(prompt)
            if self.schema is wf._Glossary:
                n = len([p for p in seen if "glossary of game state" in p])
                return wf._Glossary(terms=[Term(name=f"상태{n}", kind="has", meaning="", start="없음")])
            ids = [int(line.split(" | ")[0]) for line in prompt.split("TCs (id")[1].splitlines()[1:] if line.strip()]
            return wf._Contracts(cases=[wf._CaseStates(id=i) for i in ids] + [wf._CaseStates(id=999)])

    monkeypatch.setattr(wf, "structured", lambda model, schema: _Fake(schema))
    monkeypatch.setattr(wf, "CHUNK", 2)
    cases = [ContractCase(id=i, step=f"행동 {i}") for i in range(1, 6)]

    out = asyncio.run(wf.extract_contracts(CaseContractRequest(cases=cases)))

    assert [t.name for t in out.terms] == ["상태1", "상태2", "상태3"]
    # 둘째 묶음부터는 앞서 만든 용어집을 본다.
    assert "상태1" in [p for p in seen if "glossary of game state" in p][1]
    # 묻지 않은 id(999)는 버린다.
    assert sorted(c.id for c in out.contracts) == [1, 2, 3, 4, 5]
    assert out.start_state == {"상태1": "0", "상태2": "0", "상태3": "0"}


def test_observed_patches_keep_only_leaves_of_known_names(monkeypatch):
    import asyncio
    from app.agents.case_contract import workflow as wf

    class _Fake:
        async def ainvoke(self, prompt):
            assert "Inventory.hasLamp false→true" in prompt
            return wf._Patches(patches=[
                wf.Patch(tc_id=7, op="leaves", name="등불", value="1"),
                wf.Patch(tc_id=7, op="needs", name="등불", value="1"),
                wf.Patch(tc_id=7, op="leaves", name="화면 잠금", value="1"),
                wf.Patch(op="leaves", name="등불", value="1"),
            ])

    monkeypatch.setattr(wf, "structured", lambda model, schema: _Fake())
    request = wf.ObservedRequest(
        terms=[wf.Term(name="등불", kind="has", meaning="lamp", start="없음")],
        cases=["7 | 할머니한테 말을 건다 → 등불을 받는다 || - | -"],
        observed=["7: Inventory.hasLamp false→true, Core.InteractionLock.locked false→true"],
    )
    patches = asyncio.run(wf.observed_patches(request))
    assert [(p.tc_id, p.op, p.name, p.value) for p in patches] == [(7, "leaves", "등불", "1")]


def test_a_tc_that_moves_to_another_screen_carries_its_arrival() -> None:
    from app.agents.case_contract.workflow import _CaseStates, to_wire

    moved = to_wire(_CaseStates(id=3, arrives_at=" 광장 "))
    assert moved.arrives_at == "광장"
    assert moved.condition is None and moved.state_after == ""
