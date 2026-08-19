"""The tools the scenario authoring agent drives its search with.

Two tools, handed over on different conditions. `search_test_cases` It mirrors the QA agent's `search_knowledge`
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

`list_uncovered_cases` is handed over always, and it is not a search. It answers
which cases no scenario has reached yet — a fact about the project's state, not
about the cases themselves, so holding the whole case list does not answer it.
It is fetched rather than pushed because the answer shrinks as authoring covers
cases: a value sent at session open is wrong by the second turn, and re-sending it
every turn either bloats the turn message or, in the system prompt, throws the
cached case list away. A tool call pays only when someone asks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, tool

from app.agents.scenario.cases import (
    EXPLAIN_CASE_DESCRIPTION,
    FIND_PATH_DESCRIPTION,
    LIST_UNCOVERED_DESCRIPTION,
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
    """The turn's tools.

    `search_test_cases` is withheld when the agent already holds the test case list.
    Taking it away rather than telling the prompt not to use it is deliberate: a tool
    in reach gets called, and every call it makes there would spend a turn re-finding
    cases already in its context.

    `list_uncovered_cases` is always present. It answers a question the list cannot —
    what has been covered so far — and that answer changes while the session runs.
    """
    # Imported here, not at module load, to keep the agent layer free of an
    # app.sessions import at import time (that would cycle through the service).
    from app.sessions.channel import TestCaseSearchFailed

    @tool(description=LIST_UNCOVERED_DESCRIPTION)
    async def list_uncovered_cases() -> str:
        # What the agent reads is LIST_UNCOVERED_DESCRIPTION, not this.
        answer = await channel.fetch_uncovered()
        if answer is None:
            return (
                "Coverage could not be read just now. Say so rather than guessing a "
                "number — a made-up count is worse than no count."
            )
        if not answer.ids:
            return "Every case in this project is already carried by some scenario."
        by_scene = ", ".join(f"{s.scene} {s.count}" for s in answer.scenes)
        return (
            f"{len(answer.ids)} cases are not carried by any scenario yet "
            f"({by_scene}). Ids: {', '.join(str(i) for i in answer.ids)}. "
            "Their wording is in the case list you already hold — quote it rather "
            "than describing the ids."
        )

    @tool(description=FIND_PATH_DESCRIPTION)
    async def find_path(from_case_id: int, to_case_id: int) -> str:
        # What the agent reads is FIND_PATH_DESCRIPTION, not this.
        answer = await channel.fetch_path(from_case_id, to_case_id)
        if answer is None:
            return (
                "The route lookup did not answer in time. Do not invent the steps in "
                "between — say in `message` that you could not check the route."
            )
        reversed_note = (
            "\nORDER — these two chain the other way round: the second case's declared state "
            "leads into the first's, not the reverse. Put them in that order and ask again, "
            "unless the request genuinely wants this direction."
            if answer.ordering == "REVERSED"
            else ""
        )
        if answer.result == "NOT_REQUIRED":
            return (
                "NOT_REQUIRED — nothing goes in between. The two cases follow directly."
                + reversed_note
            )
        if answer.result == "KNOWN":
            lines = "\n".join(
                f"  {i}. {action}"
                + (f"   [input: {answer.inputs[i - 1]}]" if i <= len(answer.inputs) else "")
                for i, action in enumerate(answer.actions, 1)
            )
            return (
                "KNOWN — write each line below as its own bridge step (case_id null), in order:\n"
                f"{lines}{reversed_note}"
            )
        blocked = answer.blocked_by or "unknown"
        return (
            f"UNKNOWN — the route is not in the scene spec. Blocking: {blocked}. "
            f"{answer.note} Do not invent steps. Say so in `message`, name what is blocking, "
            f"and ask the user how it is done.{reversed_note}"
        )

    @tool(description=EXPLAIN_CASE_DESCRIPTION)
    async def explain_case(case_id: int) -> str:
        # What the agent reads is EXPLAIN_CASE_DESCRIPTION, not this.
        facts = await channel.fetch_case_facts(case_id)
        if facts is None:
            return (
                "The case lookup did not answer in time. Write the step from the case's own "
                "wording rather than guessing an operation."
            )
        lines = [f"case {case_id} · scene {facts.scene or 'unknown'}"]
        if facts.state_before:
            lines.append(
                "  requires: "
                + ", ".join(f"{g.variable} {g.operator} {g.value}" for g in facts.state_before)
            )
        if facts.state_after:
            lines.append(
                "  leaves: " + ", ".join(f"{k}={v}" for k, v in facts.state_after.items())
            )
        if not facts.operations:
            lines.append(f"  operations: none known — {facts.note}")
            return "\n".join(lines)
        lines.append(f"  operations ({len(facts.operations)}):")
        for op in facts.operations:
            detail = f"    {op.input}"
            if op.label:
                detail += f"  [{op.label}]"
            detail += f"  capability:{op.capability_id}  via {op.matched_by}"
            if op.status != "runnable":
                detail += f"  ({op.status})"
            lines.append(detail)
            if op.given:
                lines.append(f"      needs {op.given}")
            if op.summary:
                lines.append(f"      {op.summary}")
        if facts.observable is False:
            lines.append("  observable: no — the result cannot be read back during a run.")
        return "\n".join(lines)

    if has_test_case_list:
        return [list_uncovered_cases, find_path, explain_case]

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

    return [list_uncovered_cases, search_test_cases, find_path, explain_case]
