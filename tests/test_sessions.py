import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.sessions import router as sessions_router
from app.llm.client import LLMClient
from app.llm.schemas import LLMRequest, LLMResponse
from app.sessions import InMemorySessionStore, SessionExpired, SessionService


_RESPONSE = """
{
  "message": "Here is the draft.",
  "scenario": {
    "title": "Shop purchase flow",
    "description": "Verify the shop purchase flow.",
    "steps": [
      {
        "step": 1,
        "title": "Open shop",
        "state": "The player is on the lobby screen with at least 100 gold.",
        "action": "Tap the shop button.",
        "expected": "The shop screen is displayed."
      }
    ]
  }
}
"""


class FakeLLMClient(LLMClient):
    def __init__(self, response: str = _RESPONSE) -> None:
        self.calls = 0
        self._response = response

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(model=request.model, content=self._response)


def _service(**kwargs) -> tuple[SessionService, FakeLLMClient, InMemorySessionStore]:
    store = InMemorySessionStore()
    llm = FakeLLMClient()
    service = SessionService(store=store, llm_client=llm, **kwargs)
    return service, llm, store


def test_open_stores_pending_input_without_generating() -> None:
    service, llm, store = _service()

    session_id = asyncio.run(
        service.open({"u": 1}, {"g": 2}, "Test the shop flow.")
    )
    record = asyncio.run(store.load(session_id))

    assert llm.calls == 0  # no generation at open
    assert record is not None
    assert record.pending_user_input == "Test the shop flow."
    assert record.unity_context == {"u": 1}


def test_first_turn_generates_and_clears_pending() -> None:
    service, llm, store = _service()
    session_id = asyncio.run(service.open({}, {}, "Test the shop flow."))

    result = asyncio.run(service.start_first_turn(session_id))

    assert llm.calls == 1
    assert result is not None
    assert result.scenario.title == "Shop purchase flow"
    record = asyncio.run(store.load(session_id))
    assert record.pending_user_input is None
    assert len(record.history) == 2  # user + assistant


def test_first_turn_returns_none_when_no_pending() -> None:
    service, _, _ = _service()
    session_id = asyncio.run(service.open({}, {}, "First input."))
    asyncio.run(service.start_first_turn(session_id))

    # A reconnect after the first turn has no pending input.
    assert asyncio.run(service.start_first_turn(session_id)) is None


def test_run_turn_appends_history_and_caps_window() -> None:
    service, _, store = _service(history_max_turns=2)  # cap = 4 messages
    session_id = asyncio.run(service.open({}, {}, "First input."))
    asyncio.run(service.start_first_turn(session_id))

    for i in range(5):
        asyncio.run(service.run_turn(session_id, f"turn {i}", draft=None))

    record = asyncio.run(store.load(session_id))
    assert len(record.history) == 4  # 2 turns * 2 messages


def test_missing_session_raises_expired() -> None:
    service, _, _ = _service()
    with pytest.raises(SessionExpired):
        asyncio.run(service.run_turn("does-not-exist", "hi", draft=None))


def test_close_deletes_session() -> None:
    service, _, store = _service()
    session_id = asyncio.run(service.open({}, {}, "First input."))

    asyncio.run(service.close(session_id))

    assert asyncio.run(store.load(session_id)) is None


def _test_app() -> tuple[FastAPI, FakeLLMClient]:
    app = FastAPI()
    app.include_router(sessions_router)
    llm = FakeLLMClient()
    app.state.session_service = SessionService(
        store=InMemorySessionStore(), llm_client=llm
    )
    return app, llm


def test_ws_flow_open_first_turn_and_turn() -> None:
    app, _ = _test_app()
    client = TestClient(app)

    opened = client.post(
        "/sessions",
        json={"unity_context": {}, "game_context": {}, "user_input": "shop flow"},
    )
    assert opened.status_code == 200
    session_id = opened.json()["session_id"]

    with client.websocket_connect(f"/sessions/{session_id}") as ws:
        first = ws.receive_json()  # first turn result on connect
        assert first["type"] == "result"
        assert first["scenario"]["title"] == "Shop purchase flow"

        ws.send_json({"type": "turn", "user_input": "make it shorter", "draft": None})
        second = ws.receive_json()
        assert second["type"] == "result"

    approved = client.post(f"/sessions/{session_id}/approve")
    assert approved.json() == {"ok": True}


def test_ws_reports_session_expired() -> None:
    app, _ = _test_app()
    client = TestClient(app)

    with client.websocket_connect("/sessions/unknown-id") as ws:
        event = ws.receive_json()
        assert event["type"] == "error"
        assert event["code"] == "session_expired"
