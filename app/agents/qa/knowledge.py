"""Reading from, and writing to, the project's knowledge base.

A QA agent starts a run knowing only the scenario text it was handed. Everything
else about the game — what a mechanic costs, what counts as success, which of two
plausible readings of "the purchase fails" is the designed one — lives in the
project's knowledge base, extracted from its design documents. Without a way to
ask, a step whose `expected` depends on a rule is judged on a guess. And without
a way to write, everything a run works out for itself dies with the run.

These things are kept here rather than in `app/agents/qa/tools.py`, for the same
reason `vision.py` keeps the capture budget and image handling: they are these
tools' own subject matter, and the numbers, the vocabulary and the wording that
teaches the agent to ration them all have to move together.

**A correction is one call.** `update_knowledge` changes an entry in place, so it
keeps its id and the knowledge base keeps a record that the entry was repaired
rather than thrown away (ARTEL-257). Until that tool existed, correcting meant a
delete followed by a record, and that route is still walkable — nothing stops a
run from taking it. What is left over from it here is a safety net rather than
the way through: `render_missing_knowledge_warning`, the replacement write being
exempt from the write cap, and the deletion budget being the smallest allowance
in the run. A run that deletes and then fails to record has removed knowledge
rather than fixed it, and that stays true now that there is a better way to do it.

Nothing in this module touches the game. Neither a search nor a write changes a
screen, so no scene view is produced and none is appended to any result — see
`app/agents/qa/context.py` for why re-loading a scene the agent has already read
is the thing to avoid.
"""

from app.qa.envelope import KnowledgeSearchHit, KnowledgeSearchResultPayload

# --- how much of the knowledge base one run may move -------------------------
#
# The numbers themselves moved to `app/agents/qa/arch.py`, where the run's other
# structural knobs are, because a run can now be asked to use different ones and
# the arch fingerprint has to see them. They are re-exported under their old
# names for the callers and tests that read the defaults.
#
# What did not move is the argument. A run that keeps looking things up instead
# of deciding reaches the deadline with nothing reported — the same failure
# `MAX_CAPTURES_PER_RUN` exists to prevent — and searching is capped below
# capturing because a game's rules do not change during a run: the second search
# on the same subject learns nothing the first one did not. Recording is capped
# below searching because a run that learns five durable rules about a game has
# had an unusually instructive hour; one that claims more is filing observations,
# not knowledge.
#
# `max_records_per_run` is the budget for BOTH writes that put content into the
# knowledge base — `record_knowledge` and `update_knowledge` draw on it together.
# They are capped as one because they fail as one: either spends the run's steps
# tidying the knowledge base instead of reaching a verdict. A second allowance
# would double that ceiling without anyone choosing to, and would widen
# `ResolvedArch.tool_call_limit` with it for a tool that mostly replaces calls
# rather than adding them.
#
# Deletion is the smallest allowance of the three on purpose. It is the least
# reversible thing the agent does and the least watched: a wrong verdict is read
# by whoever reads the report, while an entry wrongly deleted just quietly stops
# being there for every run after this one, and the soft delete only helps
# somebody who already suspects it happened. `FORGET_KNOWLEDGE_DESCRIPTION` is
# the other half of that defence — this is the half that holds when the wording
# does not — and `QaArchSpec` refuses a spec that allows deletions without
# allowing the replacement writes.
from app.agents.qa.arch import (  # noqa: E402 - re-export, kept below the prose
    MAX_FORGETS_PER_RUN,
    MAX_RECORDS_PER_RUN,
    MAX_SEARCHES_PER_RUN,
)

# How many hits one search brings back.
#
# Orchestration clamps this to its own ceiling, so the number here is not a
# guarantee — it is this side stating the context it is willing to spend. Search
# results are NOT folded the way scene views are (`fold_stale_scenes` only folds
# what carries a scene marker), so every hit stays in the transcript until the
# run ends.
RESULT_LIMIT = 5

# Per hit. A knowledge entry's description is written for a human reading the
# knowledge base, and can run long; what the agent needs is enough to settle one
# question. Clipped rather than dropped, and the clip says so, so the agent can
# tell "that is all there is" from "there is more".
MAX_DESCRIPTION_CHARS = 500

# The topics knowledge is filed under, as Orchestration defines them. Checked
# here so a bad filter costs nothing: Orchestration rejects the whole search on
# an unknown token — deliberately, since a silently ignored filter shows up only
# as results that are quietly too broad — and that rejection would otherwise cost
# a round trip and a slot out of the run's budget.
KNOWLEDGE_TAGS = ("CONTROL", "RULE", "OBJECTIVE", "UI", "MISC")

# Written out rather than left as a docstring so the per-run cap and the tag list
# come from the constants above. An agent told only that searching is "limited"
# spends the budget at the first opportunity, and a tag list that drifts from the
# one Orchestration accepts turns every filtered search into a refusal.
SEARCH_KNOWLEDGE_DESCRIPTION = """Ask what this game's design documents say about a rule you cannot see.

Use this when the step's `expected` depends on something the screen does not
show: what a mechanic is supposed to cost, which of two readings of the step is
the designed one, what is meant to happen when a resource runs out, what counts
as having finished. These are the questions where the scene tells you WHAT
happened and you still cannot say whether it was RIGHT.

Do NOT use it to find out what is on screen. `observe_scene` is what reads the
screen, and it is current; this returns documentation, which may describe a
screen the build no longer has. In particular, do not reach for this to look up
a button's id, to check whether an element exists, or to confirm something you
have just observed.

Do NOT use it once per step. Most steps are decided by looking. A run gets {limit}
searches in total, and a run that spends them narrating instead of judging
reaches its deadline with no verdict to report.

`query` is a question in your own words — "골드가 모자랄 때 구매를 누르면
어떻게 되나" — not a keyword. The knowledge base is searched by meaning, and it
was indexed as the questions each entry answers, so a question matches it best.

`tag` optionally narrows the search to one topic — one of {tags} —
and a wrong one hides the answer rather than sharpening it, so leave it out
whenever you are unsure which the answer would be filed under.

An empty result is an answer. It means the documents do not cover this, not that
something went wrong — judge the step on what you can see and carry on."""

# The three write descriptions carry the whole usage policy for these tools, per
# ARTEL-192: the tool description is the single source, and the system prompt is
# left alone. What each one has to teach is not "how to call it" — the arguments
# are obvious — but where the line is. For recording, the line between a rule and
# this run's own state; for correcting and deleting, the line between an entry
# that should be repaired and one that should simply be gone, and behind both the
# line between stale knowledge and a bug. An agent that gets any of them wrong
# degrades the knowledge base for every run after.
RECORD_KNOWLEDGE_DESCRIPTION = """Write down something you learned about this game, so later runs start knowing it.

Use this when the run taught you a RULE the scenario did not state: what a
mechanic costs, what a control actually does, what happens when a resource runs
out, which of two readings of a screen is the designed one. The test for whether
something belongs here is one question — would it still be true in tomorrow's
run, on a fresh save?

That question is what rules out this run's own state. "The player has 500 gold",
"the shop is open", "the boss is at half health" are facts about this moment, not
about the game, and filing them poisons the answers later runs get. "Buying is
blocked while gold is below the price" is knowledge; "buying is blocked right
now" is not.

Do NOT record what the scenario already told you. Do NOT record a bug: a build
behaving wrongly is a finding for `report_step`, not a rule to teach the next
run — record it here and you have taught every later run that the broken
behaviour is correct.

`tag` is the topic it is filed under, one of {tags}. `summary` is one sentence,
the fact itself, phrased as you would answer someone who asked. `description` is
what stands behind it: the condition, the exception, what you saw that
established it.

Use `update_knowledge` instead when an entry already covers this and is merely
wrong: recording a second version of a rule leaves both in the knowledge base,
and a later run gets them both back and cannot tell which one to believe.

A run gets {limit} knowledge writes in total, shared with `update_knowledge`, so
spend them on what was worth learning.

Nothing answers a knowledge write, so send each fact once — a repeat files it
twice, and no one will tell you."""

UPDATE_KNOWLEDGE_DESCRIPTION = """Correct a knowledge entry that is wrong or out of date.

This is how knowledge gets FIXED. The entry keeps its id and its history, so the
project can tell a rule that was repaired from one that was thrown away. That is
the whole difference between this and `forget_knowledge`: correct what should be
right, delete only what should be gone with nothing put in its place.

`knowledge_id` must be the id of an entry `search_knowledge` returned to you in
this run — it is printed with each hit. You cannot correct what you have not read.

Send only what changes: `tag` (one of {tags}), `summary`, `description`. Whatever
you leave out stays exactly as it is, so fixing one sentence does not mean
retyping the entry. At least one of the three is required.

The bar is the one `forget_knowledge` sets, and for the same reason. ONE
disagreement between an entry and what you saw is more often a BUG than stale
knowledge, and a bug belongs in `report_step` — rewriting the rule to match a
broken build teaches every later run that the break is correct.

A run gets {limit} knowledge writes in total, shared with `record_knowledge`.
Nothing answers a knowledge write, so send each correction once."""

FORGET_KNOWLEDGE_DESCRIPTION = """Delete a knowledge entry that is no longer true.

This is the most destructive thing you can do in a run and the least watched. A
wrong verdict gets read by whoever reads the report; a rule you delete by mistake
just stops being there, for every run after this one, with nobody prompted to
look. So the bar is high, and you get only {limit} of these.

Delete only when the game plainly contradicts the entry AND the game is the one
that is right. ONE contradiction is not enough. A single disagreement between a
documented rule and what you saw is more often a BUG than stale documentation —
and a bug is something to report with `report_step`, not something to erase the
rule over. If you cannot tell which of the two you are looking at, report it and
leave the entry alone. Leaving a stale entry costs a later run one confusing
search result; deleting a correct one costs it the answer entirely.

`knowledge_id` must be the id of an entry `search_knowledge` returned to you in
this run — it is printed with each hit. You cannot delete something you have not
read.

Do NOT delete in order to correct. `update_knowledge` repairs an entry in one
call and leaves the project able to tell a repair from a discard; deleting and
re-recording loses that and can leave the knowledge simply gone. If you do take
that route anyway, call `record_knowledge` IMMEDIATELY afterwards, in the same
step, before anything else — a run that stops between the two has removed the
knowledge rather than fixed it, and nothing here can undo it for you.

`thought` is why this entry is wrong. It is the only record of the reasoning
behind the deletion, so write what someone would need who later asks whether this
should have been deleted at all."""


def render_entry_label(knowledge_id: str, summary: str) -> str:
    """How one knowledge entry is named back to the agent.

    The summary rides along wherever there is one, because an id alone tells the
    agent nothing about what it just removed — and the place this matters most is
    the warning below, where the whole point is naming what went missing.
    """
    return f'{knowledge_id} — "{summary}"' if summary else knowledge_id


def render_missing_knowledge_warning(deleted: list[str]) -> str:
    """The sentence that must appear whenever a write fails after a delete did not.

    This is the one path here that loses knowledge. `update_knowledge` is what a
    correction should be, but nothing forces a run to use it: an agent that
    deletes and then records is still doing something the tools allow, and the gap
    between those two calls is real — the delete is already applied on the far side
    and nothing here can take it back. Every way `record_knowledge` can fail routes
    through this, so no failure of it can be phrased as though nothing were at
    stake.

    Empty when the run has deleted nothing outstanding, which is the ordinary case
    — a first record that fails has lost nothing yet.
    """
    if not deleted:
        return ""
    entries = "\n".join(f"  - {item}" for item in deleted)
    return (
        "\n\nNOTHING WAS RECORDED, and you have already deleted:\n"
        f"{entries}\n"
        "That knowledge is missing from the project right now. Fix the problem "
        "above and call `record_knowledge` again immediately — nothing else will, "
        "and the deletion cannot be undone from here."
    )


def render_description(description: str) -> str:
    if len(description) <= MAX_DESCRIPTION_CHARS:
        return description
    return f"{description[:MAX_DESCRIPTION_CHARS]}… [truncated]"


def render_hit(index: int, hit: KnowledgeSearchHit) -> str:
    """One hit, with the provenance the agent needs to weigh it.

    The tag and source are printed because they qualify the claim: a `RULE` from
    `DOCS` is what the design says, while something from `QA` is what a previous
    run observed. The similarity is printed for the same reason — a weak match is
    still returned, and the agent has to be able to discount it rather than treat
    the top hit as authoritative by position alone.

    The id is printed because a search is the only way to reach one. `forget_knowledge`
    takes an id and refuses one this run has not been shown, so an entry the agent
    never read is an entry it cannot delete; unprinted, the id would make that rule
    unsatisfiable rather than safe.
    """
    header = (
        f"{index}. [id {hit.id or 'unknown'} · {hit.tag or 'UNTAGGED'} · "
        f"from {hit.source or 'unknown'} · similarity {hit.score:.2f}]"
    )
    body = render_description(hit.description)
    lines = [header, f"   {hit.summary}"] if hit.summary else [header]
    if body:
        lines.append(f"   {body}")
    return "\n".join(lines)


def render_results(payload: KnowledgeSearchResultPayload, remaining: int) -> str:
    """What the model reads after a search that ran.

    `remaining` rides along on every answer, empty or not. The budget is only
    useful to the agent while it can still act on it, and the one moment it is
    certainly reading this tool's output is right after it used one.
    """
    budget = f"{remaining} knowledge search(es) left in this run."
    if not payload.results:
        return (
            "The knowledge base has nothing on that. That is not an error — the "
            "documents may not cover it, or it may not be indexed yet. Judge this "
            f"step on what you can see.\n\n{budget}"
        )
    hits = "\n".join(
        render_hit(index, hit) for index, hit in enumerate(payload.results, start=1)
    )
    return (
        f"What the design documents say about {payload.query!r}:\n\n{hits}\n\n"
        "This is documentation, not the running build. Where it and the screen "
        f"disagree, the screen is what the step actually did.\n\n{budget}"
    )
