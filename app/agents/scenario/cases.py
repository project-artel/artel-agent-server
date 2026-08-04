"""Searching the run's existing TestCases, and formatting the answer.

The run-scoped authoring agent decomposes a run goal into scenarios, and each
scenario is built from EXISTING TestCases — which live only in the orchestration
server. So the agent cannot see the cases up front (they accumulate, and a first
import is already large: injecting them all would blow the context, ARTEL-206).
It searches for the ones it needs instead, the same shape the QA agent searches
the knowledge base, and references the hits by id.

These constants and the rendering live here rather than in
`app/agents/scenario/tools.py` for the reason `app/agents/qa/knowledge.py` keeps
the QA search's budget and vocabulary: they are this tool's own subject matter,
and the number, the wording, and the format that teaches the agent to ration and
read a search all move together.

Nothing here creates a TestCase. Case *creation* is a separate concern
(ARTEL-177); this agent only searches and maps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: importing app.sessions at module load would form a cycle
    # (app.sessions.__init__ pulls in the service, which imports app.agents).
    from app.sessions.channel import TestCaseSearchResult

# How many case searches one turn may make.
#
# A turn that keeps searching instead of deciding never returns an answer — the
# same failure the QA search budget guards. Enough to cover a handful of distinct
# scenarios in one decomposition, and no more.
MAX_SEARCHES_PER_RUN = 6

# How many hits one search brings back. Orchestration clamps this to its own
# ceiling, so it is this side stating the context it is willing to spend, not a
# guarantee. Higher than the knowledge search's: mapping cases into scenarios
# wants to see the candidates, where a knowledge lookup wants the top answer.
RESULT_LIMIT = 10

# Per hit. A case's precondition and expected are written for a human reading the
# case list and can run long; the agent needs enough to decide whether the case
# belongs in a scenario. Clipped rather than dropped, and the clip says so.
MAX_TEXT_CHARS = 300

# Written out rather than left as a docstring so the per-turn cap comes from the
# constant above. An agent told only that searching is "limited" spends the
# budget at the first opportunity.
SEARCH_TEST_CASES_DESCRIPTION = """Find existing TestCases to build a scenario from.

TestCases already exist in the project — imported or written earlier — and a
scenario you author is a SELECTION of them, referenced by id. This is how you see
them: they are not in your context, so search for the ones a scenario needs.

`query` is a description in your own words of the behaviour a scenario should
cover — "상점에서 골드로 아이템 구매", "튜토리얼 첫 진입 흐름" — not a keyword.
The cases are matched by meaning.

`category` optionally narrows the search to one kind of case. Leave it out unless
you are sure which category the cases you want are filed under — a wrong one hides
them rather than sharpening the search.

Each hit prints its `id`, category, title, verification status, a similarity
score, and its precondition/expected. Put the ids of the cases that belong in a
scenario into that scenario's `case_ids`.

You get {limit} searches for the whole turn, so spend them on distinct scenarios
rather than re-asking the same thing. An empty result is an answer: it means no
case matches, NOT that something went wrong. Do not invent a case or a scenario
to cover the gap — return the scenarios you could build, and say in `message`
what is still missing."""


class TestCaseSearchState:
    """What the case-search tool has done so far in one turn.

    Attempts, not successes: a search Orchestration refuses every time would
    otherwise be a loop with no bound on it, the same reasoning as the QA tools.
    """

    def __init__(self) -> None:
        self.searches_attempted = 0


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return f"{text[:MAX_TEXT_CHARS]}… [truncated]"


def render_hit(index: int, hit) -> str:
    """One hit, with the provenance the agent needs to weigh and reference it.

    The id leads because referencing it is the whole point — a scenario's
    `case_ids` are these ids. The verification status and score qualify the
    match: a weak or unverified case is still returned, and the agent has to be
    able to discount it rather than trust the top hit by position alone.
    """
    header = (
        f"{index}. [id {hit.id} · {hit.category or 'UNCATEGORISED'} · "
        f"{hit.verification_status or 'UNVERIFIED'} · similarity {hit.score:.2f}]"
    )
    lines = [header]
    if hit.title:
        lines.append(f"   {hit.title}")
    if hit.precondition:
        lines.append(f"   precondition: {_clip(hit.precondition)}")
    if hit.expected:
        lines.append(f"   expected: {_clip(hit.expected)}")
    return "\n".join(lines)


def render_results(payload: TestCaseSearchResult, remaining: int) -> str:
    """What the model reads after a search that ran.

    `remaining` rides along on every answer, empty or not: the budget is only
    useful while the agent can still act on it, and right after a search is the
    one moment it is certainly reading this output.
    """
    budget = f"{remaining} case search(es) left this turn."
    if not payload.results:
        return (
            "No existing case matches that. That is not an error — the project may "
            "not have such a case yet. Do not invent one; leave it out of your "
            f"scenarios and note what is missing.\n\n{budget}"
        )
    hits = "\n".join(
        render_hit(index, hit) for index, hit in enumerate(payload.results, start=1)
    )
    return (
        f"Existing cases matching your search:\n\n{hits}\n\n"
        "Reference the ones a scenario needs by putting their ids in that "
        f"scenario's `case_ids`.\n\n{budget}"
    )
