import asyncio

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableLambda

from app.agents import (
    AgentContext,
    GameContext,
    GameContextAgent,
    GameContextAgentRequest,
    GameContextExtractionError,
)
from app.agents.game_context.schemas import Flow, Mechanic, Overview, Screen


def _result() -> GameContext:
    return GameContext(
        overview=Overview(
            title="WordVenture",
            genre="Turn-based card adventure",
            core_loop="Combine keyword cards to cast spells and defeat enemies.",
        ),
        screens=[
            Screen(
                name="Combination window",
                purpose="Assemble a spell from cards.",
                elements=["Spell slot", "Attribute slot", "Combine button"],
                transitions=["Opens from the battle scene via the combine button."],
            )
        ],
        mechanics=[
            Mechanic(
                name="Keyword combination",
                rules=["3 spells x 5 attributes = 15 spells"],
                preconditions=["Only the Fire attribute is available at the start."],
            )
        ],
        flows=[Flow(name="Tutorial", steps=["Draw a card", "Open the combine window"])],
    )


def _canned_factory(result: GameContext):
    return lambda model: RunnableLambda(lambda _inputs: result)


def _request(text: str = "Game design document body...") -> GameContextAgentRequest:
    return GameContextAgentRequest(document_text=text)


_CTX = AgentContext(session_id="ingest-1")


def test_game_context_agent_returns_structured_result() -> None:
    result = _result()
    agent = GameContextAgent(structured_factory=_canned_factory(result))

    out = asyncio.run(agent.run(_request(), _CTX))

    assert isinstance(out, GameContext)
    assert out.overview.title == "WordVenture"
    assert out.screens[0].elements == ["Spell slot", "Attribute slot", "Combine button"]
    assert out.mechanics[0].rules == ["3 spells x 5 attributes = 15 spells"]


def test_game_context_agent_retries_on_parse_error_then_succeeds() -> None:
    result = _result()
    calls = {"n": 0}

    def flaky(_inputs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OutputParserException("bad json")
        return result

    agent = GameContextAgent(structured_factory=lambda model: RunnableLambda(flaky))

    out = asyncio.run(agent.run(_request(), _CTX))

    assert out.overview.title == "WordVenture"
    assert calls["n"] == 2  # retried once


def test_game_context_agent_raises_after_exhausting_retries() -> None:
    def always_fail(_inputs):
        raise OutputParserException("still bad")

    agent = GameContextAgent(
        structured_factory=lambda model: RunnableLambda(always_fail)
    )

    with pytest.raises(GameContextExtractionError):
        asyncio.run(agent.run(_request(), _CTX))


def test_game_context_defaults_to_empty_sections() -> None:
    ctx = GameContext()

    assert ctx.overview is None
    assert ctx.screens == []
    assert ctx.mechanics == []
    assert ctx.entities == []
    assert ctx.progression == []
    assert ctx.flows == []
    assert ctx.glossary == []
    assert ctx.misc == []
