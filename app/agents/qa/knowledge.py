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

from app.qa.envelope import (
    KnowledgeAnchor,
    KnowledgeSearchHit,
    KnowledgeSearchResultPayload,
)

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
    MAX_EXPANDS_PER_RUN,
    MAX_FORGETS_PER_RUN,
    MAX_LINKS_PER_RUN,
    MAX_RECORDS_PER_RUN,
    MAX_SEARCHES_PER_RUN,
    MAX_UNLINKS_PER_RUN,
)

# How many hits one search brings back.
#
# Orchestration clamps this to its own ceiling, so the number here is not a
# guarantee — it is this side stating the context it is willing to spend. Search
# results are NOT folded the way scene views are (`fold_stale_scenes` only folds
# what carries a scene marker), so every hit stays in the transcript until the
# run ends.
RESULT_LIMIT = 100

# Per hit. A knowledge entry's description is written for a human reading the
# knowledge base, and can run long; what the agent needs is enough to settle one
# question. Clipped rather than dropped, and the clip says so, so the agent can
# tell "that is all there is" from "there is more".
MAX_DESCRIPTION_CHARS = 5_000

# The topics knowledge is filed under, as Orchestration defines them. Checked
# here so a bad filter costs nothing: Orchestration rejects the whole search on
# an unknown token — deliberately, since a silently ignored filter shows up only
# as results that are quietly too broad — and that rejection would otherwise cost
# a round trip and a slot out of the run's budget.
KNOWLEDGE_TAGS = ("CONTROL", "RULE", "OBJECTIVE", "UI", "MISC")

# The relations one entry can carry to another, as Orchestration defines them.
# Checked here so a bad one costs nothing but the check. Orchestration answers a
# refusal since ARTEL-332, so this is no longer the only thing keeping a bad frame
# from being reported as a success — it is now a round trip the run does not spend.
#
# There is deliberately no catch-all. An agent with one easy option and three hard
# ones picks the easy one, and the graph degrades to untyped; the tool description
# says instead that if none of the four fits, do not link.
#
# `LEADS_TO` left this tuple with ARTEL-590. The screen map — which screens exist
# and how you get between them — is owned by Orchestration's `content_map` schema,
# filled from play, and a second copy built by the agent only gave later runs two
# maps that disagreed. Reading is a separate matter: `_REVERSED` still knows the
# relation, because the edges written before this are still in the graph.
KNOWLEDGE_RELATIONS = ("CONTRADICTS", "REFINES", "DEPENDS_ON", "REPLACES")

# The label a vector neighbour is printed under. Never sent: Orchestration's CHECK
# constraint has no such relation, because a stored similarity turns silently false
# the moment the embedding model changes.
SIMILAR_LABEL = "SIMILAR"

# Per neighbour line. A neighbour is an orientation, not the entry itself — enough
# to decide whether to spend a search reading it in full. Long enough for a real
# sentence, short enough that eight of them stay a handful of lines.
MAX_NEIGHBOUR_SUMMARY_CHARS = 120

# The deepest walk this side will ask for. Orchestration clamps to its own ceiling
# anyway; this keeps the number the tool description promises and the number the
# agent actually gets from drifting apart.
MAX_EXPAND_DEPTH = 2

# The markers `render_hit` wraps a hit's neighbour lines in, so the folding
# middleware can find and replace exactly that span (ARTEL-277).
#
# The start marker carries the HIT's id rather than a running serial, unlike the
# scene view's observation number. A folded scene tells the agent to call
# `observe_scene`, which takes no argument; a folded neighbour block has to tell
# it to call `expand_knowledge` on something, and that something is this id.
NEIGHBOUR_BLOCK_START_PREFIX = "<<neighbours of "
NEIGHBOUR_BLOCK_START_SUFFIX = ">>"
NEIGHBOUR_BLOCK_END = "<</neighbours>>"

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

Do NOT record which screens the game has, or how to get from one to another. That
map is built from play and kept elsewhere; a copy written here leaves a later run
holding two maps that disagree, with nothing to say which one moved.

What does belong here is the fact that only holds in one place: a control that
behaves on this screen unlike anywhere else, a screen whose usual way back does
nothing, a purchase this shop refuses in a way no other does. Name where it holds
in `scene_name`, spelled the way the game spells that scene, because an exception
nobody can locate is one a later run cannot use, and one that reads as a rule about
the game teaches every other screen something false. Add `screen_id` only when a
screen's id has been shown to you, copied exactly as it was printed — `scene_name`
on its own is a complete answer, and a `screen_id` without it is refused. Anything true wherever the player is — how
the game reads input, what a resource is for, what the objective is — leaves both
out: a fact tied to one screen is a fact the run standing on the next one never
finds.

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

Send each fact once. The result tells you whether it was stored and gives the
entry's id, but a repeat is not caught — it files the same fact twice and both
say they worked."""

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
Send each correction once; a repeat spends another write and changes nothing."""

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


LINK_KNOWLEDGE_DESCRIPTION = """Record that two knowledge entries are related, and how.

An entry that stands alone is worth less than the same entry placed among its
neighbours: a later run gets it back with the exception, the precondition or the
contradiction already attached, instead of having to search three more times for
them.

`relation` is one of {relations}, and each means something a reader ACTS on:

- `CONTRADICTS` — the two cannot both be true. The most valuable link there is,
  and the one most often left unrecorded, because the moment you notice it is
  usually the moment you are busy deciding which of them to believe.
- `REFINES` — `from` is a narrower case, exception or condition of `to`. Point it
  FROM the specific TO the general.
- `DEPENDS_ON` — `from` only holds while `to` holds. A precondition.
- `REPLACES` — `from` supersedes `to`, which you have deleted or are about to.

If none of the four fits, do NOT link. Two entries being about vaguely the same
subject is not a relation — searching already finds those, and a link that says
nothing crowds out the ones that say something.

`note` is required and it is the only record of why you thought the connection was
real, so write what someone would need who later asks whether it should be there —
what you saw, and any condition the connection holds under.

Both ids must be ones this run has been shown, either as a search hit or as a
neighbour line under one.

A run gets {limit} links. Send each one once — a repeat comes back refused."""

UNLINK_KNOWLEDGE_DESCRIPTION = """Remove a relation between two knowledge entries.

The bar is lower than deleting an entry — both entries survive, and what is lost
is one connection and the sentence behind it. But it is just as quiet: a connection
you remove simply stops being there, for every run after this one, with nobody
prompted to look.

The mistake to avoid is removing a link because the BUILD is broken. A connection
that does not hold today is far more often a bug than a claim that was never true,
and that belongs in `report_issue` — unlink it and you have deleted what an earlier
run worked out instead of reporting the breakage. Read the note first: it says what
the connection was asserted on, and a condition that is not met right now is not
the same as a connection that was wrong.

Remove a link when the connection itself was wrong: the two entries do not
actually contradict, the precondition was misread, the narrower case refines
something else.

Name it the way you saw it — `from_knowledge_id`, `to_knowledge_id` and the same
`relation`. Your `thought` is the only record of why it went away, so write it there.

A run gets {limit} of these."""

EXPAND_KNOWLEDGE_DESCRIPTION = """Follow a knowledge entry's relations further than the search already showed you.

Every search hit already arrives with its closest neighbours listed under it, so
reach for this only when you need MORE than that: what lies two hops out, or what
else in the knowledge base is simply about the same thing.

`depth` 1 is the neighbours you have; 2 goes one further. Anything larger is
clamped rather than refused.

The answer mixes two kinds of thing and they are NOT worth the same. A neighbour
marked with a relation from {relations} was asserted by a run that wrote down why —
its `note` is that reason. One marked `{similar}` is a machine guess from text
similarity, with nobody standing behind it and no note at all. Treat the first as
a claim and the second as a hint about where to look next.

`knowledge_id` must be one this run has been shown. A run gets {limit} expansions."""


def render_neighbour(neighbour) -> str:
    """One neighbour, folded to a single line.

    The note does NOT ride along here. It is the auditor's field and it can be as
    long as the reasoning that produced it; inlined under every hit it would
    roughly double what an expanded search costs the transcript, for something the
    agent can get in full from `expand_knowledge`. The glyph carries the one
    distinction that must survive the fold: `↳` was asserted by somebody, `~` was
    computed.
    """
    glyph = "~" if neighbour.origin == "VECTOR" else "↳"
    label = (neighbour.relation or "related").lower()
    if neighbour.direction == "IN" and neighbour.relation in _REVERSED:
        label = _REVERSED[neighbour.relation]
    if neighbour.score is not None:
        label = f"{label} {neighbour.score:.2f}"
    summary = neighbour.summary or ""
    if len(summary) > MAX_NEIGHBOUR_SUMMARY_CHARS:
        summary = f"{summary[:MAX_NEIGHBOUR_SUMMARY_CHARS]}…"
    return f"   {glyph} [id {neighbour.id or 'unknown'} · {label}] {summary}".rstrip()


# How a relation reads when the entry you are looking at is on the receiving end.
# `CONTRADICTS` is absent on purpose: it is symmetric, and a direction word there
# would invent a claim the graph never made.
#
# `LEADS_TO` is here and NOT in `KNOWLEDGE_RELATIONS`, which is deliberate. The
# agent can no longer write one (ARTEL-590 handed the screen map to Orchestration's
# `content_map`), but the edges earlier runs wrote are still stored and still come
# back on a search hit or an expansion. Dropping the label would render them as the
# raw relation with no direction, so an entry reached BY a route would read as one
# leading to it — the map inverted, in the results of a change that was meant to
# stop maintaining a map at all.
_REVERSED = {
    "LEADS_TO": "reached from",
    "REFINES": "refined by",
    "DEPENDS_ON": "required by",
    "REPLACES": "replaced by",
}


def render_entry_label(knowledge_id: str, summary: str) -> str:
    """How one knowledge entry is named back to the agent.

    The summary rides along wherever there is one, because an id alone tells the
    agent nothing about what it just removed — and the place this matters most is
    the warning below, where the whole point is naming what went missing.
    """
    return f'{knowledge_id} — "{summary}"' if summary else knowledge_id


UNCONFIRMED_WRITE = (
    "The frame went out but no confirmation came back, so this may or may not "
    "have been applied. Do not send it again in this run — a second attempt is "
    "how the same fact ends up stored twice."
)
"""What a write says when Orchestration did not answer (ARTEL-332).

Kept in one place because this is the sentence that carries the weight. The
three outcomes of a write are stored / refused / unknown, and only the last one
is easy to get wrong: phrased as a failure, the model writes the fact again, and
the duplicate this whole contract exists to prevent arrives by a new route.
Orchestration performs the write and skips the reply when the run has no Agent
session, and an Orchestration older than ARTEL-331 never replies at all, so
silence is genuinely uninformative rather than bad news.

The other two outcomes are worded by each tool. "Recorded", "changed", "deleted",
"linked" and "removed" are different sentences, and a shared renderer that swapped
the noun would read as a form letter.
"""


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


def render_anchors(anchors: list[KnowledgeAnchor]) -> str:
    """Where a hit holds, folded to one line, or nothing at all.

    Empty for a hit with no anchor, and the caller then appends nothing — an entry
    that claims no screen is the common case, and it is the one this line must not
    grow the transcript for.

    An anchor with no scene name is dropped rather than printed as a bare screen
    number. The pair is what locates the fact, and a number on its own asks the
    agent to guess which scene it belonged to.
    """
    places = [
        f"{anchor.scene_name} (screen {anchor.screen_id})"
        if anchor.screen_id
        else anchor.scene_name
        for anchor in anchors
        if anchor.scene_name
    ]
    if not places:
        return ""
    return f"   [holds on {', '.join(places)}]"


def render_hit(index: int, hit: KnowledgeSearchHit) -> str:
    """One hit, with the provenance the agent needs to weigh it.

    The tag and source are printed because they qualify the claim: a `RULE` from
    `DOCS` is what the design says, while something from `QA` is what a previous
    run observed. The similarity is printed for the same reason — a weak match is
    still returned, and the agent has to be able to discount it rather than treat
    the top hit as authoritative by position alone.

    The id is printed because a search is the only way to reach one. Both
    `update_knowledge` and `forget_knowledge` take an id and refuse one this run
    has not been shown, so an entry the agent never read is an entry it can
    neither correct nor delete; unprinted, the id would make that rule
    unsatisfiable rather than safe.

    An anchor, where there is one, gets its own line for the reason the tag does:
    it qualifies the claim. Without it a fact that holds on one screen reads as a
    rule about the whole game, and the agent applies it where it is false. A hit
    with no anchor prints exactly what it printed before anchors existed — an
    empty line saying "no screen" would spend transcript on the common case and
    invite the reading that the anchor is missing rather than absent.
    """
    header = (
        f"{index}. [id {hit.id or 'unknown'} · {hit.tag or 'UNTAGGED'} · "
        f"from {hit.source or 'unknown'} · similarity {hit.score:.2f}]"
    )
    body = render_description(hit.description)
    lines = [header, f"   {hit.summary}"] if hit.summary else [header]
    if body:
        lines.append(f"   {body}")
    anchor_line = render_anchors(hit.anchors)
    if anchor_line:
        lines.append(anchor_line)
    if hit.neighbors:
        # Wrapped so `fold_stale_knowledge` can replace exactly this span and
        # nothing else — the hit's own summary and description must survive, and
        # a fold that guessed at where the neighbours start would eventually eat
        # one of them.
        lines.append(
            f"{NEIGHBOUR_BLOCK_START_PREFIX}{hit.id or 'unknown'}{NEIGHBOUR_BLOCK_START_SUFFIX}"
        )
        lines.extend(render_neighbour(n) for n in hit.neighbors)
        lines.append(NEIGHBOUR_BLOCK_END)
    return "\n".join(lines)


def render_expansion(payload, remaining: int) -> str:
    """What the model reads after an expansion that ran.

    The note IS printed here, unlike in a hit's folded neighbour lines. This is
    the call the agent spent a budget slot on precisely to see more, and the note
    is often the whole payload — the condition a `DEPENDS_ON` holds under, or what
    an older `LEADS_TO` edge says you did to walk it. Without it the answer is a
    fact about the graph rather than something to act on.
    """
    budget = f"{remaining} knowledge expansion(s) left in this run."
    if not payload.neighbors:
        return (
            "Nothing is linked to that entry, and nothing else in the knowledge "
            "base is close enough to it to mention. That is an answer, not an "
            f"error.\n\n{budget}"
        )
    lines = []
    for neighbour in payload.neighbors:
        lines.append(render_neighbour(neighbour))
        if neighbour.note:
            lines.append(f"     {neighbour.note}")
    listing = "\n".join(lines)
    header = f"Around {payload.id}"
    if payload.summary:
        header = f'{header} — "{payload.summary}"'
    truncated = (
        "\n\nThere was more than this and the rest was cut. Expand from one of "
        "these instead of assuming this is the whole neighbourhood."
        if payload.truncated
        else ""
    )
    return (
        f"{header}:\n\n{listing}\n\n"
        "A relation was asserted by a run that wrote down why. A `similar` entry "
        f"is a text-similarity guess with nobody behind it.{truncated}\n\n{budget}"
    )


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
