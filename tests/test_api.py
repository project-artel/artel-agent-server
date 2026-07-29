import pytest
from fastapi.testclient import TestClient

from app.main import app, create_app
from app.prompts import loader


client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_agent_run_api_is_not_configured_yet() -> None:
    response = client.post("/agents/runs", json={})

    assert response.status_code == 404


def test_openapi_contract_describes_the_agent_server_api() -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"] == {
        "title": "Artel Agent Server API",
        "description": "API contract for Artel scenario generation, QA execution, and bug report workflows.",
        "version": "0.1.0",
    }
    assert "/health" in schema["paths"]
    assert schema["paths"]["/health"]["get"]["tags"] == ["system"]


def test_opening_a_qa_session_offers_an_optional_prompt_version() -> None:
    schema = client.get("/openapi.json").json()

    request_body = schema["components"]["schemas"]["OpenQaSessionRequest"]

    assert "prompt_version" in request_body["properties"]
    # Optional, so every caller written before ARTEL-179 keeps working.
    assert "prompt_version" not in request_body.get("required", [])


def test_the_app_refuses_to_start_on_a_prompt_version_that_does_not_exist() -> None:
    """Boot is the last moment a bad prompt is cheap to notice."""

    class BadSettings:
        qa_prompt_version = "v999"
        scenario_prompt_version = None
        game_context_prompt_version = None
        knowledge_query_prompt_version = None

    original = loader.get_settings
    loader.get_settings = lambda: BadSettings()
    loader.clear_prompt_cache()
    try:
        with pytest.raises(loader.PromptError, match="'v999' does not exist"):
            create_app()
    finally:
        loader.get_settings = original
        loader.clear_prompt_cache()
