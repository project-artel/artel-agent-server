"""Premises a tester cannot check, restated as what the running game reports."""

from app.specs_v2 import observable
from app.specs_v2.discovery import discover
from app.specs_v2.graph import graph_from_report


def test_a_loop_counter_is_not_something_the_reader_can_be_asked_for() -> None:
    assert not observable.readable("i")
    assert not observable.readable("damage")
    # A call is not made: reading the game must not change it.
    assert not observable.readable("SaveLoadController.LoadPlayData()")
    assert observable.readable("ChatWindow.streamingCoroutine")
    assert observable.readable("MapMove.StagePosition")


def test_the_same_object_written_two_ways_is_one_subject() -> None:
    """The expectation holds a resolved scene path; the proxy holds the code chain."""
    assert observable.subject("ChatWindow.chatText.text") == "chatText"
    assert observable.subject("Canvas/ChatWindow.chatText") == "chatText"
    assert observable.subject("Player.hp") == "hp"


def _path(condition: dict, effects: list[dict]):
    class Path:
        pass

    item = Path()
    item.condition = condition
    item.effects = effects
    return item


GUARD = {
    "kind": "test",
    "left": "i",
    "operator": ">=",
    "right": "ChatWindow.streamingText.Length",
}


def test_an_assignment_under_an_unreadable_guard_becomes_that_guard_observable_form() -> None:
    table = observable.proxies(
        [
            _path(
                GUARD,
                [
                    {
                        "kind": "write",
                        "target": "ChatWindow.streamingCoroutine",
                        "detail": "null",
                    },
                    {
                        "kind": "ui-value",
                        "target": "ChatWindow.label.text",
                        "detail": "ChatWindow.streamingText, true",
                    },
                ],
            )
        ]
    )

    text = "i >= ChatWindow.streamingText.Length"
    assert table[text] == [
        ("ChatWindow.streamingCoroutine", "null"),
        ("ChatWindow.label.text", "ChatWindow.streamingText"),
    ]


def test_an_assignment_from_a_masked_parameter_proves_nothing() -> None:
    """`streamingText = String.Concat(_, " ")` says nothing anyone can check."""
    table = observable.proxies(
        [
            _path(
                GUARD,
                [
                    {
                        "kind": "write",
                        "target": "ChatWindow.streamingText",
                        "detail": 'String.Concat(_, " ")',
                    }
                ],
            )
        ]
    )

    assert table == {}


def test_a_readable_guard_is_left_alone() -> None:
    condition = {
        "kind": "test",
        "left": "MapMove.StagePosition",
        "operator": "==",
        "right": "1",
    }
    rewritten, swapped = observable.rewrite(condition, {"whatever": []}, set())

    assert rewritten == condition
    assert swapped == []


def test_the_proxy_replaces_the_leaf_and_says_what_it_replaced() -> None:
    table = {"i >= ChatWindow.streamingText.Length": [("ChatWindow.streamingCoroutine", "null")]}
    rewritten, swapped = observable.rewrite({**GUARD}, table, set())

    assert rewritten["left"] == "ChatWindow.streamingCoroutine"
    assert rewritten["right"] == "null"
    assert rewritten["observableProxyFor"] == "i >= ChatWindow.streamingText.Length"
    assert swapped == ["i >= ChatWindow.streamingText.Length"]


def test_a_proxy_that_restates_the_expectation_is_refused() -> None:
    """Premise and expectation as one observation is a row that cannot fail."""
    table = {
        "i >= ChatWindow.streamingText.Length": [
            ("ChatWindow.label.text", "ChatWindow.streamingText"),
        ]
    }
    rewritten, swapped = observable.rewrite({**GUARD}, table, {"Canvas/ChatWindow.label"})

    assert swapped == []
    assert rewritten["left"] == "i"


def test_nested_conditions_are_walked() -> None:
    table = {"i >= ChatWindow.streamingText.Length": [("ChatWindow.streamingCoroutine", "null")]}
    condition = {
        "kind": "every",
        "parts": [
            {**GUARD},
            {"kind": "test", "left": "MapMove.StagePosition", "operator": "==", "right": "1"},
        ],
    }
    rewritten, swapped = observable.rewrite(condition, table, set())

    assert len(swapped) == 1
    assert rewritten["parts"][0]["left"] == "ChatWindow.streamingCoroutine"
    assert rewritten["parts"][1]["left"] == "MapMove.StagePosition"


def _report() -> dict:
    stream = "System.Boolean Demo.ChatWindow::MoveNext()"
    return {
        "schema": 6,
        "capture": "editor",
        "build": {"evidence": "fixture", "platform": "WindowsEditor"},
        "scenes": ["ChatScene"],
        "objects": [
            {
                "scene": "ChatScene",
                "path": "ChatWindow",
                "selector": "ChatScene/ChatWindow",
                "active": True,
                "components": [{"type": "Demo.ChatWindow"}],
            }
        ],
        "persistentObjects": [],
        "types": {
            "Demo.ChatWindow": [
                {
                    "schema": 6,
                    "entry": stream,
                    "entryId": "Assembly-CSharp|Demo.ChatWindow|MoveNext|System.Boolean()",
                    "source": stream,
                    "methodId": "Assembly-CSharp|Demo.ChatWindow|MoveNext|System.Boolean()",
                    "recordKind": "candidate",
                    "triggerKind": "lifecycle",
                    "confidence": "exact",
                    "callPath": [stream],
                    "condition": {**GUARD, "context": "this", "offset": 12},
                    "inputs": [],
                    "effects": [
                        {
                            "kind": "write",
                            "category": "state",
                            "target": "ChatWindow.streamingCoroutine",
                            "detail": "null",
                            "source": stream,
                            "offset": 20,
                        },
                        {
                            "kind": "scene",
                            "category": "observable",
                            "target": "NextScene",
                            "detail": None,
                            "source": stream,
                            "offset": 24,
                        },
                    ],
                    "calls": [],
                    "handles": [],
                    "alsoReachedBy": [],
                    "gaps": [],
                }
            ]
        },
        "unplaced": {},
        "gaps": [],
    }


def test_discovery_rewrites_the_premise_and_records_that_it_did() -> None:
    result = discover(graph_from_report(_report(), source="test"))

    rewritten = [
        contract
        for contract in result.contracts
        if contract.condition.get("observableProxyFor")
    ]
    assert rewritten, "the counter guard should have been restated"
    for contract in rewritten:
        assert contract.condition["left"] == "ChatWindow.streamingCoroutine"
        assert observable.ISSUE in contract.issues
