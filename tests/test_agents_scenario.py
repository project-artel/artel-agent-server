import asyncio

import pytest

from app.agents import (
    AgentContext,
    ScenarioAgent,
    ScenarioAgentRequest,
    ScenarioAgentResult,
    ScenarioDraft,
    ScenarioGenerationError,
    ScenarioStep,
)
from app.llm.client import LLMClient
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.llm.schemas import LLMRequest, LLMResponse


_VALID_RESPONSE = """
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


class FakeLLMClient(LLMClient):
    def __init__(self, response: str) -> None:
        self.last_request: LLMRequest | None = None
        self._response = response

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.last_request = request
        return LLMResponse(model=request.model, content=self._response)


class SequenceLLMClient(LLMClient):
    """Returns queued responses in order, one per complete() call."""

    def __init__(self, responses: list[str]) -> None:
        self.requests: list[LLMRequest] = []
        self._responses = responses

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        content = self._responses[len(self.requests) - 1]
        return LLMResponse(model=request.model, content=content)


def _request(**overrides) -> ScenarioAgentRequest:
    base = {
        "user_input": "Create a login reward QA scenario.",
        "game_context": {"constraints": ["Reward can be claimed once per day."]},
        "unity_context": {"states": [{"key": "login_reward.claimed"}]},
    }
    base.update(overrides)
    return ScenarioAgentRequest(**base)


def test_scenario_agent_returns_valid_result() -> None:
    llm = FakeLLMClient(_VALID_RESPONSE)
    agent = ScenarioAgent()
    context = AgentContext(session_id="session-1", llm=llm)

    result = asyncio.run(agent.run(_request(), context))

    assert isinstance(result, ScenarioAgentResult)
    assert result.message == "Created the first draft."
    assert result.scenario.steps[0].step == 1
    assert result.scenario.steps[0].state == "The Unity client is installed and not yet running."
    assert llm.last_request is not None
    assert llm.last_request.model == DEFAULT_MODEL.value
    assert llm.last_request.response_format is not None
    assert llm.last_request.response_format["type"] == "json_schema"
    assert llm.last_request.response_format["json_schema"]["strict"] is True


def test_scenario_agent_falls_back_to_json_object_for_non_strict_model() -> None:
    llm = FakeLLMClient(_VALID_RESPONSE)
    agent = ScenarioAgent()
    context = AgentContext(session_id="session-2", llm=llm)

    asyncio.run(agent.run(_request(model=LLMModel.gemma_4_free), context))

    assert llm.last_request is not None
    assert llm.last_request.model == LLMModel.gemma_4_free.value
    assert llm.last_request.response_format == {"type": "json_object"}


def test_scenario_agent_strips_code_fence() -> None:
    llm = FakeLLMClient(f"```json\n{_VALID_RESPONSE}\n```")
    agent = ScenarioAgent()
    context = AgentContext(session_id="session-3", llm=llm)

    result = asyncio.run(agent.run(_request(), context))

    assert result.scenario.title == "Login reward flow"


def test_scenario_agent_retries_then_succeeds() -> None:
    llm = SequenceLLMClient(["not json at all", _VALID_RESPONSE])
    agent = ScenarioAgent()
    context = AgentContext(session_id="session-4", llm=llm)

    result = asyncio.run(agent.run(_request(), context))

    assert result.message == "Created the first draft."
    assert len(llm.requests) == 2
    # The retry appends the failed answer plus a correction instruction.
    assert len(llm.requests[1].messages) > len(llm.requests[0].messages)


def test_scenario_agent_raises_after_exhausting_retries() -> None:
    llm = SequenceLLMClient(["nope", "still nope"])
    agent = ScenarioAgent()
    context = AgentContext(session_id="session-5", llm=llm)

    with pytest.raises(ScenarioGenerationError):
        asyncio.run(agent.run(_request(), context))


def test_scenario_draft_rejects_duplicate_step_numbers() -> None:
    step = ScenarioStep(
        step=1,
        title="Launch game",
        state="The Unity client is installed and not yet running.",
        action="Start the Unity client.",
        expected="The lobby is displayed.",
    )

    with pytest.raises(ValueError, match="Scenario step numbers must be unique."):
        ScenarioDraft(title="Duplicate step", description="Invalid flow.", steps=[step, step])


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

    with pytest.raises(ValueError, match="numbered sequentially"):
        ScenarioDraft(title="Missing step", description="Invalid flow.", steps=steps)
