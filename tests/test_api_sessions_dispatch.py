"""The authoring WS dispatch: case-search frames vs turns, mid-turn.

The turn now runs as a task while the handler keeps reading, so the agent's
`test_case_search` frame is answered mid-turn. This exercises that routing end to
end: a `test_case_search_result` reaches the channel, while a `turn` arriving
mid-turn is rejected as busy. Mirrors the QA dispatch coverage.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents import ScenarioAgent, ScenarioAgentResult, ScenarioPlan
from app.agents.scenario.schemas import AuthoredStep
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
                        steps=[
                            AuthoredStep(action=f"Verify case {hit.id}", case_id=int(hit.id))
                            for hit in answer.results
                        ],
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
        assert [step["case_id"] for step in result["scenarios"][0]["steps"]] == [7]


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


class ExplodingAgent(ScenarioAgent):
    """A turn that fails in a way nobody wrote a handler for."""

    def __init__(self) -> None:
        super().__init__(agent_factory=lambda **_kwargs: None)

    async def run(self, request, context, channel):  # type: ignore[override]
        raise RuntimeError("upstream went away")


def test_an_unhandled_turn_failure_is_reported_instead_of_hanging() -> None:
    """A turn that raises something unexpected must still end the turn (ARTEL-510).

    Before this, only `SessionExpired`, `ScenarioGenerationError` and
    `openai.APIError` were caught. Anything else escaped the dispatch loop and
    closed the socket with no frame sent — the client saw a request that never
    finished and no reason. That is what a user reported: an authoring turn still
    "requesting" 500 s in, recoverable only by reloading the page.
    """
    app = FastAPI()
    app.include_router(sessions_router)
    app.state.session_service = SessionService(
        store=InMemorySessionStore(), agent=ExplodingAgent()
    )
    client = TestClient(app)
    session_id = _open(client)

    with client.websocket_connect(f"/sessions/{session_id}") as ws:
        frame = ws.receive_json()

    assert frame["type"] == "error"
    assert frame["code"] == "turn_failed"


def test_cancel_ends_the_turn_and_the_session_takes_the_next_one() -> None:
    """ESC 가 하는 일(ARTEL-954): 기다림은 끝나고 대화는 남는다.

    `close` 와 갈라 둔 값어치가 두 번째 단정에 있다 — 끊은 뒤에 보낸 말이 `busy` 로 막히지도,
    세션이 없어 만료로 떨어지지도 않고 평소대로 한 턴을 돈다.
    """
    client = TestClient(_app())
    session_id = _open(client)

    with client.websocket_connect(f"/sessions/{session_id}") as ws:
        # 첫 턴이 케이스를 묻고 답을 기다린다 — 여기서 끊는다.
        assert ws.receive_json()["type"] == "test_case_search"

        ws.send_json({"type": "cancel"})
        cancelled = ws.receive_json()
        assert cancelled == {"type": "cancelled", "was_running": True}

        # 끊긴 턴은 결과도 오류도 보내지 않는다. 다음 말이 새 턴으로 받아들여지는 것이
        # 그 증거다(`busy` 였다면 앞엣것이 아직 돈다는 뜻이다).
        ws.send_json({"type": "turn", "user_input": "다시 해 줘"})
        again = ws.receive_json()
        assert again["type"] == "test_case_search"


def test_cancel_with_nothing_running_says_so() -> None:
    """한가할 때의 ESC. 오류가 아니다 — 끊을 것이 없었다고만 답한다."""
    client = TestClient(_app())
    session_id = _open(client)

    with client.websocket_connect(f"/sessions/{session_id}") as ws:
        # 첫 턴을 끝까지 돌려 세션을 한가하게 만든다.
        search = ws.receive_json()
        ws.send_json(
            {
                "type": "test_case_search_result",
                "correlationId": search["messageId"],
                "results": [],
            }
        )
        assert ws.receive_json()["type"] == "result"

        ws.send_json({"type": "cancel"})
        assert ws.receive_json() == {"type": "cancelled", "was_running": False}
