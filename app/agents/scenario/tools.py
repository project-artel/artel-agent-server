"""The tools the scenario authoring agent drives its search with.

One tool: `search_test_cases`. It mirrors the QA agent's `search_knowledge`
wrapper (`app/agents/qa/tools.py`) — a per-turn budget, and the three search
outcomes formatted for the model to act on — over the scenario session's own
channel rather than the QA envelope.

Since ARTEL-319 a session that received the project's test case list gets NO tools:
the cases are already in the prompt, so a search could only return what the agent
is holding, and offering it would spend turns re-finding them. The tool is handed
over only when the test case list is empty — an orchestration that does not send one, or
a non-member session — which is also the rollback path if the test case list is turned
off upstream. Neither this wrapper nor the embedding search behind it is dead
code; it is the branch that carries those sessions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, tool

from app.agents.scenario.cases import (
    MAX_SEARCHES_PER_RUN,
    RESULT_LIMIT,
    SEARCH_TEST_CASES_DESCRIPTION,
    TestCaseSearchState,
    render_results,
)

if TYPE_CHECKING:
    # Type-only: see app/agents/scenario/cases.py for why app.sessions is not
    # imported at module load.
    from app.sessions.channel import ScenarioChannel


def build_tools(
    channel: ScenarioChannel,
    state: TestCaseSearchState,
    *,
    has_test_case_list: bool = False,
) -> list[BaseTool]:
    """The turn's tools. Empty when the agent already holds the test case list.

    Taking the tool away rather than telling the prompt not to use it is
    deliberate: a tool in reach gets called, and every call it makes here would
    spend a turn re-finding cases already in its context.
    """
    if has_test_case_list:
        return []

    # Imported here, not at module load, to keep the agent layer free of an
    # app.sessions import at import time (that would cycle through the service).
    from app.sessions.channel import TestCaseSearchFailed

    @tool(description=SEARCH_TEST_CASES_DESCRIPTION.format(limit=MAX_SEARCHES_PER_RUN))
    async def search_test_cases(query: str, category: str | None = None) -> str:
        # What the agent reads is SEARCH_TEST_CASES_DESCRIPTION, not this.
        if state.searches_attempted >= MAX_SEARCHES_PER_RUN:
            return (
                f"You have used all {MAX_SEARCHES_PER_RUN} case searches this turn. "
                "Build the scenarios from the cases you have already found."
            )

        state.searches_attempted += 1
        answer = await channel.search_test_cases(
            query, (category or "").strip() or None, RESULT_LIMIT
        )
        remaining = MAX_SEARCHES_PER_RUN - state.searches_attempted

        if answer is None:
            return (
                "The case search did not answer in time. Build the scenarios from "
                f"the cases you already have. {remaining} search(es) left."
            )
        if isinstance(answer, TestCaseSearchFailed):
            # Said plainly, with what to do instead: a failed lookup is a side
            # errand that failed, not a reason to invent cases.
            return (
                f"The case search could not run — {answer.reason}. Build the "
                "scenarios from the cases you already have, and note what is "
                f"missing. {remaining} search(es) left."
            )
        return render_results(answer, remaining)

    return [search_test_cases]
