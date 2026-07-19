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
    assert llm.last_request is not None
    assert llm.last_request.model == "openrouter/auto"


def test_scenario_draft_rejects_duplicate_step_numbers() -> None:
    step = ScenarioStep(
        step=1,
        title="Launch game",
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
            action="Start the Unity client.",
            expected="The lobby is displayed.",
        ),
        ScenarioStep(
            step=3,
            title="Claim reward",
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
