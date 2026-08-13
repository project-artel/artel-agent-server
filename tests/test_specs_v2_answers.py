"""The evidence keeps its own answers; this is where they are looked up."""

from app.specs_v2.answers import Answers


def _path(source: str, *, route=(), calls=(), effects=(), condition=None):
    class Path:
        pass

    item = Path()
    item.source_signature = source
    item.call_path = tuple(route) or (source,)
    item.calls = list(calls)
    item.effects = list(effects)
    item.condition = condition or {"kind": "always"}
    return item


PROMPT = "System.Void Demo.Chat::SetPromptVisible(System.Boolean)"
DONE = "System.Void Demo.Chat::OnStreamComplete()"
NEXT = "System.Boolean Demo.Chat::MoveNext()"


def _report_paths():
    """One method called `true` from one place and `false` from another."""
    shown = {
        "kind": "active-state",
        "category": "availability",
        "target": "Chat.prompt",
        # A parameter: inside `SetPromptVisible` it is not a literal, and the
        # SDK says so rather than guessing.
        "detail": "(not a literal)",
        "offset": 22,
    }
    return [
        _path(DONE, calls=[{"target": PROMPT, "args": "true", "offset": 2}]),
        _path(NEXT, calls=[{"target": PROMPT, "args": "false", "offset": 96}]),
        _path(PROMPT, route=(DONE, PROMPT), effects=[shown]),
        _path(PROMPT, route=(NEXT, PROMPT), effects=[shown]),
    ]


def test_a_method_called_both_ways_has_no_answer_and_each_route_has_one() -> None:
    known = Answers.of(_report_paths())

    assert known.passed[("OnStreamComplete", "SetPromptVisible")] == "true"
    assert known.passed[("MoveNext", "SetPromptVisible")] == "false"

    finished, started = _report_paths()[2], _report_paths()[3]
    assert known.parameter_value(finished) == "true"
    assert known.parameter_value(started) == "false"


def test_one_caller_passing_two_literals_is_left_unanswered() -> None:
    """That happens in a branch, and which branch this row took is not said."""
    known = Answers.of(
        [
            _path(
                DONE,
                calls=[
                    {"target": PROMPT, "args": "true", "offset": 2},
                    {"target": PROMPT, "args": "false", "offset": 9},
                ],
            )
        ]
    )

    assert known.passed == {}


def test_a_null_check_is_not_a_comparison_against_zero() -> None:
    """IL spells it that way; the runtime reader never shows a zero there."""
    known = Answers.of(
        [
            _path(
                NEXT,
                effects=[
                    {
                        "kind": "write",
                        "category": "state",
                        "target": "Chat.streamingCoroutine",
                        "detail": "null",
                        "offset": 20,
                    }
                ],
            )
        ]
    )
    assert "Chat.streamingCoroutine" in known.references

    resolved, _ = known.resolve(
        {
            "kind": "test",
            "left": "Chat.streamingCoroutine",
            "operator": "!=",
            "right": "0",
        },
        set(),
    )
    assert resolved["right"] == "null"
    assert resolved["nullComparison"] is True


def test_a_field_the_evidence_never_nulls_keeps_its_zero() -> None:
    """`hp != 0` is a number, and turning it into `null` would be a lie."""
    resolved, _ = Answers().resolve(
        {"kind": "test", "left": "Player.hp", "operator": "!=", "right": "0"}, set()
    )

    assert resolved["right"] == "0"
    assert "nullComparison" not in resolved


def test_a_record_that_undoes_its_own_guard_is_a_transition() -> None:
    """가드와 효과가 동시에 참인 순간이 없으면 그것은 상태가 아니다."""
    from app.specs_v2.answers import negates_own_guard

    # 참조의 `!= 0` 이 `!= null` 로 고쳐진 뒤에 이 검사가 돈다. 파이프라인의
    # 순서가 그렇고, 여기서도 같은 모양을 준다.
    running = {"kind": "test", "left": "Chat.handle", "operator": "!=", "right": "null"}
    stops_it = [
        {"kind": "write", "target": "Chat.handle", "detail": "null"},
        {"kind": "ui-value", "target": "Chat.label.text", "detail": "Chat.full, true"},
    ]
    assert negates_own_guard(running, stops_it)

    # 가드를 건드리지 않는 효과는 전이가 아니라 그 상태에서 보이는 것이다.
    leaves_it = [{"kind": "ui-value", "target": "Chat.label.text", "detail": "Chat.full"}]
    assert not negates_own_guard(running, leaves_it)


def test_the_same_check_the_other_way_round() -> None:
    from app.specs_v2.answers import negates_own_guard

    idle = {"kind": "test", "left": "Chat.handle", "operator": "==", "right": "null"}
    starts_it = [{"kind": "write", "target": "Chat.handle", "detail": "Chat.started"}]

    assert negates_own_guard(idle, starts_it)


def test_the_call_site_literal_reaches_the_expected_result() -> None:
    """모으기만 하고 안 쓰면 `값 미확정` 이 그대로 나간다."""
    from app.specs_v2.discovery import discover
    from app.specs_v2.graph import graph_from_report

    shown = "System.Void Demo.Chat::SetPromptVisible(System.Boolean)"
    done = "System.Void Demo.Chat::Finish()"
    report = {
        "schema": 6,
        "capture": "editor",
        "build": {"evidence": "fixture", "platform": "WindowsEditor"},
        "scenes": ["ChatScene"],
        "objects": [
            {
                "scene": "ChatScene",
                "path": "Chat",
                "selector": "ChatScene/Chat",
                "active": True,
                "components": [{"type": "Demo.Chat"}],
            }
        ],
        "persistentObjects": [],
        "types": {
            "Demo.Chat": [
                {
                    "schema": 6,
                    "entry": done,
                    "entryId": "Assembly-CSharp|Demo.Chat|Finish|System.Void()",
                    "source": done,
                    "methodId": "Assembly-CSharp|Demo.Chat|Finish|System.Void()",
                    "recordKind": "candidate",
                    "triggerKind": "lifecycle",
                    "confidence": "exact",
                    "callPath": [done],
                    "condition": {"kind": "always"},
                    "inputs": [],
                    "effects": [],
                    "calls": [{"target": shown, "args": "true", "offset": 2}],
                    "handles": [],
                    "alsoReachedBy": [],
                    "gaps": [],
                },
                {
                    "schema": 6,
                    "entry": done,
                    "entryId": "Assembly-CSharp|Demo.Chat|Finish|System.Void()",
                    "source": shown,
                    "methodId": "Assembly-CSharp|Demo.Chat|SetPromptVisible|System.Void(System.Boolean)",
                    "recordKind": "candidate",
                    "triggerKind": "lifecycle",
                    "confidence": "exact",
                    "callPath": [done, shown],
                    "condition": {"kind": "always"},
                    "inputs": [],
                    "effects": [
                        {
                            "kind": "active-state",
                            "category": "availability",
                            "target": "Chat.prompt",
                            "detail": "(not a literal)",
                            "source": shown,
                            "offset": 22,
                        }
                    ],
                    "calls": [],
                    "handles": [],
                    "alsoReachedBy": [],
                    "gaps": [],
                },
            ]
        },
        "unplaced": {},
        "gaps": [],
    }

    result = discover(graph_from_report(report, source="test"))
    values = [
        item.value
        for contract in result.contracts
        for item in contract.assertions
        if item.target and "prompt" in item.target
    ]
    assert values, "표시 상태를 바꾸는 계약이 나와야 한다"
    assert all(value is True for value in values)
