import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.deployment import new_session_id
from app.main import create_app
from tests.test_qa_prompt_version import open_request


@pytest.fixture
def slotted(monkeypatch):
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DEPLOYMENT_SLOT", "blue")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        get_settings.cache_clear()


def test_qa_session_waiting_for_websocket_prevents_drain(slotted):
    path = "/internal/deployment/status"
    assert slotted.get(path).json() == {"slot": "blue", "idle": True}
    response = slotted.post("/internal/qa-sessions", json=open_request())
    assert response.status_code == 200, response.text
    session_id = response.json()["session_id"]
    assert re.fullmatch(r"blue-[0-9a-f]{32}", session_id)
    assert slotted.get(path).json()["idle"] is False
    asyncio.run(slotted.app.state.qa_session_service.close(session_id))
    assert slotted.get(path).json()["idle"] is True


def test_direct_deployment_preserves_session_id_format(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT_SLOT", raising=False)
    get_settings.cache_clear()
    assert re.fullmatch(r"[0-9a-f]{32}", new_session_id())
