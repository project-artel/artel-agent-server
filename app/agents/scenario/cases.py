"""The run's existing TestCases: the test case list the agent holds, and the search behind it.

The run-scoped authoring agent decomposes a run goal into scenarios, and each
scenario is built from EXISTING TestCases — which live only in the orchestration
server.

**How the agent sees them changed in ARTEL-319.** It used to see them only
through `search_test_cases`, because injecting them all was assumed to blow the
context (ARTEL-206). Measured, that assumption does not hold: a Korean case in
the stored schema is ~74 tokens with its bodies, so a thousand of them is ~74k
and sits in the cached prefix of the system prompt. So orchestration now sends
the whole list when the session opens and the agent reads it from context.

That matters beyond convenience. A vector search returns a ranking and never the
size of what it ranked, so the old path could not tell the agent — or us — that
anything had been missed; the per-turn budget below surfaced 30-40 cases out of a
project that may hold a thousand. Coverage was bounded by recall. Reading the
whole list removes that bound outright.

**The search stays.** It is the path when the test case list is empty — an orchestration
that does not send one yet, or a non-member session — and that fallback is also
the rollback, so nothing here is dead code even while a session with the list never
calls it. `build_tools` decides, per turn, which of the two the agent gets.

These constants and the rendering live here rather than in
`app/agents/scenario/tools.py` for the reason `app/agents/qa/knowledge.py` keeps
the QA search's budget and vocabulary: they are this tool's own subject matter,
and the number, the wording, and the format that teaches the agent to ration and
read a search all move together.

Nothing here creates a TestCase. Case *creation* is a separate concern
(ARTEL-177); this agent only reads and maps.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from app.agents.scenario.schemas import TestCaseListItem

if TYPE_CHECKING:
    # Type-only: importing app.sessions at module load would form a cycle
    # (app.sessions.__init__ pulls in the service, which imports app.agents).
    from app.sessions.channel import TestCaseSearchResult

# How many case searches one turn may make.
#
# A ceiling no real turn reaches, matching the QA run's allowances. The old
# number rationed a turn to six searches on the argument that a turn which keeps
# searching never decides; what it produced was decompositions that stopped
# short of the cases they needed. What a search is for is taught by
# `SEARCH_TEST_CASES_DESCRIPTION` instead.
MAX_SEARCHES_PER_RUN = 1_000_000

# How many hits one search brings back. Orchestration clamps this to its own
# ceiling, so it is this side stating the context it is willing to spend, not a
# guarantee. Higher than the knowledge search's: mapping cases into scenarios
# wants to see the candidates, where a knowledge lookup wants the top answer.
RESULT_LIMIT = 100

# Per hit. A case's precondition and expected are written for a human reading the
# case list and can run long; the agent needs enough to decide whether the case
# belongs in a scenario. Clipped rather than dropped, and the clip says so.
MAX_TEXT_CHARS = 4_000

# Written out rather than left as a docstring so the per-turn cap comes from the
# constant above. An agent told only that searching is "limited" spends the
# budget at the first opportunity.
SEARCH_TEST_CASES_DESCRIPTION = """Find existing TestCases a scenario should verify.

TestCases already exist in the project — imported or written earlier — and a
scenario you author is an ordered flow of steps whose verification points map to
these cases. This is how you see them: they are not in your context, so search
for the ones a scenario needs.

`query` is a description in your own words of the behaviour a scenario should
cover — "상점에서 골드로 아이템 구매", "튜토리얼 첫 진입 흐름" — not a keyword.
The cases are matched by meaning.

`category` optionally narrows the search to one kind of case. Leave it out unless
you are sure which category the cases you want are filed under — a wrong one hides
them rather than sharpening the search.

Each hit prints its `id`, category, title, verification status, a similarity
score, and its precondition/expected. Write the step(s) that exercise a case and
set that step's `case_id` to the case's `id` — the step observing its expected
result is the case's verification step.

You get {limit} searches for the whole turn, so spend them on distinct scenarios
rather than re-asking the same thing. An empty result is an answer: it means no
case matches, NOT that something went wrong. Do not invent a case — you may still
write the action steps and leave their `case_id` null, and say in `message` what
is still missing."""


# What the agent reads in place of the test case list when none arrived. Written as
# prose the model can act on rather than left blank: an empty section reads as
# "this project has no cases", which is the opposite of what an absent list
# means, and the agent would stop instead of searching.
NO_TEST_CASE_LIST_NOTICE = """The project's case list was not provided this session, so the cases are NOT in
your context. Use `search_test_cases` to find the ones each scenario needs. An
empty search result means no case matches — not that something went wrong."""


def render_game_shape(entries: list[TestCaseListItem], entry_scene: str | None = None) -> str:
    """What the game looks like, in one block, before any case (ARTEL-670).

    **The map was never given as a shape.** Every prompt version so far carried it
    only as per-case fragments — which screen this case is on, where that value
    moves — so the agent had 43 cards and no board. Measured on a real project, the
    words that would name the board (`entry`, a screen list, the graph) appeared
    zero times in 121,712 characters, while one screen name appeared 103 times
    scattered inside case bodies.

    Ordering is the judgement the agent is weakest at, and this is what a person
    reads first when they order by hand: which screens exist, which one the game
    boots into, what leads where, and which values are progress rather than
    position. None of it is new data — it is the same map, folded.

    Everything here is derived from the cases already on the wire, except
    ``entry_scene``, which only orchestration can know: the screen graph is cyclic,
    so no amount of structure says where the game starts. Absent, the block says so
    rather than guessing — a wrong entry is worse than none.
    """
    if not entries:
        return "(no cases, so nothing is known about this game's screens)"

    scenes = sorted({entry.scene for entry in entries if entry.scene})
    counts = Counter(entry.scene for entry in entries if entry.scene)

    lines = [f"{len(scenes)} screens carry cases."]
    lines.append(
        f"The game boots into {entry_scene}."
        if entry_scene
        else "Which screen the game boots into was not sent."
    )

    # One step out of each screen, gathered from the cases standing on it. Same
    # facts the cases carry; here they are a graph instead of 43 asides.
    leads: dict[str, set[str]] = {}
    for entry in entries:
        for exit_ in entry.exits:
            if exit_.scene and exit_.scene != entry.scene:
                leads.setdefault(entry.scene, set()).add(exit_.scene)
    if leads:
        lines.append("")
        lines.append("Where each screen leads in one step:")
        for scene in sorted(leads):
            lines.append(f"  {scene} → {', '.join(sorted(leads[scene]))}")

    lines.append("")
    lines.append(
        "Cases per screen: "
        + " · ".join(f"{scene} {counts[scene]}" for scene, _ in counts.most_common())
    )

    # **Each fact once.** Keyed by (screen, by, how, when) so the same write reached
    # through five different cases prints one line. What separates progress from
    # position lives here: a value only something else can raise is a value the
    # scenario has to earn in order, and one an arrow key moves is not.
    moves: dict[str, list[str]] = {}
    for entry in entries:
        for guard in entry.state_before:
            seen = moves.setdefault(guard.variable, [])
            for move in guard.moves:
                how = move.how or "NOT INSTRUCTABLE — it happens on its own"
                line = f"    {move.by or '?'} in {move.scene} ({how})"
                if move.when:
                    line += f" when {move.when}"
                if line not in seen:
                    seen.append(line)
    moving = {name: found for name, found in moves.items() if found}
    if moving:
        lines.append("")
        lines.append("How each value moves — every way the map knows, listed once:")
        for name in sorted(moving):
            lines.append(f"  {name}")
            lines.extend(moving[name])

    return "\n".join(lines)


def render_test_case_list(entries: list[TestCaseListItem]) -> str:
    """Every case in the project, as the block the system prompt carries.

    Order is preserved exactly as it arrived. Orchestration sorts by id and this
    block sits in the cached prefix of the prompt, so re-sorting here — even into
    something more readable, like grouping by category — would change the prefix
    whenever the sort key did and throw the cache away for a cosmetic win.

    Bodies are printed in full, unlike a search hit (`render_hit` clips at
    MAX_TEXT_CHARS). A search returns candidates to skim; this is the material the
    agent writes steps from, and a clipped precondition is exactly the kind of
    detail whose absence produces a plausible wrong step.
    """
    if not entries:
        return NO_TEST_CASE_LIST_NOTICE

    lines = [
        f"All {len(entries)} test cases in this project. "
        "These are ALL of them — there is nothing else to find."
    ]
    for entry in entries:
        lines.append(
            f"\n[id {entry.id} · {entry.scene} · {entry.verification_status}] {entry.step}"
        )
        if entry.precondition:
            lines.append(f"    precondition: {entry.precondition}")
        lines.append(f"    expected: {entry.expected_value}")
        # The state this case needs and leaves, parsed from its structure rather
        # than from the sentence above. Ordering by these is why they are here.
        for guard in entry.state_before:
            needs = f"    needs: {guard.variable} {guard.operator} {guard.value}"
            # Where that value moves — the screen names only. **How it moves is in the
            # game's shape block, written once** (ARTEL-670). It used to be repeated
            # under every requirement: measured on a real project, 676 such lines
            # carried 57 distinct facts, one fact restated 11.9 times, and they were
            # 79% of this whole block. The map was in the prompt all along, shredded.
            if guard.raised_in:
                needs += f"  ← moves in {', '.join(guard.raised_in)}"
            lines.append(needs)
        if entry.state_after:
            leaves = ", ".join(f"{k} {v}" for k, v in entry.state_after.items())
            lines.append(f"    leaves: {leaves}")
        # What to press for this case itself, ready to put in the step's `input`.
        # Blank is an answer — an observation has nothing to press — so the line is
        # written only when there is something.
        if entry.input:
            lines.append(f"    input: {entry.input}")
        # One step out of this screen. Blank `by` is an answer: the game goes there
        # by itself and there is nothing to press.
        for exit_ in entry.exits:
            lines.append(f"    to {exit_.scene}: {exit_.by or '(goes on its own)'}")
    return "\n".join(lines)


LIST_UNCOVERED_DESCRIPTION = """Which test cases no scenario has covered yet.

Coverage is a fact about the project's scenarios, not about the cases — the case
list you hold says what exists, never what has already been reached. It also
changes while you work: cases you cover in this turn stop being uncovered.

Call this when the user asks what is left or what to test next, and when you want
to lead an open-ended request toward a real gap. Do not guess at a count; this is
where the number comes from.

The answer gives ids. Their wording is in the case list you already hold — quote
that rather than reciting numbers, which mean nothing to the person reading.

**Answer with a journey, not a tally by screen.** "지도를 끝까지 돌아 보스까지 가면
아홉 건이 덮입니다" tells someone what to go and play; "Map_scene 9건, StoryScene 4건"
is a category count they still have to turn into a plan themselves. Use `to <screen>:`
and each case's `needs:`/`leaves:` to see which uncovered cases sit on one run, and
name that run. A screen with a large count is where to start looking, not the answer."""


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

    The id leads because referencing it is the whole point — a verification
    step's `case_id` is one of these ids. The verification status and score
    qualify the match: a weak or unverified case is still returned, and the agent
    has to be able to discount it rather than trust the top hit by position alone.
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
        "For each case a scenario verifies, write the step(s) that exercise it and "
        f"set that step's `case_id` to the case's id.\n\n{budget}"
    )


FIND_PATH_DESCRIPTION = """What has to happen between two test cases — for pairs the flows do not cover.

**The flows already answer this for the order they give.** Every hop inside a flow is written
out there: what goes in between, or that nothing does, or what blocks it. Read that line rather
than asking again for the same pair — the answers were all computed before this turn began, and
asking re-derives what you are already holding.

Call this only when you leave that order: two cases from different flows, a pair the flows never
put next to each other, or an order the user asked for that the flows do not have. Do NOT guess
what goes in between; that is what this answers.

It replies one of three ways.

  KNOWN         The route, as a list of actions. Write each one as a bridge step (case_id null),
                in the order given, between the two cases.
  NOT_REQUIRED  Nothing goes in between. The two cases follow directly.
  UNKNOWN       **The route is not known.** Do not invent one. Say in `message` that you do not
                know how to reach that state and name what is blocking, so the user can tell you
                or it can be found by playing. Leaving a case out and saying why is a correct
                answer; a step that will fail when run is not.

`blocked_by` names what stands in the way — a scene pair like `Map_scene→GameClearScene`, or a
variable like `StagePosition`. Quote it when you say you do not know: the user can answer a
question about a named thing, not about a vague gap.

Arguments are the two case ids, in the order you want them to run."""


