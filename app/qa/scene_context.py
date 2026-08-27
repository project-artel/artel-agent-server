"""What the project already knows about the scene the run is standing on.

Two halves, fetched together in one round trip at the start of a run and held in
memory for it (ARTEL-611's `/internal/projects/{id}/game-builds/{id}/scene-context`):

- **capabilities** — what Orchestration's content map says can be done on this
  scene, built from play rather than written by hand.
- **anchored knowledge** — the facts that are true HERE and nowhere else: a
  control that behaves unlike it does anywhere else, a way back that does
  nothing on this one screen.

Without this the agent starts each scene knowing neither, and the only way to
find out is to spend a tool call on a search it has to think to make. That is
the whole point: this arrives without being asked for, and costs no budget.

**This block is anchored knowledge ONLY.** Everything true of the game wherever
the player is standing — how input is read, what a resource is for, what the
objective is — is most of the knowledge base and is NOT here. It is reached with
`search_knowledge`, and both the block's own heading and the system prompt say
so, because a list sitting in front of the agent is read as complete. Narrowing
the search by scene instead would be the same mistake from the other side: the
server-side filter drops un-anchored entries, so a scene-filtered search hides
exactly the game-wide rules the agent most needs.

**Where this is drawn, and why that decides its size.** It rides under the scene
view a tool result carries (`SceneMemory.render` in `app/qa/scene.py`), drawn on
the first render after the run moves to a new scene and not again while it stays
there. A tool result is never rewritten, so one paint lasts the whole visit; a
paint per turn would instead leave one copy of the same paragraph per turn
permanently in the context. That is also why every list here is bounded, why each
entry is one line, and why knowledge carries an id and a summary and nothing
else — the full text stays one `search_knowledge` away, on the turn that actually
wants it.

**A failed lookup is not a failed run.** `fetch_scene_context` never raises: a run
that cannot start because an advisory lookup timed out is worse than a run
without the advice. The block is simply absent.
"""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.config import get_settings

logger = logging.getLogger(__name__)

# How many capability lines the block carries, and how many knowledge lines.
#
# Single digits while the payload will happily carry fifty capabilities for a
# busy scene. A line here is paid once per scene visit and then sits in the
# conversation for the rest of the run, beside everything else that scene visit
# produced — the block competes with the screen it describes, and the screen is
# what a verdict is read off.
#
# Capabilities get the larger share because a capability is what the agent might
# ACT on, and knowledge lines are recoverable — the id is printed, and a search
# brings the entry back whole. Neither list is ranked: the payload's order is
# fixed by Orchestration (`SceneContextResponse.scenes` is sorted, and so is
# what hangs off it), and reordering here would cost the prompt cache on every
# run for a guess at what matters.
MAX_CAPABILITIES_IN_SCENE_CONTEXT = 8
MAX_KNOWLEDGE_IN_SCENE_CONTEXT = 6

# Per free-text field. A summary is written as one line, but nothing enforces
# that on the way in, and one pathological entry must not be able to double the
# block. Clipped rather than dropped, and the clip is visible, so the agent can
# tell "that is all there is" from "there is more".
MAX_TEXT_CHARS = 160

# The markers the rendered block is wrapped in. Its own pair, distinct from
# `SCENE_VIEW_START_PREFIX`, and drawn OUTSIDE it: `fold_stale_scenes` replaces
# everything between that other pair with a placeholder, and a block inside it
# would be folded away with the screen it describes. Nothing folds this one —
# folding rewrites a message the model has already been sent, which is what broke
# the prompt prefix in ARTEL-621, and a block drawn once per scene visit is not
# large enough to be worth paying that for.
SCENE_CONTEXT_START = "<<scene context>>"
SCENE_CONTEXT_END = "<<end scene context>>"

# What the endpoint is called, relative to `orchestration_base_url`.
SCENE_CONTEXT_PATH = "/internal/projects/{project_id}/game-builds/{game_build_id}/scene-context"

# One round trip, once per scenario, before the agent's first turn. Short because
# the run is waiting on it and the answer is advisory: a slow lookup that
# eventually succeeds costs the run more than no lookup at all.
SCENE_CONTEXT_TIMEOUT_SECONDS = 5.0


def _clip(text: str) -> str:
    stripped = " ".join(text.split())
    if len(stripped) <= MAX_TEXT_CHARS:
        return stripped
    return f"{stripped[:MAX_TEXT_CHARS]}…"


class _Payload(BaseModel):
    """Shared parsing rules for everything that comes off the wire here.

    `extra="ignore"` so a field added on the Orchestration side is a no-op rather
    than a refusal, and every field carries a default so one unexpected null does
    not cost the run the whole block.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="ignore"
    )


class SceneCapability(_Payload):
    """One thing the content map says can be done on this scene.

    The condition tree, the evidence address and the effect are deliberately not
    here — the endpoint does not send them, because nothing that cannot be drawn
    as one prompt line earns a place in a block this small.

    `control_selector_hint` is a HINT and never an aiming key. It carries sibling
    indices, so it shifts between runs, and the action protocol takes an int
    instance id anyway. `_capability_line` prints a path only as orientation, and
    the block's own heading says the ids to act on come from the scene view.
    """

    capability_id: str = ""
    # The reference that survives a re-import, where there is one. This is the
    # value worth quoting in a report or a recorded entry.
    capability_key: str | None = None
    summary: str = ""
    given_text: str | None = None
    interaction: str = ""
    input_key: str | None = None
    control_path: str | None = None
    control_label: str | None = None
    status: str = ""
    actionability: str = ""
    observability: str = ""
    applicability: str = ""
    verification: str = ""
    repeat_until_done: bool = False
    control_selector_hint: str | None = None


class SceneKnowledge(_Payload):
    """One fact anchored to this scene: an id and a summary, and nothing else.

    The description is not in the payload and must not be fetched to fill this
    line. The id is what makes the omission cheap — the agent can search the
    entry back whole on the one turn it actually needs the text, instead of the
    run carrying that text through every turn it does not.
    """

    knowledge_id: str = ""
    summary: str = ""


class SceneContextEntry(_Payload):
    """One scene's slice: what can be done here, and what is true only here.

    `known_to_content_map` false means this scene reached the list through an
    anchor naming a scene the map has never heard of — anchors are stored without
    being checked against the map, so this is an ordinary state rather than a
    fault. Such a scene has no capabilities and its knowledge still matters.

    "The map knows this scene and there is nothing to do on it" and "the map has
    never heard of this scene" are different answers, and the rendering keeps them
    apart: collapsing them would have the agent read a mapped scene as an unknown
    one.
    """

    scene_name: str = ""
    known_to_content_map: bool = False
    scene_summary: str | None = None
    capabilities: list[SceneCapability] = Field(default_factory=list)
    knowledge: list[SceneKnowledge] = Field(default_factory=list)


class SceneContext(_Payload):
    """Every scene of one build, as fetched once at the start of a run.

    Held whole because the run cannot know which scenes it will visit, and
    re-fetching per scene change would put a network round trip on the critical
    path of every transition for an answer that cannot have changed.
    """

    game_build_id: str = ""
    # None is the normal state for a build nobody has uploaded evidence for yet.
    # The lookup answers 200 with an empty or anchor-only `scenes` in that case,
    # which is not an error and must not be reported as one.
    content_map_id: str | None = None
    capture: str | None = None
    scenes: list[SceneContextEntry] = Field(default_factory=list)

    def entry_for(self, scene_name: str | None) -> SceneContextEntry | None:
        """The slice for one scene, by exact name.

        The scene name is the only key joining the two halves — it is what the
        content map files a capability under and what an anchor names — and the
        scene view above prints it. Matched exactly: a game is free to name two
        scenes `Battle` and `Battle 2`, and a fuzzy match would hand the agent
        another screen's rules as if they were this one's.
        """
        if not scene_name:
            return None
        for entry in self.scenes:
            if entry.scene_name == scene_name:
                return entry
        return None

    def render(self, scene_name: str | None) -> str | None:
        """The block for one scene, or `None` when there is nothing to say.

        `None` — rather than a block saying "nothing is known" — when the lookup
        carries no entry for this scene at all. There is a real difference between
        the two, and it is not one this block can carry honestly: no entry means
        the map has no capabilities AND no anchor names the scene, which is the
        ordinary case for most scenes of most builds. A paragraph saying so on
        arriving at every such scene would be the block's largest single cost and
        its smallest contribution.
        """
        entry = self.entry_for(scene_name)
        if entry is None:
            return None
        return f"{SCENE_CONTEXT_START}\n" + "\n".join(_entry_lines(entry)) + f"\n{SCENE_CONTEXT_END}"


def _capability_line(capability: SceneCapability) -> str:
    """One capability as one line.

    Shape: `[key] interaction "label" (path) — summary  [status, verification]`,
    with each part dropped when the payload does not carry it. The path is
    orientation, not a target: `control_path` first because it has no sibling
    indices in it, and `control_selector_hint` only as a fallback, which is all
    that field is for.

    `capability_key` and no fallback to `capability_id`. The key is the reference
    that survives a re-import and is therefore the one worth quoting in a report
    or a recorded entry; the id is a join column no agent-facing tool accepts, and
    printing it here would put a bare number in brackets one section above the
    knowledge ids, which ARE quoted — to `report_step`. One of those two numbers
    being uncitable is not something a line of prompt can reliably teach.
    """
    parts: list[str] = (
        [f"[{capability.capability_key}]"] if capability.capability_key else []
    )

    action = capability.interaction or "?"
    if capability.input_key:
        action = f"{action} {capability.input_key}"
    if capability.control_label:
        action = f'{action} "{capability.control_label}"'
    where = capability.control_path or capability.control_selector_hint
    if where:
        action = f"{action} ({where})"
    parts.append(action)

    if capability.summary:
        parts.append(f"— {_clip(capability.summary)}")

    # Two axes rather than all five. `status` is the derived answer to "can this
    # be turned into a step at all", and `verification` is the separate question
    # of whether anyone has actually pressed it — which is what decides where an
    # agent looking for something to try should look first. The three axes
    # `status` is derived FROM would be three more columns saying what it already
    # said.
    flags = [flag for flag in (capability.status, capability.verification) if flag]
    if capability.repeat_until_done:
        flags.append("repeat until done")
    if flags:
        parts.append(f"[{', '.join(flags)}]")

    line = "  " + " ".join(parts)
    if capability.given_text:
        # The precondition, on the same line: a capability whose precondition does
        # not hold is one the agent should not reach for, and a second line per
        # capability would double the block for something that is usually short.
        line = f"{line}  given: {_clip(capability.given_text)}"
    return line


def _cut_note(shown: int, total: int, what: str) -> str:
    """What a truncated list says about itself.

    Said out loud, always. A silently shortened list reads as the whole list, and
    an agent that believes it has seen every capability on a screen stops looking
    for the one that was cut.
    """
    return f"showing {shown} of {total} {what}; {total - shown} cut for space"


def _entry_lines(entry: SceneContextEntry) -> list[str]:
    lines = [
        # The boundary, first and in the heading itself. What is here is anchored
        # to this scene; the game-wide rules — most of the knowledge base — are
        # not, and are reached by searching. The prompt says this too; a block
        # that only the prompt bounded would be read as complete by any run whose
        # prompt was compacted away.
        "what is already known about this scene, and ONLY about this scene. "
        "Rules that hold across the game are not here — search_knowledge reaches those.",
    ]
    if entry.scene_summary:
        lines.append(f"the map describes it as: {_clip(entry.scene_summary)}")

    lines.append("")
    if not entry.known_to_content_map:
        lines.append(
            "the content map has never heard of this scene, so it lists nothing that "
            "can be done here. That is not a fault — this scene reached you because "
            "knowledge below is anchored to it."
        )
    elif not entry.capabilities:
        lines.append(
            "the content map knows this scene and lists nothing that can be done here."
        )
    else:
        shown = entry.capabilities[:MAX_CAPABILITIES_IN_SCENE_CONTEXT]
        heading = f"the content map says this can be done here ({len(entry.capabilities)} known)"
        if len(shown) < len(entry.capabilities):
            heading = (
                "the content map says this can be done here — "
                f"{_cut_note(len(shown), len(entry.capabilities), 'capabilities')}"
            )
        lines.append(f"{heading}:")
        lines.extend(_capability_line(capability) for capability in shown)
        # Said once, under the list rather than per line. A path is where the
        # control sits in the map, and the map is documentation: the build in
        # front of you is what the scene view above reports, and its ids are the
        # only thing the action tools accept.
        lines.append(
            "  (a path is where the map found the control, not something to aim at — "
            "take ids and coordinates from the scene view above)"
        )

    lines.append("")
    if not entry.knowledge:
        lines.append("no knowledge is anchored to this scene.")
        return lines

    shown_knowledge = entry.knowledge[:MAX_KNOWLEDGE_IN_SCENE_CONTEXT]
    heading = "knowledge anchored to this scene, id and summary only"
    if len(shown_knowledge) < len(entry.knowledge):
        heading = (
            f"{heading} — "
            f"{_cut_note(len(shown_knowledge), len(entry.knowledge), 'entries')}"
        )
    lines.append(f"{heading}:")
    lines.extend(
        f"  [{item.knowledge_id}] {_clip(item.summary)}" for item in shown_knowledge
    )
    lines.append(
        "  (search_knowledge on one of these brings back its full text, and its id is "
        "what report_step's used_knowledge_ids takes)"
    )
    return lines


async def fetch_scene_context(
    project_id: int | None,
    game_build_id: int | None,
    qa_try_id: int | None = None,
    base_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> SceneContext | None:
    """Read one build's scene context, or `None` for any reason at all.

    **Nothing raised in here reaches the run.** A timeout, a 404, a payload whose
    shape has drifted, an Orchestration that is simply down — every one of them
    produces a warning and a `None`, and the run proceeds with no block. The
    alternative is a run that fails to start because an advisory lookup did, which
    is strictly worse: the block makes a good run better, and its absence costs
    nothing the agent cannot get from a tool.

    `None` is also the answer when the caller has no ids to ask with. Today
    Orchestration's session open does not send `project_id`/`game_build_id`
    (`QaSessionOpenContext` carries `game_instance_id` and `qa_run_id`), so this
    lands on the same path as a failed lookup until it does — deliberately, so
    the feature ships dark rather than shipping a run that refuses to open.
    """
    if project_id is None or game_build_id is None:
        return None
    root = base_url if base_url is not None else get_settings().orchestration_base_url
    if not root:
        # No Orchestration to ask. A local run and the test suite both live here.
        return None

    url = root.rstrip("/") + SCENE_CONTEXT_PATH.format(
        project_id=project_id, game_build_id=game_build_id
    )
    params = {"qaTryId": str(qa_try_id)} if qa_try_id is not None else None
    try:
        if client is not None:
            response = await client.get(url, params=params)
        else:
            async with httpx.AsyncClient(timeout=SCENE_CONTEXT_TIMEOUT_SECONDS) as owned:
                response = await owned.get(url, params=params)
        response.raise_for_status()
        return SceneContext.model_validate(response.json())
    except Exception as error:  # noqa: BLE001 - an advisory lookup never fails a run
        logger.warning("[QA] scene context unavailable, running without it: %s", error)
        return None
