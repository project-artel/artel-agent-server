import asyncio

from app.agents import AgentContext, ScenarioAgent
from app.agents.scenario_schemas import (
    ScenarioAgentRequest,
    ScenarioAgentResult,
    ScenarioContext,
    ScenarioDraft,
    ScenarioStep,
)
from app.llm.client import LLMClient
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.llm.schemas import LLMRequest, LLMResponse


class FakeLLMClient(LLMClient):
    def __init__(self, response: str) -> None:
        self.last_request: LLMRequest | None = None
        self._response = response

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.last_request = request
        return LLMResponse(model=request.model, content=self._response)


def test_scenario_agent_returns_valid_result() -> None:
    llm = FakeLLMClient(
        """
        {
          "message": "Created the first draft.",
          "scenario": {
            "title": "Login reward flow",
            "description": "Verify the Unity game login reward flow.",
            "steps": [
              {
                "step": 1,
                "title": "Launch game",
                "state": "The Unity client is installed and not yet running.",
                "action": "Start the Unity client and wait for the lobby.",
                "expected": "The lobby is displayed without errors."
              }
            ]
          }
        }
        """
    )
    agent = ScenarioAgent()
    context = AgentContext(session_id="session-1", llm=llm)
    request = ScenarioAgentRequest(
        user_input="Create a login reward QA scenario.",
        context=ScenarioContext(
            states=[
                {
                    "key": "login_reward.claimed",
                    "description": "Whether the login reward was claimed.",
                }
            ],
            events=[
                {
                    "name": "RewardClaimed",
                    "description": "Raised after a reward is claimed.",
                }
            ],
            functions=[
                {
                    "name": "LoginRewardButton.OnClick",
                    "description": "Claims the current login reward.",
                }
            ],
            facts=[
                {
                    "id": "fact-1",
                    "content": "The day 1 login reward grants 100 gold.",
                }
            ],
        ),
    )

    result = asyncio.run(agent.run(request, context))

    assert isinstance(result, ScenarioAgentResult)
    assert result.message == "Created the first draft."
    assert result.scenario.title == "Login reward flow"
    assert result.scenario.steps[0].step == 1
    assert result.scenario.steps[0].expected == "The lobby is displayed without errors."
    assert result.scenario.steps[0].state == "The Unity client is installed and not yet running."
    assert llm.last_request is not None
    assert llm.last_request.model == DEFAULT_MODEL.value
    # A strict-capable default model must request a strict json_schema.
    assert llm.last_request.response_format is not None
    assert llm.last_request.response_format["type"] == "json_schema"
    assert llm.last_request.response_format["json_schema"]["strict"] is True


def test_scenario_agent_falls_back_to_json_object_for_non_strict_model() -> None:
    llm = FakeLLMClient(
        """
        {
          "message": "ok",
          "scenario": {
            "title": "t",
            "description": "d",
            "steps": [
              {
                "step": 1,
                "title": "Launch game",
                "state": "The Unity client is installed and not yet running.",
                "action": "Start the Unity client.",
                "expected": "The lobby is displayed."
              }
            ]
          }
        }
        """
    )
    agent = ScenarioAgent(model=LLMModel.gemma_4_free)
    context = AgentContext(session_id="session-2", llm=llm)
    request = ScenarioAgentRequest(
        user_input="Create a scenario.",
        context=ScenarioContext(),
    )

    asyncio.run(agent.run(request, context))

    assert llm.last_request is not None
    assert llm.last_request.model == LLMModel.gemma_4_free.value
    assert llm.last_request.response_format == {"type": "json_object"}


def test_scenario_draft_rejects_duplicate_step_numbers() -> None:
    step = ScenarioStep(
        step=1,
        title="Launch game",
        state="The Unity client is installed and not yet running.",
        action="Start the Unity client.",
        expected="The lobby is displayed.",
    )

    try:
        ScenarioDraft(title="Duplicate step", description="Invalid flow.", steps=[step, step])
    except ValueError as exc:
        assert "Scenario step numbers must be unique." in str(exc)
    else:
        raise AssertionError("Expected duplicate step number to fail validation.")


def test_scenario_draft_rejects_non_sequential_step_numbers() -> None:
    steps = [
        ScenarioStep(
            step=1,
            title="Launch game",
            state="The Unity client is installed and not yet running.",
            action="Start the Unity client.",
            expected="The lobby is displayed.",
        ),
        ScenarioStep(
            step=3,
            title="Claim reward",
            state="The player is on the lobby screen.",
            action="Tap the login reward button.",
            expected="The reward is added to inventory.",
        ),
    ]

    try:
        ScenarioDraft(title="Missing step", description="Invalid flow.", steps=steps)
    except ValueError as exc:
        assert "Scenario steps must be numbered sequentially from 1." in str(exc)
    else:
        raise AssertionError("Expected non-sequential steps to fail validation.")
