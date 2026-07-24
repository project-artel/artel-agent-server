import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableLambda

from app.agents import (
    OutputLanguage,
    ScenarioAgent,
    ScenarioAgentResult,
    ScenarioDraft,
    ScenarioStep,
)
from app.api.sessions import router as sessions_router
from app.sessions import InMemorySessionStore, SessionExpired, SessionService
from app.sessions.schemas import SessionRecord


def _result(message: str = "Here is the draft.") -> ScenarioAgentResult:
    return ScenarioAgentResult(
        message=message,
        scenario=ScenarioDraft(
            title="Shop purchase flow",
            description="Verify the shop purchase flow.",
            steps=[
                ScenarioStep(
                    step=1,
                    title="Open shop",
                    state="The player is on the lobby screen with at least 100 gold.",
                    action="Tap the shop button.",
                    expected="The shop screen is displayed.",
                )
            ],
        ),
    )


class CountingAgent(ScenarioAgent):
    """Agent whose structured chain returns a canned result and counts calls."""

    def __init__(self, result: ScenarioAgentResult) -> None:
        self.calls = 0

        def factory(model):
            def run(_inputs):
                self.calls += 1
                return result

            return RunnableLambda(run)

        super().__init__(structured_factory=factory)


class CapturingAgent(ScenarioAgent):
    """Records the language of the last request reaching the agent."""

    def __init__(self, result: ScenarioAgentResult) -> None:
        super().__init__(structured_factory=lambda model: None)
        self._result = result
        self.languages: list[OutputLanguage] = []

    async def run(self, request, context):  # type: ignore[override]
        self.languages.append(request.language)
        return self._result


def _service(**kwargs) -> tuple[SessionService, CountingAgent, InMemorySessionStore]:
    store = InMemorySessionStore()
    agent = CountingAgent(_result())
    service = SessionService(store=store, agent=agent, **kwargs)
    return service, agent, store


def _capturing_service() -> tuple[SessionService, CapturingAgent, InMemorySessionStore]:
    store = InMemorySessionStore()
    agent = CapturingAgent(_result())
    service = SessionService(store=store, agent=agent)
    return service, agent, store


def test_open_stores_pending_input_without_generating() -> None:
    service, agent, store = _service()

    session_id = asyncio.run(service.open({"u": 1}, {"g": 2}, "Test the shop flow."))
    record = asyncio.run(store.load(session_id))

    assert agent.calls == 0
    assert record is not None
    assert record.pending_user_input == "Test the shop flow."
    assert record.unity_context == {"u": 1}


def test_first_turn_generates_and_full_stores_history() -> None:
    service, agent, store = _service()
    session_id = asyncio.run(service.open({}, {}, "Test the shop flow."))

    result = asyncio.run(service.start_first_turn(session_id))

    assert agent.calls == 1
    assert result is not None
    record = asyncio.run(store.load(session_id))
    assert record.pending_user_input is None
    assert len(record.history) == 2
    assert record.history[0].role == "user" and record.history[0].scenario is None
    # Assistant turn keeps the full scenario (full store).
    assert record.history[1].role == "assistant"
    assert record.history[1].scenario is not None
    assert record.history[1].scenario.title == "Shop purchase flow"


def test_first_turn_returns_none_when_no_pending() -> None:
    service, _, _ = _service()
    session_id = asyncio.run(service.open({}, {}, "First input."))
    asyncio.run(service.start_first_turn(session_id))

    assert asyncio.run(service.start_first_turn(session_id)) is None


def test_run_turn_caps_history_window() -> None:
    service, _, store = _service(history_max_turns=2)  # cap = 4 messages
    session_id = asyncio.run(service.open({}, {}, "First input."))
    asyncio.run(service.start_first_turn(session_id))

    for i in range(5):
        asyncio.run(service.run_turn(session_id, f"turn {i}", draft=None))

    record = asyncio.run(store.load(session_id))
    assert len(record.history) == 4


def test_first_turn_uses_session_language() -> None:
    # Regression: the first turn runs from the stored pending input, so the
    # language set at open() must reach it — not only later WS turns.
    service, agent, _ = _capturing_service()
    session_id = asyncio.run(
        service.open({}, {}, "Test the shop flow.", language=OutputLanguage.en)
    )

    asyncio.run(service.start_first_turn(session_id))

    assert agent.languages == [OutputLanguage.en]


def test_run_turn_overrides_and_persists_language() -> None:
    service, agent, store = _capturing_service()
    session_id = asyncio.run(service.open({}, {}, "First input."))  # defaults to ko
    asyncio.run(service.start_first_turn(session_id))

    asyncio.run(
        service.run_turn(session_id, "switch", draft=None, language=OutputLanguage.en)
    )
    # Language sticks for subsequent turns without re-sending it.
    asyncio.run(service.run_turn(session_id, "again", draft=None))

    assert agent.languages == [
        OutputLanguage.ko,
        OutputLanguage.en,
        OutputLanguage.en,
    ]
    assert asyncio.run(store.load(session_id)).language == OutputLanguage.en


def test_session_record_defaults_language_when_missing() -> None:
    # Records persisted before `language` existed must still load (as Korean).
    record = SessionRecord.model_validate(
        {"unity_context": {}, "game_context": {}, "history": []}
    )

    assert record.language == OutputLanguage.ko


def test_missing_session_raises_expired() -> None:
    service, _, _ = _service()
    with pytest.raises(SessionExpired):
        asyncio.run(service.run_turn("does-not-exist", "hi", draft=None))


def test_close_deletes_session() -> None:
    service, _, store = _service()
    session_id = asyncio.run(service.open({}, {}, "First input."))

    asyncio.run(service.close(session_id))

    assert asyncio.run(store.load(session_id)) is None


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(sessions_router)
    app.state.session_service = SessionService(
        store=InMemorySessionStore(), agent=CountingAgent(_result())
    )
    return app


def test_ws_flow_open_first_turn_and_turn() -> None:
    client = TestClient(_test_app())

    opened = client.post(
        "/sessions",
        json={"unity_context": {}, "game_context": {}, "user_input": "shop flow"},
    )
    assert opened.status_code == 200
    session_id = opened.json()["session_id"]

    with client.websocket_connect(f"/sessions/{session_id}") as ws:
        first = ws.receive_json()
        assert first["type"] == "result"
        assert first["scenario"]["title"] == "Shop purchase flow"

        ws.send_json({"type": "turn", "user_input": "make it shorter", "draft": None})
        second = ws.receive_json()
        assert second["type"] == "result"

    approved = client.post(f"/sessions/{session_id}/approve")
    assert approved.json() == {"ok": True}


def test_ws_close_terminates_and_deletes_session() -> None:
    app = _test_app()
    client = TestClient(app)

    opened = client.post(
        "/sessions",
        json={"unity_context": {}, "game_context": {}, "user_input": "shop flow"},
    )
    session_id = opened.json()["session_id"]

    with client.websocket_connect(f"/sessions/{session_id}") as ws:
        assert ws.receive_json()["type"] == "result"

        ws.send_json({"type": "close"})
        closed = ws.receive_json()
        assert closed["type"] == "closed"

    # Session is gone: a fresh connection reports it as expired.
    with client.websocket_connect(f"/sessions/{session_id}") as ws:
        event = ws.receive_json()
        assert event["type"] == "error"
        assert event["code"] == "session_expired"


def test_ws_reports_session_expired() -> None:
    client = TestClient(_test_app())

    with client.websocket_connect("/sessions/unknown-id") as ws:
        event = ws.receive_json()
        assert event["type"] == "error"
        assert event["code"] == "session_expired"


def test_open_session_rejects_unknown_language() -> None:
    client = TestClient(_test_app())

    response = client.post(
        "/sessions",
        json={"user_input": "shop flow", "language": "korean"},
    )

    assert response.status_code == 422


def test_open_session_language_reaches_first_turn() -> None:
    app = FastAPI()
    app.include_router(sessions_router)
    store = InMemorySessionStore()
    agent = CapturingAgent(_result())
    app.state.session_service = SessionService(store=store, agent=agent)
    client = TestClient(app)

    opened = client.post("/sessions", json={"user_input": "shop flow", "language": "en"})
    session_id = opened.json()["session_id"]

    with client.websocket_connect(f"/sessions/{session_id}") as ws:
        assert ws.receive_json()["type"] == "result"

    assert agent.languages == [OutputLanguage.en]


def test_ws_turn_rejects_unknown_language() -> None:
    client = TestClient(_test_app())
    opened = client.post("/sessions", json={"user_input": "shop flow"})
    session_id = opened.json()["session_id"]

    with client.websocket_connect(f"/sessions/{session_id}") as ws:
        assert ws.receive_json()["type"] == "result"

        ws.send_json({"type": "turn", "user_input": "x", "language": "korean"})
        event = ws.receive_json()
        assert event["type"] == "error"
        assert event["code"] == "bad_request"
