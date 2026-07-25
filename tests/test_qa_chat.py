import asyncio

from app.agents.qa import QaActResult, QaChatResult, QaPlannedAction, QaVerifyResult
from app.agents.scenario import ScenarioDraft, ScenarioStep
from app.qa.envelope import MessageType
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore


def _scenario() -> ScenarioDraft:
    return ScenarioDraft(
        title="Shop purchase flow",
        description="Verify the shop purchase flow.",
        steps=[
            ScenarioStep(
                step=1,
                title="Open shop",
                state="The player is on the lobby screen.",
                action="Tap the shop button.",
                expected="The shop screen is displayed.",
            )
        ],
    )


class RecordingAgent:
    """Canned agent that keeps the requests it was given, for asserting on them."""

    def __init__(self) -> None:
        self.act_requests = []
        self.chat_requests = []

    async def act(self, request, context) -> QaActResult:
        self.act_requests.append(request)
        return QaActResult(
            thought="Tapping the shop button.",
            action_message="Tap shop",
            actions=[QaPlannedAction(method="button_click", target_id=1)],
        )

    async def evaluate(self, request, context) -> QaVerifyResult:
        return QaVerifyResult(reasoning="Shop is open.", passed=True, verdict_message="Passed")

    async def respond(self, request, context) -> QaChatResult:
        self.chat_requests.append(request)
        return QaChatResult(reply="Understood — I will use the menu instead.")


def _service() -> tuple[QaExecutionService, RecordingAgent, InMemoryQaSessionStore]:
    store = InMemoryQaSessionStore()
    agent = RecordingAgent()
    return QaExecutionService(store=store, agent=agent), agent, store


def _open(service: QaExecutionService) -> str:
    return asyncio.run(
        service.open(
            qa_try_id=7,
            game_instance_id=8,
            test_scenario_id=9,
            scenario=_scenario(),
        )
    )


def _chat(service: QaExecutionService, session_id: str, message: str):
    return asyncio.run(
        service.on_chat(session_id, {"type": "CHAT", "payload": {"message": message}})
    )


def test_chat_replies_and_records_both_turns() -> None:
    service, _, store = _service()
    session_id = _open(service)

    output = _chat(service, session_id, "Use the menu, not the button.")

    assert len(output.frames) == 1
    assert output.frames[0]["type"] == MessageType.CHAT.value
    assert output.frames[0]["payload"]["message"] == "Understood — I will use the menu instead."
    assert output.terminal is False

    record = asyncio.run(store.load(session_id))
    assert [turn.role for turn in record.chat] == ["USER", "AGENT"]
    assert record.chat[0].message == "Use the menu, not the button."


def test_chat_turn_records_the_step_number_not_the_step() -> None:
    """Regression: the step object was passed where its number belongs."""
    service, _, store = _service()
    session_id = _open(service)

    _chat(service, session_id, "Anything.")

    record = asyncio.run(store.load(session_id))
    assert record.chat[0].step == 1
    assert record.chat[1].step == 1


def test_chat_reaches_the_next_action() -> None:
    """The point of the feature: what the operator says steers the next act."""
    service, agent, _ = _service()
    session_id = _open(service)

    _chat(service, session_id, "Use the menu, not the button.")
    asyncio.run(
        service.on_game_state(
            session_id,
            {"type": "GAME_STATE", "payload": {"scene": "lobby", "interactables": [], "observables": {}}},
        )
    )

    assert len(agent.act_requests) == 1
    carried = agent.act_requests[0].chat
    assert [turn.role for turn in carried] == ["USER", "AGENT"]
    assert carried[0].message == "Use the menu, not the button."


def test_chat_before_any_scene_still_answers() -> None:
    """A question asked before the first GAME_STATE must not blow up on a null scene."""
    service, agent, _ = _service()
    session_id = _open(service)

    output = _chat(service, session_id, "What are you about to do?")

    assert output.frames[0]["payload"]["message"]
    assert agent.chat_requests[0].game_state is None
