"""What shape the QA agent has, as data rather than as module constants.

The loop bounds, the per-run allowances, whether the model is shown screenshots
and which middleware wraps the calls are the axes a structural experiment moves.
Left as constants they can only be changed by editing and redeploying, which
makes "compare two agent structures" mean "compare two deployments" — and two
deployments cannot run side by side against the same game.

Versioning follows from that split:

* Prompts are data, so they live in version directories and many versions
  coexist (see ``app/prompts/``).
* Structure is code. Old structures are NOT kept as parallel copies here — an
  unmaintained copy keeps running while the tool signatures around it move on,
  and a comparison against a rotted structure is worse than no comparison,
  because nothing says whether the loss came from the design or from the rot.
  A past structure is identified by its commit and image tag and reproduced by
  redeploying it.
* What survives in the record is therefore an identity, not an implementation:
  ``QA_ARCH_LABEL`` for reading and grouping, ``arch_fingerprint`` for catching
  the case the label misses.

The label is bumped by hand and the fingerprint is derived, because each covers
the other's failure. A label alone goes stale the first time someone changes a
tool and forgets to bump it, and every run after that is filed under a structure
it did not have. A fingerprint alone is unreadable — a report grouped by
``a3f1c9d2e8b0`` says nothing about what that structure was.
"""

import hashlib
import json
from enum import StrEnum
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import get_settings
from app.llm.models import LLMModel, get_model_spec

# Bumped by hand when the structure changes in a way worth naming. Reads in
# reports; the fingerprint below is what actually separates the buckets.
#
# v2 because this is the second structure: v1 was one structured LLM call per
# step, replaced wholesale by the tool loop rather than kept beside it.
#
# v3 because `list_scene_capabilities`, `record_capability_verdict` and
# `record_new_capability` (`app/agents/qa/tools/capability_tools.py`) joined the
# tool set: the run can now read and write the content map's `capability` rows,
# which is a different structure even though the loop around it did not change.
#
# v4 because the screen's own text moved into a section of its own, `the screen
# reads:`, drawn on every model call instead of once (`app/qa/pulse.py`). Before
# it, a label the game left on screen was printed the reading it changed and
# never again, and `DEFAULT_KEEP_SCENES = 1` folded that message away on the next
# tool result — so a tutorial line was in front of the model for exactly one call.
# A run before this change and a run after it read different screens from the same
# game.
#
# Nothing the fingerprint hashes moved: the tool set, the tool signatures, the
# middleware order and every knob are the same. That is not this change being
# small, it is `arch_fingerprint` not covering the format of what the model reads
# — see its docstring for what it does cover. The hand-bumped label is the only
# thing in the record that can separate these two structures, which is the case
# the module docstring above says the label exists for.
#
# `screen_capture` (ARTEL-868) opened as an axis without moving this label. The
# default run's shape did not move: the default is `on_demand`, which is what the
# run has always done, and the tool set, the tool signatures and the middleware
# list are all unchanged by the field's arrival. `fold_stale_knowledge` is the
# precedent — `d78e6d4` added it to `QaArchSpec` as a new axis and left the label
# at `v2-tool-loop`. It is a weaker precedent than it looks: that commit also put
# `fold_knowledge_neighbours` into the default middleware list, which AGENTS.md
# does count as a change of shape. This change needs no such licence — the tool
# set, the tool signatures, the middleware list and every knob of the default run
# are provably where they were.
#
# The fingerprint does move, for every structure, because it hashes
# `arch.model_dump()` and that dump gained a key. That is expected and is why
# `_FINGERPRINT_SCHEME` is NOT bumped alongside it: the digests already separate,
# and bumping would only make one structure carry two of them.
#
# The arms of that axis are named in `QaArchSpec.label` rather than here, because
# a deployment runs them all from one image. The specs are checked in at
# `benchmarks/wordventure/arch/` in the workspace. Every arm takes a NEW label
# rather than reusing this constant: `agent_arch` is what `/api/qa-stats` groups
# cells by, so an arm left on the default label shares its cell with every run
# this deployment has ever done.
#
# * `v4-capture-on-demand` — `screen_capture=on_demand`, today's run.
# * `v4-capture-every-call` — the first `every_call` build, which stored the
#   picture in the conversation. Retired. It read 1.3% of its input from cache
#   against the baseline's 97.3% and cost $14.22 against $0.87, because storing a
#   picture per turn means editing an already-sent message to dispose of the last
#   one, and that moves the prefix out from under the cache boundary.
# * `v4-capture-by-role` — `screen_capture=by_role` (ARTEL-870). Two to four
#   pictures a call instead of one, chosen by what the run has done: the current
#   screen, the screen before the last action, and — when there is one — the scene
#   change and the last failure. Same tool set, same middleware; only the number of
#   transient messages moves, so the fingerprint separates it through the knob.
# * `v4-capture-transient` — the same knob after that fix: the picture rides on
#   one request and is never stored. **Same `screen_capture` value, so the same
#   fingerprint** — `arch_fingerprint` hashes the knobs, the tool schemas and the
#   middleware order, and none of those moved. The label is the only thing that
#   separates the two builds in the record, which is the case the module docstring
#   above says the hand-bumped label exists for.
QA_ARCH_LABEL = "v4-screen-text"

# Which facts the fingerprint is computed from. Bump when that set changes, so
# a digest from the old scheme is never mistaken for one from the new.
_FINGERPRINT_SCHEME = 1

# The fixed part of the tool-call budget: the opening observation, `finish_run`,
# and the headroom between them. The per-run allowances are added on top of it
# rather than folded into it — see `ResolvedArch.tool_call_limit`.
#
# Set to a ceiling no real run reaches. Both bounds are runaway guards now, not
# a ration: a run cut off mid-scenario reports nothing, which is the one outcome
# worse than a slow run. A caller that wants a bounded structure sends its own.
BASE_TOOL_CALLS = 1_000
TOOL_CALLS_PER_STEP = 1_000
RUN_DEADLINE_SECONDS = 86_400.0

# --- how much of the game and the knowledge base one run may move -------------
#
# These live here, with the other knobs, rather than beside the tools that spend
# them. They stopped being facts about those tools the moment a run could be
# asked to use different ones: two runs that differ only in how much they may
# look up are two structures, and the number has to be somewhere the fingerprint
# can see it. `knowledge.py` and `vision.py` re-export them under their old names
# and keep the prose about what each tool is for, which is still theirs.
#
# Every default below is a ceiling no real run reaches. The numbers used to be a
# ration — each sized against the argument that a run which keeps looking things
# up never decides — and what they produced in practice was runs that stopped
# mid-scenario with the question unanswered. Rationing is left to the tool
# descriptions, which still say what each tool is and is not for.
#
# They stay as `QaArchSpec` fields rather than being deleted: a structural
# experiment can still ask for a rationed run, and the fingerprint still has to
# separate one that did from one that did not.
MAX_CAPTURES_PER_RUN = 1_000_000
MAX_SEARCHES_PER_RUN = 1_000_000
MAX_RECORDS_PER_RUN = 1_000_000
MAX_FORGETS_PER_RUN = 1_000_000
MAX_LINKS_PER_RUN = 1_000_000
MAX_UNLINKS_PER_RUN = 1_000_000
MAX_EXPANDS_PER_RUN = 1_000_000
MAX_ISSUES_PER_RUN = 1_000_000


class VisionMode(StrEnum):
    """Whether the run shows the model screenshots.

    ``auto`` follows the model's own capability, which is what every run did
    before this was a choice. ``off`` is what makes "the same model with and
    without vision" a comparison one deployment can run.
    """

    auto = "auto"
    on = "on"
    off = "off"


class ScreenCaptureMode(StrEnum):
    """When a screenshot of the running game reaches the model.

    ``on_demand`` is what every run did before this was a choice: the model calls
    `capture_screen` and the picture arrives on the next model call. ``every_call``
    puts a fresh one in front of the model on every call it did not already ask for
    a whole-screen picture on.

    An enum rather than a bool for two reasons. `on_change` — capture only when
    the screen actually moved — is the next value this axis is expected to take,
    and the string lands verbatim in `run_config`, where `screen_capture=every_call`
    reads in a report and `auto_capture=true` does not.

    ``by_role`` is ``every_call`` with more than one picture. It carries the current
    screen, the screen as it was just before the last action, and — when there is
    one — the screen at the last scene change and at the last failure. Two, three
    or four, decided by what the run has done rather than by the model. The pilot
    that motivates it is in `ARTEL-868`: matched against the labelled steps, the
    `every_call` arm caught one of L1's six deliberately-refused steps and the
    baseline caught none and two. One picture says what is on screen; most QA
    verdicts turn on what *changed* when something was pressed, and that needs the
    pair.

    `every_call` does NOT remove `capture_screen` from the tool set. Removing it
    would make the arm two changes rather than one: the picture arriving unasked,
    and the loss of the cropped close-up of a single element that the tool's
    `target_id` gives. It would also strand the vision directive
    (`app/prompts/qa_run/*/vision_directive.md`), which names the tool, and fixing
    that would move `prompt_version` — an axis the comparison needs held still.
    """

    on_demand = "on_demand"
    every_call = "every_call"
    by_role = "by_role"


class QaArchError(ValueError):
    """The requested structure cannot be built for the requested model."""


class QaArchSpec(BaseModel):
    """A requested structure. Every field optional; the defaults are today's run.

    The ceilings are runaway guards, not rations: they sit far above what any
    scenario asks for, and exist so a malformed request cannot ask for an
    unbounded call budget or a deadline that never fires.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(default=QA_ARCH_LABEL, max_length=50)
    base_tool_calls: int = Field(default=BASE_TOOL_CALLS, ge=1, le=1_000_000)
    tool_calls_per_step: int = Field(default=TOOL_CALLS_PER_STEP, ge=1, le=1_000_000)
    deadline_seconds: float = Field(default=RUN_DEADLINE_SECONDS, gt=0, le=86_400)
    max_searches_per_run: int = Field(default=MAX_SEARCHES_PER_RUN, ge=0, le=1_000_000)
    max_records_per_run: int = Field(default=MAX_RECORDS_PER_RUN, ge=0, le=1_000_000)
    max_forgets_per_run: int = Field(default=MAX_FORGETS_PER_RUN, ge=0, le=1_000_000)
    max_links_per_run: int = Field(default=MAX_LINKS_PER_RUN, ge=0, le=1_000_000)
    max_unlinks_per_run: int = Field(default=MAX_UNLINKS_PER_RUN, ge=0, le=1_000_000)
    max_expands_per_run: int = Field(default=MAX_EXPANDS_PER_RUN, ge=0, le=1_000_000)
    max_captures_per_run: int = Field(default=MAX_CAPTURES_PER_RUN, ge=0, le=1_000_000)
    max_issues_per_run: int = Field(default=MAX_ISSUES_PER_RUN, ge=0, le=1_000_000)
    vision: VisionMode = VisionMode.auto
    screen_capture: ScreenCaptureMode = ScreenCaptureMode.on_demand
    fold_stale_scenes: bool = True
    # Folds the neighbour blocks the search volunteers, and only those (ARTEL-277).
    # Separate from `fold_stale_scenes` because the two are independently useful
    # to turn off: what they fold is recovered by different tools out of
    # different budgets.
    fold_stale_knowledge: bool = True
    # Compaction rewrites what the model reads once a run grows past a fraction of
    # its context, so a run with it and a run without it are two agents even with
    # the same tools. `None` defers to the deployment's own setting, which is what
    # every caller that does not care should send.
    compaction: bool | None = None
    compaction_trigger_fraction: float | None = Field(default=None, gt=0, le=1)
    compaction_keep_messages: int | None = Field(default=None, ge=1, le=200)
    compaction_min_new_messages: int | None = Field(default=None, ge=1, le=100)
    compaction_trim_tokens: int | None = Field(default=None, ge=0, le=200_000)

    @model_validator(mode="after")
    def forgets_need_records(self) -> "QaArchSpec":
        """Deleting without being able to write the replacement loses knowledge.

        `app/agents/qa/knowledge.py` exempts a replacement write from the record
        cap precisely so this cannot happen mid-run; a spec with deletions and no
        records would put the hole back before the run even starts.
        """
        if self.max_forgets_per_run > 0 and self.max_records_per_run == 0:
            raise QaArchError(
                "max_forgets_per_run > 0 requires max_records_per_run > 0: a "
                "deletion whose replacement cannot be written loses knowledge."
            )
        return self

    @model_validator(mode="after")
    def graph_tools_need_searches(self) -> "QaArchSpec":
        """An id can only be linked, unlinked or expanded after a search showed it.

        Both graph tools refuse an endpoint the run has not been shown, and the
        only things that show one are a search and the neighbours that ride along
        with it. A spec that allows them with no searches therefore enables tools
        that can never legally be called — the run pays for them in its tool-call
        ceiling and can never spend them.

        Unlike `forgets_need_records` this asks for no paired write: withdrawing a
        link is not something that leaves a hole somebody has to fill.
        """
        graph_calls = (
            self.max_links_per_run + self.max_unlinks_per_run + self.max_expands_per_run
        )
        if graph_calls > 0 and self.max_searches_per_run == 0:
            raise QaArchError(
                "max_links_per_run / max_unlinks_per_run / max_expands_per_run "
                "require max_searches_per_run > 0: an entry the run was never "
                "shown can be neither linked nor expanded."
            )
        return self


DEFAULT_ARCH = QaArchSpec()


class ResolvedArch(BaseModel):
    """A spec with nothing left to decide. `vision` is now a fact, not a wish."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    base_tool_calls: int
    tool_calls_per_step: int
    deadline_seconds: float
    max_searches_per_run: int
    max_records_per_run: int
    max_forgets_per_run: int
    max_links_per_run: int
    max_unlinks_per_run: int
    max_expands_per_run: int
    max_captures_per_run: int
    max_issues_per_run: int
    vision: bool
    screen_capture: ScreenCaptureMode
    fold_stale_scenes: bool
    fold_stale_knowledge: bool
    compaction: bool
    compaction_trigger_fraction: float
    compaction_keep_messages: int
    compaction_min_new_messages: int
    compaction_trim_tokens: int

    def tool_call_limit(self, steps: int) -> int:
        """Two bounds exist because either alone leaves a hole: a call cap alone
        lets one unanswered call hold the run open, a clock alone lets a fast
        loop burn budget. This is the call half; `deadline_seconds` is the clock.

        The allowances are added to the base rather than taken out of the steps.
        Left inside, `search_knowledge` would have spent its budget on the
        scenario and shortened every run by however much it looked things up.
        Captures are deliberately NOT added: the capture budget has always come
        out of the step allowance, and folding it in here would quietly widen
        every existing run's ceiling. Issue reports are not added for both
        reasons at once — a report is made about the step being judged, so it
        belongs in that step's allowance, and adding it here would widen the
        ceiling of every run including the ones that never file one.
        """
        base = (
            self.base_tool_calls
            + self.max_searches_per_run
            + self.max_records_per_run
            + self.max_forgets_per_run
            + self.max_links_per_run
            + self.max_unlinks_per_run
            + self.max_expands_per_run
        )
        return base + self.tool_calls_per_step * max(steps, 1)


def _resolved(spec: QaArchSpec, vision: bool) -> ResolvedArch:
    """Fill in every field the spec left to the deployment.

    The compaction knobs default to `None` rather than to numbers because their
    real default is operational — it lives in `Settings` and is tuned per
    environment. Copying those numbers here would be a second source of truth,
    and the copy is the one that goes stale.
    """
    settings = get_settings()
    chosen = spec.model_dump()
    deferred = {
        "compaction": settings.qa_compaction_enabled,
        "compaction_trigger_fraction": settings.qa_compaction_trigger_fraction,
        "compaction_keep_messages": settings.qa_compaction_keep_messages,
        "compaction_min_new_messages": settings.qa_compaction_min_new_messages,
        "compaction_trim_tokens": settings.qa_compaction_trim_tokens,
    }
    for field, fallback in deferred.items():
        if chosen[field] is None:
            chosen[field] = fallback
    return ResolvedArch(**{**chosen, "vision": vision})


@lru_cache(maxsize=1)
def default_resolved_arch() -> ResolvedArch:
    """The structure a caller gets by asking for nothing, with vision on.

    A convenience for callers that build tools outside a run — the tools do not
    read the compaction knobs, and a real run always resolves against its model.
    """
    return _resolved(DEFAULT_ARCH, vision=True)


def resolve_arch(spec: QaArchSpec, model: LLMModel) -> ResolvedArch:
    """Settle `vision` against what the model can actually do.

    A requested `on` that the model cannot honour is refused rather than
    downgraded. Silently running text-only under a spec that says vision would
    file the run in the wrong bucket, and a comparison built on that is wrong in
    the one direction nobody checks.
    """
    supports_vision = get_model_spec(model).supports_vision
    if spec.vision is VisionMode.on and not supports_vision:
        raise QaArchError(
            f"Model '{model.value}' cannot read images, so vision='on' cannot be "
            f"honoured. Use 'auto' to follow the model, or 'off' to state it."
        )
    vision = supports_vision if spec.vision is VisionMode.auto else spec.vision is VisionMode.on
    # Against the settled `vision`, not against `spec.vision`. The case that
    # matters is `vision='auto'` on a model that cannot read images, which is only
    # False after the line above. Checked here rather than in a `model_validator`
    # for the same reason: the spec alone does not know.
    #
    # Refused rather than downgraded, exactly as `vision='on'` is.
    # `middleware_names_for` only wires `capture_vision` when vision is on, so a
    # downgrade would leave a run labelled `every_call` running with no pictures at
    # all — the wrong-bucket failure the refusal above exists to prevent.
    if spec.screen_capture is not ScreenCaptureMode.on_demand and not vision:
        raise QaArchError(
            f"screen_capture='{spec.screen_capture.value}' needs vision, and this run "
            f"resolved to vision off (model '{model.value}', "
            f"vision='{spec.vision.value}'). Use screen_capture='on_demand', or a "
            f"model that reads images."
        )
    return _resolved(spec, vision)


def arch_fingerprint(
    arch: ResolvedArch, tools: list, middleware_names: tuple[str, ...]
) -> str:
    """A digest of the structure, and of nothing else.

    Model, prompt version and language are deliberately excluded. They are
    separate comparison axes, and a digest that moved with them could not group
    "the same structure under two models" — which is the comparison this exists
    to make possible.

    Tool argument schemas are in because a tool whose signature changed is a
    different tool to the model: same name, different affordance, different run.
    """
    facts = {
        "scheme": _FINGERPRINT_SCHEME,
        "kind": "create_agent-tool-loop",
        # `label` is excluded for the same reason the model is: it is a name, not
        # a structure. Hashing it would mean a rename split one structure into two
        # buckets, and the pair of records could no longer show that the rename
        # changed nothing.
        "arch": arch.model_dump(mode="json", exclude={"label"}),
        "tools": sorted(
            (tool.name, json.dumps(tool.args, sort_keys=True, default=str))
            for tool in tools
        ),
        # Order matters: middleware wraps in sequence, so a reordering is a
        # different call path even with the same members.
        "middleware": list(middleware_names),
    }
    digest = json.dumps(facts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(digest.encode()).hexdigest()[:12]


@lru_cache(maxsize=32)
def structure_of(arch: ResolvedArch) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """Tool names, middleware names and the fingerprint for one structure.

    The fingerprint has to be known when the session opens, before any channel
    exists, so the tools are built here against throwaway wiring. Only their
    names and schemas are read — `build_tools` defines closures and touches
    nothing — and building them from the real builder is the point: a signature
    that changed in `tools.py` moves the digest without anyone remembering to
    update a second list.

    Cached because the answer depends only on the structure, and a run should not
    pay to rediscover it.
    """
    # Imported here: `tools` imports this module's siblings, and at module scope
    # this would close the import cycle.
    from app.agents.qa.compaction import build_compact_tool
    from app.agents.qa.runner import middleware_names_for
    from app.agents.qa.tools import QaRunState, build_tools

    state = QaRunState(total_steps=0)
    tools = build_tools(_ThrowawayChannel(), state, arch)
    # The compaction middleware carries its own tool rather than registering it in
    # `build_tools` — a `compact_context` that sets a flag nothing reads is worse
    # than an absent one. It still has to be counted here: what the agent can call
    # is part of what the agent is.
    if arch.compaction:
        tools = tools + [build_compact_tool(state)]
    names = tuple(tool.name for tool in tools)
    middleware = middleware_names_for(arch)
    return names, middleware, arch_fingerprint(arch, tools, middleware)


class _ThrowawayChannel:
    """Stands in for a `QaRunChannel` while tool schemas are read off.

    The tools close over a channel but their schemas do not depend on it, and
    nothing is called here. Any attribute access would be a bug in this module,
    so it raises rather than returning a plausible-looking stub.
    """

    def __getattr__(self, name: str):
        raise AssertionError(
            f"structure_of() must not invoke the channel (asked for {name!r})."
        )
