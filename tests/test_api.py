from fastapi.testclient import TestClient

from app.main import app


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
