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
QA_ARCH_LABEL = "v3-content-map-tools"

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
