"""The `search_test_cases` tool: budget and the three search outcomes.

Mirrors `tests/test_qa_tools.py`'s coverage of `search_knowledge` — a mock
channel supplies each outcome and the test asserts what the model is told.
"""

import asyncio

from app.agents.scenario.cases import MAX_SEARCHES_PER_RUN, TestCaseSearchState
from app.agents.scenario.tools import build_tools
from app.sessions.channel import (
    TestCaseHit,
    TestCaseSearchFailed,
    TestCaseSearchResult,
)


class FakeChannel:
    """A channel stub that records searches and returns a scripted answer."""

    def __init__(self, answer) -> None:
        self._answer = answer
        self.calls: list[tuple[str, str | None, int]] = []

    async def search_test_cases(self, query, category, limit):
        self.calls.append((query, category, limit))
        return self._answer


def _tool(answer):
    state = TestCaseSearchState()
    channel = FakeChannel(answer)
    (search,) = build_tools(channel, state)
    return search, state, channel


def _call(search, **kwargs) -> str:
    return asyncio.run(search.ainvoke(kwargs))


def test_a_hit_is_rendered_with_ids_to_reference() -> None:
    answer = TestCaseSearchResult(
        results=[
            TestCaseHit(
                id="42",
                category="SHOP",
                title="Buy with gold",
                precondition="Has 100 gold",
                expected="Item is purchased",
                verification_status="VERIFIED",
                score=0.91,
            )
        ]
    )
    search, state, channel = _tool(answer)

    out = _call(search, query="buy an item")

    assert "id 42" in out
    assert "Buy with gold" in out
    assert "5 case search(es) left" in out  # 6 budget, 1 used
    assert state.searches_attempted == 1
    assert channel.calls == [("buy an item", None, 10)]


def test_an_empty_result_tells_the_agent_not_to_invent() -> None:
    search, _, _ = _tool(TestCaseSearchResult(results=[]))

    out = _call(search, query="nothing matches")

    assert "No existing case matches" in out
    assert "invent" in out


def test_a_failure_is_reported_without_failing_the_turn() -> None:
    search, _, _ = _tool(TestCaseSearchFailed(reason="scope not found"))

    out = _call(search, query="q")

    assert "could not run" in out
    assert "scope not found" in out


def test_a_timeout_is_reported_as_no_answer() -> None:
    search, _, _ = _tool(None)

    out = _call(search, query="q")

    assert "did not answer in time" in out


def test_the_search_budget_is_enforced() -> None:
    search, state, channel = _tool(TestCaseSearchResult(results=[]))
    state.searches_attempted = MAX_SEARCHES_PER_RUN

    out = _call(search, query="one too many")

    assert f"all {MAX_SEARCHES_PER_RUN} case searches" in out
    # Refused before it goes out: no round trip is spent on it.
    assert channel.calls == []


def test_a_blank_category_is_sent_as_none() -> None:
    search, _, channel = _tool(TestCaseSearchResult(results=[]))

    _call(search, query="q", category="  ")

    assert channel.calls == [("q", None, 10)]
