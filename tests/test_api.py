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
