from typing import Annotated

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.agents.qa.arch import QaArchError, QaArchSpec, resolve_arch
from app.agents.qa.arch_options import LaunchOption, launch_options
from app.main import app


def test_a_new_marked_field_supplies_its_own_validation_bounds():
    class ExampleSpec(BaseModel):
        size: Annotated[int, LaunchOption("a", ko="크기", en="Size")] = Field(ge=2, le=17)
        internal: bool = True

    assert launch_options(ExampleSpec).model_dump(by_alias=True) == {"options": [{
        "key": "size", "labelCode": "a", "kind": "number", "min": 2, "max": 17,
        "description": {"ko": "크기", "en": "Size"}, "engines": ("agent",),
    }]}


def test_launch_options_preserve_home_labels_and_only_expose_selected_fields():
    response = TestClient(app).get("/internal/qa-arch-options")
    assert response.status_code == 200
    options = response.json()["options"]
    assert [(option["key"], option["labelCode"]) for option in options] == [
        ("phase_cycle", "a"), ("skills", "b"), ("macros", "c"), ("screen_capture", "d"),
        ("vision", "e"), ("pulse_relevance", "f"), ("report_step_view", "g"),
        ("fold_stale_scenes", "h"), ("fold_stale_knowledge", "i"), ("fold_stale_skills", "j"),
        ("fold_stale_macro_results", "k"), ("fold_threshold_chars", "l"),
        ("compaction", "m"), ("code_mode", "n"),
    ]
    for option in options:
        assert option["engines"] == ["agent"]
        assert all(option["description"][language] for language in ("en", "ko"))
        assert "default" not in option


def test_every_catalog_value_can_be_submitted_with_its_wire_type():
    options = TestClient(app).get("/internal/qa-arch-options").json()["options"]
    by_key = {option["key"]: option for option in options}
    # 선택값 순번도 Home run label의 일부다.
    choices = {
        "phase_cycle": ["off", "in_verdict", "lite", "full"],
        "skills": ["off", "on_demand"],
        "macros": ["off", "on"],
        "screen_capture": ["on_demand", "every_call", "by_role"],
        "vision": ["auto", "on", "off"],
    }
    for key, values in choices.items():
        assert by_key[key]["kind"] == "choice"
        assert by_key[key]["values"] == values
        for value in values:
            assert getattr(QaArchSpec.model_validate({key: value}), key) == value
    assert by_key["screen_capture"]["visionOnlyValues"] == ["every_call", "by_role"]
    assert by_key["vision"]["visionOnlyValues"] == ["on"]
    assert by_key["screen_capture"]["unavailableWhen"] == [
        {"key": "vision", "equals": "off", "values": ["every_call", "by_role"]},
    ]
    for value in choices["screen_capture"]:
        selection = QaArchSpec(vision="off", screen_capture=value)
        if value in by_key["screen_capture"]["unavailableWhen"][0]["values"]:
            with pytest.raises(QaArchError, match="needs vision"):
                resolve_arch(selection, "openai/gpt-6-luna")
        else:
            assert resolve_arch(selection, "openai/gpt-6-luna").screen_capture == value

    threshold = by_key["fold_threshold_chars"]
    assert threshold["kind"] == "number"
    assert (threshold["min"], threshold["max"]) == (0, 10_000_000)
    for value in (threshold["min"], threshold["max"]):
        assert QaArchSpec(fold_threshold_chars=value).fold_threshold_chars == value
    for option in options:
        if option["key"] not in (*choices, "fold_threshold_chars"):
            assert option["kind"] == "toggle"
            for value in (True, False):
                assert getattr(QaArchSpec.model_validate({option["key"]: value}), option["key"]) is value
