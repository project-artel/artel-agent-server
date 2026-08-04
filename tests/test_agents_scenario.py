import asyncio

import pytest
from langchain_core.runnables import RunnableLambda
from langchain_core.tracers.context import collect_runs

from app.agents import (
    AgentContext,
    OutputLanguage,
    ScenarioAgent,
    ScenarioAgentRequest,
    ScenarioAgentResult,
    ScenarioGenerationError,
    ScenarioPlan,
)
from app.agents.scenario.prompt import (
    LANGUAGE_DIRECTIVES,
    build_first_message,
    build_system_prompt,
)
from app.llm.chat_model import select_structured_method
from app.llm.models import LLMModel
from app.sessions.channel import ScenarioChannel


def _result(message: str = "Authored two scenarios.") -> ScenarioAgentResult:
    return ScenarioAgentResult(
        message=message,
        scenarios=[
            ScenarioPlan(
                title="Login reward flow",
                description="Verify the login reward flow.",
                case_ids=[11, 12],
            ),
            ScenarioPlan(
                title="Shop purchase flow",
                description="Verify the shop purchase flow.",
                case_ids=[21],
            ),
        ],
    )


def _canned_factory(result: ScenarioAgentResult):
    """A factory whose agent returns the graph state create_agent would produce.

    create_agent's final state carries the parsed schema under
    ``structured_response``; the agent reads exactly that key.
    """

    def factory(*, model, tools, system_prompt):
        return RunnableLambda(
            lambda _inputs: {"messages": [], "structured_response": result}
        )

    return factory


def _channel() -> ScenarioChannel:
    async def send(_frame: dict) -> None:
        return None

    return ScenarioChannel(send)


def _request(**overrides) -> ScenarioAgentRequest:
    base = {
        "user_input": "Author scenarios for login rewards and the shop.",
        "game_context": {"constraints": ["Reward can be claimed once per day."]},
        "unity_context": {"states": [{"key": "login_reward.claimed"}]},
    }
    base.update(overrides)
    return ScenarioAgentRequest(**base)


_CTX = AgentContext(session_id="session-1")


def test_agent_context_builds_trace_config() -> None:
    context = AgentContext(
        session_id="session-1", metadata={"environment": "test", "session_id": "wrong"}
    )

    assert context.trace_config("scenario-generation") == {
        "run_name": "scenario-generation",
        "tags": ["agent"],
        "metadata": {"environment": "test", "session_id": "session-1"},
    }


def test_scenario_agent_names_and_tags_its_trace() -> None:
    """The run LangSmith shows carries the name, tag and session id."""
    agent = ScenarioAgent(agent_factory=_canned_factory(_result()))

    with collect_runs() as collected:
        asyncio.run(agent.run(_request(), _CTX, _channel()))

    (root,) = collected.traced_runs
    assert root.name == "scenario-generation"
    assert root.tags == ["agent"]
    assert root.extra["metadata"]["session_id"] == "session-1"


def test_scenario_agent_returns_multi_scenario_result() -> None:
    result = _result()
    agent = ScenarioAgent(agent_factory=_canned_factory(result))

    out = asyncio.run(agent.run(_request(), _CTX, _channel()))

    assert isinstance(out, ScenarioAgentResult)
    assert out.message == "Authored two scenarios."
    assert [plan.title for plan in out.scenarios] == [
        "Login reward flow",
        "Shop purchase flow",
    ]
    assert out.scenarios[0].case_ids == [11, 12]


def test_scenario_agent_raises_when_no_structured_response() -> None:
    """A loop that ended without a plan is a generation failure, not empty output."""

    def factory(*, model, tools, system_prompt):
        return RunnableLambda(lambda _inputs: {"messages": [], "structured_response": None})

    agent = ScenarioAgent(agent_factory=factory)

    with pytest.raises(ScenarioGenerationError):
        asyncio.run(agent.run(_request(), _CTX, _channel()))


def test_scenario_agent_binds_the_search_tool() -> None:
    """The turn is a tool loop: the case-search tool must reach the model."""
    seen: dict[str, list[str]] = {}

    def factory(*, model, tools, system_prompt):
        seen["tools"] = [tool.name for tool in tools]
        seen["system_prompt"] = system_prompt
        return RunnableLambda(
            lambda _inputs: {"messages": [], "structured_response": _result()}
        )

    agent = ScenarioAgent(agent_factory=factory)
    asyncio.run(agent.run(_request(), _CTX, _channel()))

    assert seen["tools"] == ["search_test_cases"]
    # The system prompt is the resolved v2 text, language directive substituted.
    assert "search_test_cases" in seen["system_prompt"]
    assert "{" not in seen["system_prompt"]


def test_empty_scenarios_is_a_valid_result() -> None:
    """No matching cases: the agent returns a message and an empty plan list."""
    result = ScenarioAgentResult(message="No matching cases yet.", scenarios=[])
    agent = ScenarioAgent(agent_factory=_canned_factory(result))

    out = asyncio.run(agent.run(_request(), _CTX, _channel()))

    assert out.scenarios == []
    assert out.message == "No matching cases yet."


def test_scenario_result_parses_string_case_ids_as_ints() -> None:
    """Search hits carry string ids; the plan stores them as ints (spec)."""
    parsed = ScenarioAgentResult.model_validate(
        {
            "message": "ok",
            "scenarios": [
                {"title": "t", "description": "d", "case_ids": ["7", "8"]}
            ],
        }
    )

    assert parsed.scenarios[0].case_ids == [7, 8]


def test_scenario_result_scenario_id_edit_vs_add() -> None:
    """scenario_id: an id (string coerced to int) means edit; absent means add."""
    parsed = ScenarioAgentResult.model_validate(
        {
            "message": "ok",
            "scenarios": [
                {"scenario_id": "5", "title": "edit", "description": "d", "case_ids": [1]},
                {"title": "add", "description": "d", "case_ids": [2]},
            ],
        }
    )

    assert parsed.scenarios[0].scenario_id == 5
    assert parsed.scenarios[1].scenario_id is None


def test_first_message_carries_current_scenarios() -> None:
    """The run's existing scenarios reach the prompt so the agent can edit by id."""
    message = build_first_message(
        _request(
            current_scenarios=[
                ScenarioPlan(
                    scenario_id=42,
                    title="Checkout flow",
                    description="Verify checkout.",
                    case_ids=[1, 2],
                )
            ]
        )
    )

    assert "Checkout flow" in message
    assert "42" in message
    # The placeholder was resolved (not left literal) in the rendered prompt.
    assert "{current_scenarios}" not in message


def test_first_message_empty_current_scenarios_renders_empty_list() -> None:
    """A fresh run renders the current-scenarios block as an empty JSON list."""
    message = build_first_message(_request())

    assert "[]" in message


def test_system_prompt_uses_requested_language_directive() -> None:
    ko_body, _ = build_system_prompt(_request(locale=OutputLanguage.ko))
    en_body, version = build_system_prompt(_request(locale=OutputLanguage.en))

    assert LANGUAGE_DIRECTIVES[OutputLanguage.ko] in ko_body
    assert LANGUAGE_DIRECTIVES[OutputLanguage.en] in en_body
    assert "한국어" in ko_body
    assert "English" in en_body
    # v2 is the newest scenario prompt version and the default.
    assert version == "v2"


def test_first_message_carries_the_run_goal_and_context() -> None:
    message = build_first_message(_request())

    assert "Author scenarios for login rewards and the shop." in message
    assert "login_reward.claimed" in message


def test_language_directives_cover_every_language() -> None:
    assert set(LANGUAGE_DIRECTIVES) == set(OutputLanguage)


def test_select_structured_method_by_model() -> None:
    assert select_structured_method(LLMModel.gpt_4o_mini) == "json_schema"
    assert select_structured_method(LLMModel.gemma_4_free) == "json_mode"
