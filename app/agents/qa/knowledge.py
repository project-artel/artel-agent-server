"""Asking the project's knowledge base what the screen cannot tell you.

A QA agent starts a run knowing only the scenario text it was handed. Everything
else about the game — what a mechanic costs, what counts as success, which of two
plausible readings of "the purchase fails" is the designed one — lives in the
project's knowledge base, extracted from its design documents. Without a way to
ask, a step whose `expected` depends on a rule is judged on a guess.

Three things are kept here rather than in `app/agents/qa/tools.py`, for the same
reason `vision.py` keeps the capture budget and image handling: they are this
tool's own subject matter, and the numbers, the vocabulary and the wording that
teaches the agent to ration them all have to move together.

Nothing in this module touches the game. A search changes no screen, so no scene
view is produced and none is appended to the result — see `app/agents/qa/context.py`
for why re-loading a scene the agent has already read is the thing to avoid.
"""

from app.qa.envelope import KnowledgeSearchHit, KnowledgeSearchResultPayload

# A run that keeps looking things up instead of deciding reaches the deadline
# with nothing reported — the same failure `MAX_CAPTURES_PER_RUN` exists to
# prevent. Lower than the capture budget because a game's rules do not change
# during a run: the second search on the same subject learns nothing the first
# one did not.
MAX_SEARCHES_PER_RUN = 6

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
    """
    header = f"{index}. [{hit.tag or 'UNTAGGED'} · from {hit.source or 'unknown'} · similarity {hit.score:.2f}]"
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
