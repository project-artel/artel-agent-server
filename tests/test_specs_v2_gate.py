"""A coroutine's input gate decides which side of it an effect happens on."""

from app.specs_v2.discovery import discover
from app.specs_v2.graph import graph_from_report

COROUTINE = "System.Boolean Demo.Story/<Tell>d__1::MoveNext()"
PREDICATE = "System.Boolean Demo.Story::<Tell>b__1_0()"
LEAVE = "System.Void Demo.Story::Leave()"
START = "System.Void Demo.Story::Start()"


def _record(source: str, path: list[str], **extra) -> dict:
    method = source.split("::")[-1].split("(")[0]
    base = {
        "schema": 6,
        "entry": path[0],
        "entryId": "Assembly-CSharp|Demo.Story|Start|System.Void()",
        "source": source,
        "methodId": f"Assembly-CSharp|Demo.Story|{method}|System.Void()",
        "recordKind": "candidate",
        "triggerKind": "lifecycle",
        "confidence": "exact",
        "callPath": path,
        "condition": {"kind": "always"},
        "inputs": [],
        "effects": [],
        "calls": [],
        "handles": [],
        "alsoReachedBy": [],
        "gaps": [],
    }
    base.update(extra)
    return base


def _report() -> dict:
    """A dialogue loop: the coroutine waits for a key, then leaves the scene.

    The leaving happens in `Leave()`, a callee of the state machine, and the key
    lives in the predicate handed to `WaitUntil`. Nothing in `Leave()`'s own
    record says an input is needed — its entry is `Start`, because that is where
    the coroutine was started.
    """
    return {
        "schema": 6,
        "capture": "editor",
        "build": {"evidence": "fixture-gate", "platform": "WindowsEditor"},
        "scenes": ["StoryScene", "MapScene"],
        "objects": [
            {
                "scene": "StoryScene",
                "path": "Story",
                "selector": "StoryScene/Story",
                "active": True,
                "components": [{"type": "Demo.Story"}],
            }
        ],
        "persistentObjects": [],
        "types": {
            "Demo.Story": [
                # The predicate the coroutine waits on. It carries the key.
                _record(
                    PREDICATE,
                    [START, COROUTINE, PREDICATE],
                    inputs=[
                        {
                            "kind": "key",
                            "control": "Space",
                            "phase": "down",
                            "absent": False,
                            "offset": 0,
                        }
                    ],
                    handedOverAt=100,
                    handedOverTo="System.Void UnityEngine.WaitUntil::.ctor(System.Func`1)",
                    gaps=["reached-through-delegate"],
                ),
                # The state machine, calling out past the handoff.
                _record(
                    COROUTINE,
                    [START, COROUTINE],
                    calls=[
                        {
                            "targetId": "Assembly-CSharp|Demo.Story|Leave|System.Void()",
                            "target": LEAVE,
                            "receiver": None,
                            "receiverWhere": None,
                            "args": None,
                            "offset": 140,
                        }
                    ],
                ),
                # What it leaves to. Its own record knows nothing about a key.
                _record(
                    LEAVE,
                    [START, COROUTINE, LEAVE],
                    effects=[
                        {
                            "kind": "scene",
                            "category": "observable",
                            "target": "MapScene",
                            "detail": None,
                            "source": LEAVE,
                            "offset": 10,
                        }
                    ],
                ),
            ]
        },
        "unplaced": {},
        "gaps": [],
    }


def _leaving(result):
    return [
        contract
        for contract in result.contracts
        if any(item.target == "MapScene" for item in contract.assertions)
    ]


def test_an_effect_past_the_gate_is_reached_by_the_input() -> None:
    result = discover(graph_from_report(_report(), source="test"))

    contracts = _leaving(result)
    assert contracts, "leaving the scene should be discovered"
    assert any(contract.trigger.kind == "input" for contract in contracts)


def test_the_scene_entry_reading_of_the_same_effect_is_not_kept_beside_it() -> None:
    """One row saying a key is needed and one saying only looking is, is a pair
    a reader has to choose between with nothing to choose on."""
    result = discover(graph_from_report(_report(), source="test"))

    kinds = {contract.trigger.kind for contract in _leaving(result)}
    assert "input" in kinds
    assert "scene_entry" not in kinds
