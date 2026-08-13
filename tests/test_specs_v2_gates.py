"""입력으로 열리는 대기 지점을 세는 법.

한 코루틴이 두 번 멈춰 서고 두 번 다 사람이 눌러야 열릴 때, 대기를 하나만 세면
**둘 사이에서 일어난 일이 뒤쪽 대기의 결과로 붙는다.** 첫 입력이 일으킨 변화가 두
번째 입력의 기대 결과가 되어, 어느 순간에도 참이 아닌 행이 나온다.
"""

from __future__ import annotations

from app.specs_v2.discovery import _gates
from app.specs_v2.graph import graph_from_report


def _report(records: list[dict]) -> dict:
    return {
        "schema": 6,
        "capture": "editor",
        "build": {},
        "scenes": [{"name": "S", "objects": []}],
        "types": {"T": records},
        "unplaced": {},
        "objects": [],
        "persistentObjects": [],
        "gaps": [],
    }


def _record(**over) -> dict:
    base = {
        "schema": 6,
        "owner": "T",
        "entry": "System.Void T::Run()",
        "entryId": "A|T|Run|System.Void()",
        "source": "System.Void T::Run()",
        "methodId": "A|T|Run|System.Void()",
        "recordKind": "flow",
        "triggerKind": "lifecycle",
        "confidence": "verified",
        "callPath": ["System.Void T::Run()"],
        "condition": {"kind": "always"},
        "inputs": [],
        "effects": [],
        "calls": [],
        "handles": [],
        "alsoReachedBy": [],
        "gaps": [],
    }
    return {**base, **over}


HELD = "System.Boolean T::Pressed()"
LAMBDA = "System.Boolean T::<Run>b__0()"
MACHINE = "System.Boolean T/<Run>d__1::MoveNext()"
PRESS = {"kind": "key", "control": "any", "phase": "down", "absent": False, "offset": 0}


def test_a_delegate_that_calls_the_key_reader_is_still_a_gate() -> None:
    graph = graph_from_report(
        _report(
            [
                # 키를 직접 읽는 조각. 자기 `inputs` 를 들고 있다.
                _record(
                    source=HELD,
                    methodId="A|T|Pressed|System.Boolean()",
                    callPath=["System.Void T::Run()", MACHINE, HELD],
                    condition={"kind": "gesture", "input": "key:any (down)", "offset": 0},
                    inputs=[PRESS],
                ),
                # 읽지 않고 **부르는** 조각. `inputs` 가 비어 있지만 입력은 한 홉 옆이다.
                _record(
                    source=LAMBDA,
                    methodId="A|T|b__0|System.Boolean()",
                    callPath=["System.Void T::Run()", MACHINE, LAMBDA],
                    condition={
                        "kind": "test",
                        "left": "T.busy",
                        "operator": "!=",
                        "right": "0",
                        "offset": 11,
                    },
                    handedOverAt=158,
                    handedOverTo="System.Void T::.ctor()",
                    gaps=["reached-through-delegate"],
                    calls=[{"target": HELD, "targetId": "A|T|Pressed|System.Boolean()", "offset": 13}],
                ),
            ]
        ),
        source="t",
    )

    gates = _gates(graph)

    assert [gate.handed_over_at for gate in gates] == [158]
    # 빌린 것은 입력만이 아니다. 키를 읽는 가지의 가드가 곧 "이 상태에서 눌러야
    # 열린다" 이고, 그것이 사전 조건이다.
    borrowed = gates[0]
    assert borrowed.inputs == (PRESS,)
    assert "T.busy" in str(borrowed.condition)
    assert "key:any" in str(borrowed.condition)


def test_a_delegate_that_reads_nothing_is_not_a_gate() -> None:
    """부르는 곳에 입력이 없으면 빌릴 것도 없다. 없는 것을 지어내지 않는다."""
    graph = graph_from_report(
        _report(
            [
                _record(
                    source=LAMBDA,
                    methodId="A|T|b__0|System.Boolean()",
                    callPath=["System.Void T::Run()", MACHINE, LAMBDA],
                    handedOverAt=158,
                    handedOverTo="System.Void T::.ctor()",
                    gaps=["reached-through-delegate"],
                    calls=[{"target": "System.Boolean T::Done()", "targetId": "A|T|Done", "offset": 6}],
                )
            ]
        ),
        source="t",
    )

    assert _gates(graph) == []


def test_two_readers_leave_it_unanswered() -> None:
    """어느 입력이 이 대기를 여는지 근거가 말하지 않으면 고르지 않는다."""
    other = "System.Boolean T::Other()"
    graph = graph_from_report(
        _report(
            [
                _record(
                    source=HELD,
                    methodId="A|T|Pressed|System.Boolean()",
                    condition={"kind": "gesture", "input": "key:any (down)", "offset": 0},
                    inputs=[PRESS],
                ),
                _record(
                    source=other,
                    methodId="A|T|Other|System.Boolean()",
                    condition={"kind": "gesture", "input": "key:Return (down)", "offset": 0},
                    inputs=[{**PRESS, "control": "Return"}],
                ),
                _record(
                    source=LAMBDA,
                    methodId="A|T|b__0|System.Boolean()",
                    callPath=["System.Void T::Run()", MACHINE, LAMBDA],
                    handedOverAt=158,
                    handedOverTo="System.Void T::.ctor()",
                    gaps=["reached-through-delegate"],
                    calls=[
                        {"target": HELD, "targetId": "A|T|Pressed|System.Boolean()", "offset": 13},
                        {"target": other, "targetId": "A|T|Other|System.Boolean()", "offset": 19},
                    ],
                ),
            ]
        ),
        source="t",
    )

    assert _gates(graph) == []


def test_a_call_from_before_the_gate_is_not_what_the_input_did() -> None:
    """대기 앞에서 부른 것은 이번 입력의 결과가 아니다.

    코루틴이 돌고 → 멈추고 → 눌러서 열리고 → 한 바퀴 돌아 다시 부른다. 그 호출이
    닿는 결과를 이번 입력의 기대로 적으면, 같은 기대가 정반대 전제로 두 번 나온다.

    같은 구간의 가드가 대기 뒤에서 평가된다는 사실에 기대면 안 된다. 그것은 **가드**가
    뒤라는 말이지 호출이 뒤라는 말이 아니다.
    """
    from app.specs_v2.discovery import discover

    machine = "System.Boolean T/<Run>d__1::MoveNext()"
    before = "System.Void T::Early()"
    after = "System.Void T::Late()"

    graph = graph_from_report(
        _report(
            [
                _record(
                    source=HELD,
                    methodId="A|T|Pressed|System.Boolean()",
                    callPath=["System.Void T::Run()", machine, HELD],
                    condition={"kind": "gesture", "input": "key:any (down)", "offset": 0},
                    inputs=[PRESS],
                    handedOverAt=100,
                    handedOverTo="System.Void T::.ctor()",
                    gaps=["reached-through-delegate"],
                ),
                # 대기 앞에서 부르고, 가드는 루프 끝(@300)에서 평가된다.
                _record(
                    source=machine,
                    methodId="A|T/d__1|MoveNext|System.Boolean()",
                    callPath=["System.Void T::Run()", machine],
                    condition={"kind": "test", "left": "T.n", "operator": "<", "right": "3", "offset": 300},
                    calls=[{"target": before, "targetId": "A|T|Early|System.Void()", "offset": 40}],
                ),
                # 대기 뒤에서 부른다.
                _record(
                    source=machine,
                    methodId="A|T/d__1|MoveNext|System.Boolean()",
                    callPath=["System.Void T::Run()", machine],
                    calls=[{"target": after, "targetId": "A|T|Late|System.Void()", "offset": 140}],
                ),
                _record(
                    source=before,
                    methodId="A|T|Early|System.Void()",
                    callPath=["System.Void T::Run()", machine, before],
                    effects=[{"kind": "ui-value", "category": "observable", "target": "Panel.label.text", "detail": "\"early\"", "offset": 4}],
                ),
                _record(
                    source=after,
                    methodId="A|T|Late|System.Void()",
                    callPath=["System.Void T::Run()", machine, after],
                    effects=[{"kind": "ui-value", "category": "observable", "target": "Panel.label.text", "detail": "\"late\"", "offset": 4}],
                ),
            ]
        ),
        source="t",
    )

    resumed = [
        contract
        for contract in discover(graph).contracts
        if contract.trigger.kind == "input"
    ]
    reached = {str(item.value) for contract in resumed for item in contract.assertions}

    assert '"late"' in reached
    assert '"early"' not in reached
