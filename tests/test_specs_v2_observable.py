"""Premises a tester cannot check, restated as what the running game reports."""

from app.specs_v2 import observable
from app.specs_v2.answers import Answers, NOTES, RESTATED, SUBSTITUTED
from app.specs_v2.discovery import discover
from app.specs_v2.graph import graph_from_report
from app.specs_v2.model import Assertion, SourceRef


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


def _path(condition: dict, effects: list[dict], calls: list[dict] | None = None):
    class Path:
        pass

    item = Path()
    item.condition = condition
    item.effects = effects
    item.calls = calls or []
    item.call_path = ("System.Void Demo::Owner()",)
    item.source_signature = "System.Void Demo::Owner()"
    return item


GUARD = {
    "kind": "test",
    "left": "i",
    "operator": ">=",
    "right": "ChatWindow.streamingText.Length",
}


def test_an_assignment_under_an_unreadable_guard_becomes_that_guard_observable_form() -> None:
    table = Answers.of(
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
    ).left_behind

    text = "i >= ChatWindow.streamingText.Length"
    assert table[text] == [
        ("ChatWindow.streamingCoroutine", "null"),
        ("ChatWindow.label.text", "ChatWindow.streamingText"),
    ]


def test_an_assignment_from_a_masked_parameter_proves_nothing() -> None:
    """`streamingText = String.Concat(_, " ")` says nothing anyone can check."""
    table = Answers.of(
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
    ).left_behind

    assert table == {}


def test_a_readable_guard_is_left_alone() -> None:
    condition = {
        "kind": "test",
        "left": "MapMove.StagePosition",
        "operator": "==",
        "right": "1",
    }
    rewritten, swapped = Answers(left_behind={"whatever": []}).resolve(condition, set())

    assert rewritten == condition
    assert swapped == []


def test_the_proxy_replaces_the_leaf_and_says_what_it_replaced() -> None:
    table = {"i >= ChatWindow.streamingText.Length": [("ChatWindow.streamingCoroutine", "null")]}
    rewritten, swapped = Answers(left_behind=table).resolve({**GUARD}, set())

    assert rewritten["left"] == "ChatWindow.streamingCoroutine"
    assert rewritten["right"] == "null"
    assert rewritten["observableProxyFor"] == "i >= ChatWindow.streamingText.Length"
    assert swapped == [RESTATED]


def test_a_proxy_that_restates_the_expectation_is_refused() -> None:
    """Premise and expectation as one observation is a row that cannot fail."""
    table = {
        "i >= ChatWindow.streamingText.Length": [
            ("ChatWindow.label.text", "ChatWindow.streamingText"),
        ]
    }
    rewritten, swapped = Answers(left_behind=table).resolve({**GUARD}, {"Canvas/ChatWindow.label"})

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
    rewritten, swapped = Answers(left_behind=table).resolve(condition, set())

    assert swapped == [RESTATED]
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
        assert RESTATED in contract.issues


def test_two_coroutines_spell_their_counter_the_same_and_must_not_be_joined() -> None:
    """Both compile to a state machine whose entry point is `MoveNext`."""
    lines = observable.qualify(
        {"kind": "test", "left": "i", "operator": "<", "right": "Script.lineCount"},
        "Story/<Tell>d__1.MoveNext",
    )
    letters = observable.qualify(
        {"kind": "test", "left": "i", "operator": ">=", "right": "Chat.streamingText.Length"},
        "Chat/<Type>d__2.MoveNext",
    )

    assert lines["left"] == "Story/<Tell>d__1.MoveNext.i"
    assert letters["left"] == "Chat/<Type>d__2.MoveNext.i"
    assert lines["left"] != letters["left"]


def test_a_field_keeps_the_name_the_evidence_gave_it() -> None:
    """Only bare names are qualified; a field already says where it lives."""
    condition = {
        "kind": "test",
        "left": "MapMove.StagePosition",
        "operator": "==",
        "right": "1",
    }

    assert observable.qualify(condition, "MapMove.Start") == condition


def test_what_no_branch_could_restate_is_named() -> None:
    counter = observable.qualify(
        {"kind": "test", "left": "i", "operator": "<", "right": "3"}, "Story.MoveNext"
    )
    assert observable.unreadable_atoms(counter) == ["Story.MoveNext.i < 3"]
    assert (
        observable.unreadable_atoms(
            {"kind": "test", "left": "MapMove.StagePosition", "operator": "==", "right": "1"}
        )
        == []
    )


def test_a_row_resting_on_an_unanswerable_premise_is_not_ready() -> None:
    report = _report()
    # Nothing in this branch assigns a readable field from another, so the
    # counter has no observable form and the premise stays unanswerable.
    report["types"]["Demo.ChatWindow"][0]["effects"] = [
        {
            "kind": "ui-value",
            "category": "observable",
            "target": "ChatWindow.label.text",
            "detail": 'String.Concat(_, " ")',
            "source": "System.Boolean Demo.ChatWindow::MoveNext()",
            "offset": 20,
        }
    ]
    result = discover(graph_from_report(report, source="test"))

    resting = [
        contract
        for contract in result.contracts
        if observable.UNCHECKABLE in contract.issues
    ]
    assert resting, "the counter guard should still be unanswerable"
    for contract in resting:
        assert observable.UNCHECKABLE in contract.issues
        assert contract.quality != "ready"


def _wrote(source: str, effects: list[dict]):
    class Path:
        pass

    item = Path()
    item.source_signature = source
    item.effects = effects
    item.calls = []
    item.call_path = (source,)
    item.condition = {"kind": "always"}
    return item


LOAD = "System.Int32 Core.SaveLoadController::LoadPlayData()"


def test_a_call_is_answered_by_the_one_field_it_writes() -> None:
    """It saves on the way out, so nothing may call it — but the field holds it."""
    written = Answers.of(
        [
            _wrote(
                LOAD,
                [
                    {
                        "kind": "write",
                        "target": "MapMove.StagePosition",
                        "detail": 'PlayerPrefs.GetInt("StagePosition", -1)',
                    }
                ],
            )
        ]
    ).wrote

    assert written == {"LoadPlayData": "MapMove.StagePosition"}


def test_two_writes_leave_the_question_of_which_one_answers_unanswered() -> None:
    written = Answers.of(
        [
            _wrote(
                LOAD,
                [
                    {"kind": "write", "target": "MapMove.StagePosition", "detail": "1"},
                    {"kind": "write", "target": "MapMove.other", "detail": "2"},
                ],
            )
        ]
    ).wrote

    assert written == {}


def test_the_premise_is_said_through_the_field_and_keeps_the_call_it_replaced() -> None:
    condition = {
        "kind": "test",
        "left": "TitleSceneManager.saveLoadController.LoadPlayData()",
        "operator": "==",
        "right": "-1",
    }
    rewritten, swapped = Answers(wrote={"LoadPlayData": "MapMove.StagePosition"}).resolve(
        condition, set()
    )

    assert rewritten["left"] == "MapMove.StagePosition"
    assert rewritten["substitutedCalls"]["left"].endswith("LoadPlayData()")
    assert swapped == [SUBSTITUTED]


def test_a_call_taking_arguments_is_not_substituted() -> None:
    """The field is the same answer only when the call is the same call."""
    condition = {
        "kind": "test",
        "left": "Container.GetScriptData(i)",
        "operator": "==",
        "right": "0",
    }
    rewritten, swapped = Answers(wrote={"GetScriptData": "Story.current"}).resolve(
        condition, set()
    )

    assert swapped == []
    assert rewritten["left"] == "Container.GetScriptData(i)"


def test_a_derivation_note_does_not_lower_the_grade() -> None:
    """Both notes mean the row became answerable, not that something is missing."""
    from app.specs_v2.discovery import DERIVATION_NOTES, _quality
    from app.specs_v2.model import Trigger

    trigger = Trigger("control", "MenuScene", "Play 조작", "Canvas/Play", "m_OnClick", "exact")
    assertion = Assertion(
        "scene", "PlayScene", "transition", "PlayScene", "observable", "exact", "scene",
        SourceRef("r", "e", "m", 0),
    )
    assert NOTES <= DERIVATION_NOTES
    assert _quality(trigger, [assertion], list(DERIVATION_NOTES)) == "ready"


def test_some_of_it_answerable_is_not_the_same_as_none_of_it() -> None:
    """A field to start from, and the rest gauged from the screen."""
    partly = {
        "kind": "every",
        "parts": [
            observable.qualify(
                {"kind": "test", "left": "i", "operator": "<", "right": "3"},
                "Story.MoveNext",
            ),
            {"kind": "test", "left": "Chat.streamingCoroutine", "operator": "==", "right": "null"},
        ],
    }
    assert observable.unreadable_atoms(partly)
    assert observable.readable_atoms(partly) == ["Chat.streamingCoroutine == null"]

    none = observable.qualify(
        {"kind": "test", "left": "i", "operator": "<", "right": "3"}, "Story.MoveNext"
    )
    assert observable.unreadable_atoms(none)
    assert observable.readable_atoms(none) == []
