import asyncio

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableLambda

from app.agents import (
    AgentContext,
    OutputLanguage,
    ScenarioAgent,
    ScenarioAgentRequest,
    ScenarioAgentResult,
    ScenarioDraft,
    ScenarioGenerationError,
    ScenarioStep,
)
from app.agents.scenario.prompt import LANGUAGE_DIRECTIVES, build_chain_inputs
from app.llm.chat_model import select_structured_method
from app.llm.models import LLMModel


def _result(message: str = "Created the first draft.") -> ScenarioAgentResult:
    return ScenarioAgentResult(
        message=message,
        scenario=ScenarioDraft(
            title="Login reward flow",
            description="Verify the Unity game login reward flow.",
            steps=[
                ScenarioStep(
                    step=1,
                    title="Launch game",
                    state="The Unity client is installed and not yet running.",
                    action="Start the Unity client and wait for the lobby.",
                    expected="The lobby is displayed without errors.",
                )
            ],
        ),
    )


def _canned_factory(result: ScenarioAgentResult):
    return lambda model: RunnableLambda(lambda _inputs: result)


def _request(**overrides) -> ScenarioAgentRequest:
    base = {
        "user_input": "Create a login reward QA scenario.",
        "game_context": {"constraints": ["Reward can be claimed once per day."]},
        "unity_context": {"states": [{"key": "login_reward.claimed"}]},
    }
    base.update(overrides)
    return ScenarioAgentRequest(**base)


_CTX = AgentContext(session_id="session-1")


def test_scenario_agent_returns_structured_result() -> None:
    result = _result()
    agent = ScenarioAgent(structured_factory=_canned_factory(result))

    out = asyncio.run(agent.run(_request(), _CTX))

    assert isinstance(out, ScenarioAgentResult)
    assert out.message == "Created the first draft."
    assert out.scenario.steps[0].state == "The Unity client is installed and not yet running."


def test_scenario_agent_retries_on_parse_error_then_succeeds() -> None:
    result = _result()
    calls = {"n": 0}

    def flaky(_inputs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OutputParserException("bad json")
        return result

    agent = ScenarioAgent(structured_factory=lambda model: RunnableLambda(flaky))

    out = asyncio.run(agent.run(_request(), _CTX))

    assert out.message == "Created the first draft."
    assert calls["n"] == 2  # retried once


def test_scenario_agent_raises_after_exhausting_retries() -> None:
    def always_fail(_inputs):
        raise OutputParserException("still bad")

    agent = ScenarioAgent(structured_factory=lambda model: RunnableLambda(always_fail))

    with pytest.raises(ScenarioGenerationError):
        asyncio.run(agent.run(_request(), _CTX))


def test_chain_inputs_use_requested_language_directive() -> None:
    ko_inputs = build_chain_inputs(_request(locale=OutputLanguage.ko))
    en_inputs = build_chain_inputs(_request(locale=OutputLanguage.en))

    assert ko_inputs["language_directive"] == LANGUAGE_DIRECTIVES[OutputLanguage.ko]
    assert en_inputs["language_directive"] == LANGUAGE_DIRECTIVES[OutputLanguage.en]
    assert "한국어" in ko_inputs["language_directive"]
    assert "English" in en_inputs["language_directive"]


def test_chain_inputs_default_to_korean_directive() -> None:
    inputs = build_chain_inputs(_request())

    assert inputs["language_directive"] == LANGUAGE_DIRECTIVES[OutputLanguage.ko]


def test_language_directives_cover_every_language() -> None:
    # Guards against adding an OutputLanguage member without a directive, which
    # would raise KeyError at request time instead of silently defaulting.
    assert set(LANGUAGE_DIRECTIVES) == set(OutputLanguage)


def test_select_structured_method_by_model() -> None:
    assert select_structured_method(LLMModel.gpt_4o_mini) == "json_schema"
    assert select_structured_method(LLMModel.gemma_4_free) == "json_mode"


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
