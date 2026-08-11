from copy import deepcopy

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.specs_v2 import router


def _record(
    method: str,
    *,
    effect: dict,
    inputs: list[dict] | None = None,
    condition: dict | None = None,
) -> dict:
    signature = f"System.Void Demo.PlayerController::{method}()"
    return {
        "schema": 6,
        "entry": signature,
        "entryId": (
            f"Assembly-CSharp|Demo.PlayerController|{method}|System.Void()"
        ),
        "source": signature,
        "methodId": signature,
        "recordKind": "candidate",
        "triggerKind": "unity-message",
        "confidence": "exact",
        "callPath": [signature],
        "condition": condition or {"kind": "always"},
        "inputs": inputs or [],
        "effects": [
            effect,
            {
                "kind": "write",
                "category": "state",
                "target": f"PlayerController.{method}Observed",
                "detail": "true",
                "offset": effect["offset"] + 1,
            },
        ],
        "calls": [],
        "handles": [],
        "alsoReachedBy": [],
        "gaps": [],
    }


def _sdk_report(*, capture: str = "editor", development: bool = False) -> dict:
    play_signature = "System.Void Demo.MenuController::Play()"
    return {
        "schema": 6,
        "capture": capture,
        "build": {
            "evidence": f"fixture-{capture}",
            "platform": "WindowsPlayer" if development else "WindowsEditor",
            "development": development,
        },
        "scenes": ["MenuScene", "PlayScene"],
        "objects": [
            {
                "scene": "MenuScene",
                "path": "Canvas/PlayButton",
                "selector": "MenuScene/Canvas/PlayButton",
                "active": True,
                "label": "Play",
                "components": [
                    {
                        "type": "UnityEngine.UI.Button",
                        "calls": [
                            {
                                "targetType": "Demo.MenuController",
                                "method": "Play",
                                "event": "m_OnClick",
                            }
                        ],
                    }
                ],
            },
            {
                "scene": "MenuScene",
                "path": "MenuController",
                "selector": "MenuScene/MenuController",
                "active": True,
                "components": [{"type": "Demo.MenuController"}],
            },
            {
                "scene": "PlayScene",
                "path": "Player",
                "selector": "PlayScene/Player",
                "active": True,
                "components": [{"type": "Demo.PlayerController"}],
            },
        ],
        "persistentObjects": [],
        "types": {
            "Demo.MenuController": [
                {
                    "schema": 6,
                    "entry": play_signature,
                    "entryId": (
                        "Assembly-CSharp|Demo.MenuController|Play|System.Void()"
                    ),
                    "source": play_signature,
                    "methodId": play_signature,
                    "recordKind": "candidate",
                    "triggerKind": "unity-event",
                    "confidence": "exact",
                    "callPath": [play_signature],
                    "condition": {"kind": "always"},
                    "inputs": [],
                    "effects": [
                        {
                            "kind": "scene",
                            "category": "observable",
                            "target": "PlayScene",
                            "detail": None,
                            "offset": 1,
                        },
                        {
                            "kind": "write",
                            "category": "state",
                            "target": "MenuController.playRequested",
                            "detail": "true",
                            "offset": 2,
                        },
                    ],
                    "calls": [],
                    "handles": [],
                    "alsoReachedBy": [],
                    "gaps": [],
                }
            ],
            "Demo.PlayerController": [
                _record(
                    "Update",
                    inputs=[
                        {
                            "kind": "key",
                            "control": "Space",
                            "phase": "down",
                            "absent": False,
                            "offset": 2,
                        }
                    ],
                    condition={"kind": "gesture", "input": "key:Space:down"},
                    effect={
                        "kind": "instantiate",
                        "category": "observable",
                        "target": "PlayerController.projectilePrefab",
                        "detail": None,
                        "offset": 3,
                    },
                ),
                _record(
                    "ShowScore",
                    inputs=[
                        {
                            "kind": "key",
                            "control": "S",
                            "phase": "down",
                            "absent": False,
                            "offset": 4,
                        }
                    ],
                    effect={
                        "kind": "ui-value",
                        "category": "observable",
                        "target": "PlayerController.scoreText",
                        "detail": "System.Int32.ToString()",
                        "offset": 5,
                    },
                ),
                _record(
                    "ShowMissing",
                    inputs=[
                        {
                            "kind": "key",
                            "control": "R",
                            "phase": "down",
                            "absent": False,
                            "offset": 6,
                        }
                    ],
                    effect={
                        "kind": "ui-value",
                        "category": "observable",
                        "target": "Missing.value",
                        "detail": '"broken"',
                        "offset": 7,
                    },
                ),
            ],
        },
        "unplaced": {},
        "gaps": [],
    }


app = FastAPI()
app.include_router(router, prefix="/internal")
client = TestClient(app)


def test_generate_accepts_one_raw_sdk_report() -> None:
    response = client.post("/internal/specs/v2/generate", json=_sdk_report())

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "spec-discovery.v2"
    assert payload["artifact"] == "editor"
    assert payload["capture"] == "editor"
    assert payload["summary"]["ready_specs"] >= 2
    assert payload["summary"]["candidate_specs"] >= 1
    assert payload["summary"]["review_specs"] >= 1
    assert any(
        row["expected_result"] == "`PlayScene` 화면으로 전환된다"
        for row in payload["ready_specs"]
    )
    key_steps = [
        row["test_step"]
        for row in payload["ready_specs"]
        if "Space" in row["test_step"]
    ]
    assert key_steps
    assert all("Space:down" not in step for step in key_steps)
    assert any(
        row["status"] == "candidate"
        and "ambiguous_expected_value" in row["review_reason"]
        for row in payload["ready_specs"]
    )
    assert any(
        "unresolved_target" in row["review_reason"]
        for row in payload["review_specs"]
    )


def test_generation_is_deterministic_for_the_same_report() -> None:
    report = _sdk_report()

    first = client.post("/internal/specs/v2/generate", json=report)
    second = client.post("/internal/specs/v2/generate", json=deepcopy(report))

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


def test_development_build_is_analyzed_without_editor_data() -> None:
    response = client.post(
        "/internal/specs/v2/generate",
        json=_sdk_report(capture="player", development=True),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["artifact"] == "devbuild"
    assert payload["capture"] == "player"
    assert payload["build_evidence"] == "fixture-player"
    assert {
        row["capture"]
        for row in payload["ready_specs"] + payload["review_specs"]
    } == {"player"}


def test_unsupported_sdk_schema_is_rejected() -> None:
    response = client.post(
        "/internal/specs/v2/generate",
        json={"schema": 4},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "unsupported SDK schema: 4"


def test_schema_five_remains_supported() -> None:
    report = _sdk_report()
    report["schema"] = 5

    response = client.post("/internal/specs/v2/generate", json=report)

    assert response.status_code == 200
    assert response.json()["schema_version"] == "spec-discovery.v2"


def test_openapi_publishes_the_raw_object_contract() -> None:
    operation = client.get("/openapi.json").json()["paths"][
        "/internal/specs/v2/generate"
    ]["post"]

    request_schema = operation["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert operation["tags"] == ["specs-v2"]
    assert request_schema["type"] == "object"
    assert request_schema["additionalProperties"] is True
