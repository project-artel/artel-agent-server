"""Reading from, and writing to, the project's knowledge base.

A QA agent starts a run knowing only the scenario text it was handed. Everything
else about the game — what a mechanic costs, what counts as success, which of two
plausible readings of "the purchase fails" is the designed one — lives in the
project's knowledge base, extracted from its design documents. Without a way to
ask, a step whose `expected` depends on a rule is judged on a guess. And without
a way to write, everything a run works out for itself dies with the run.

These things are kept here rather than in `app/agents/qa/tools/`, for the same
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

**An anchor says where a fact is true.** Most of what belongs here is true of the
game wherever the player is standing — how input is read, what a resource is for,
what the objective is. Some of it is not: a control that behaves on one screen
unlike anywhere else, a shop that refuses a purchase in a way no other does. An
anchor is the scene, and where the run knows it the screen, that such a fact is
tied to. It rides on the write (`scene_name` and `screen_id` on
`KnowledgeCreatePayload`) and comes back on a hit (`anchors`).

Both fields are optional, and **an absent anchor is a claim, not a gap**: it says
the fact holds everywhere, which is the ordinary case and the one that has to stay
cheap. That is why nothing here fills the anchor from the run's current scene —
doing so would file every game-wide rule under whichever screen the run happened
to be standing on, and a rule filed that way is one the run on the next screen
never finds. The agent names the anchor or leaves it out; the failure mode runs
both ways, and the tool description is where the agent is taught to tell them
apart.

The screen map itself is NOT anchored knowledge and does not live here at all —
which screens exist and how to get between them is owned by Orchestration's
`content_map`, filled from play (ARTEL-582). An anchor points AT a screen; it does
not describe one.

Nothing in this module touches the game. Neither a search nor a write changes a
screen, so no scene view is produced and none is appended to any result — see
`app/agents/qa/context.py` for why re-loading a scene the agent has already read
is the thing to avoid.
"""

from app.prompts import load_tool_description
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
# somebody who already suspects it happened. `tool_forget_knowledge.md` is
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
# results are NOT folded the way scene views are (`fold_context` folds only a
# result's neighbour block, never the hit itself), so every hit stays in the
# transcript until the run ends.
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
#
# `PART_OF` 는 이 튜플에 아예 들어가지 않는다. `LEADS_TO` 와는 다른 이유다.
# agent 가 한 번 주장했다가 나중에 그 권한을 잃은 관계가 아니라, 지식 항목을
# 그 출처인 문서 node 에서 뽑아내는 순간 적재 경로가 만드는 구조 관계이기
# 때문이다(ARTEL-748). 여기에는 `link_knowledge` 가 판단할 실행 시점의 여지가
# 없으므로 agent 가 주장할 것도 없다. 읽는 쪽은 다시 별개다 — `_REVERSED` 는
# 이 표기를 알고 있어서, `expand_knowledge` 가 한 번도 쓴 적 없는 edge 의
# 방향을 그대로 보여줄 수 있다(ARTEL-749).
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

def knowledge_tool_description(tool_name: str, **values: object) -> str:
    """What the model reads for one knowledge tool, from `qa_run/<version>/tool_<tool_name>.md`.

    The budget and the tag and relation lists are placeholders filled from the
    same constants this module and the arch spec own, so the number the agent is
    told and the number it gets cannot drift apart.
    """
    return load_tool_description(tool_name).body.format(**values)


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
    "PART_OF": "contains",
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
        # Wrapped so `fold_context` can replace exactly this span and
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
