"""The authoring WS dispatch: case-search frames vs turns, mid-turn.

The turn now runs as a task while the handler keeps reading, so the agent's
`test_case_search` frame is answered mid-turn. This exercises that routing end to
end: a `test_case_search_result` reaches the channel, while a `turn` arriving
mid-turn is rejected as busy. Mirrors the QA dispatch coverage.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents import ScenarioAgent, ScenarioAgentResult, ScenarioPlan
from app.api.sessions import router as sessions_router
from app.sessions import InMemorySessionStore, SessionService
from app.sessions.channel import TestCaseSearchResult


class SearchingAgent(ScenarioAgent):
    """An agent whose turn makes one case search and maps the hits it gets."""

    def __init__(self) -> None:
        super().__init__(agent_factory=lambda **_kwargs: None)

    async def run(self, request, context, channel):  # type: ignore[override]
        answer = await channel.search_test_cases("shop cases", None, 10)
        if isinstance(answer, TestCaseSearchResult) and answer.results:
            return ScenarioAgentResult(
                message="Authored a scenario.",
                scenarios=[
                    ScenarioPlan(
                        title="Shop",
                        description="Verify shop.",
                        case_ids=[int(hit.id) for hit in answer.results],
                    )
                ],
            )
        return ScenarioAgentResult(message="No matching cases.", scenarios=[])


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(sessions_router)
    app.state.session_service = SessionService(
        store=InMemorySessionStore(), agent=SearchingAgent()
    )
    return app


def _open(client: TestClient) -> str:
    opened = client.post("/sessions", json={"user_input": "author shop scenarios"})
    return opened.json()["session_id"]


def test_search_result_routes_to_the_channel_and_a_turn_is_busy() -> None:
    client = TestClient(_app())
    session_id = _open(client)

    with client.websocket_connect(f"/sessions/{session_id}") as ws:
        # The first turn started and the agent asked for cases.
        frame = ws.receive_json()
        assert frame["type"] == "test_case_search"
        assert frame["query"] == "shop cases"
        message_id = frame["messageId"]

        # A turn arriving while one is in flight is rejected, not started.
        ws.send_json({"type": "turn", "user_input": "another"})
        busy = ws.receive_json()
        assert busy["type"] == "error"
        assert busy["code"] == "busy"

        # The search reply reaches the channel; the turn completes on it.
        ws.send_json(
            {
                "type": "test_case_search_result",
                "correlationId": message_id,
                "results": [
                    {
                        "id": "7",
                        "category": "SHOP",
                        "title": "Buy",
                        "expected": "bought",
                        "verificationStatus": "VERIFIED",
                        "score": 0.8,
                    }
                ],
            }
        )
        result = ws.receive_json()
        assert result["type"] == "result"
        assert result["scenarios"][0]["case_ids"] == [7]


def test_an_unsupported_inbound_frame_is_reported() -> None:
    client = TestClient(_app())
    session_id = _open(client)

    with client.websocket_connect(f"/sessions/{session_id}") as ws:
        # Drain the first turn's search frame so the socket is otherwise idle.
        assert ws.receive_json()["type"] == "test_case_search"

        ws.send_json({"type": "nonsense"})
        event = ws.receive_json()
        assert event["type"] == "error"
        assert event["code"] == "bad_request"
