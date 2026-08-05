"""The agent's structure as a comparable thing.

What is being guarded here is a claim about the record, not about behaviour: that
two runs filed under the same structure really did have the same structure, and
that two runs filed apart really did differ. Every failure in this file is a
comparison that would have silently averaged two different agents.
"""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.agents.qa import arch as arch_module
from app.agents.qa.arch import (
    DEFAULT_ARCH,
    QA_ARCH_LABEL,
    QaArchError,
    QaArchSpec,
    ResolvedArch,
    VisionMode,
    resolve_arch,
    structure_of,
)
from app.config import get_settings
from app.llm.models import LLMModel, get_model_spec
from app.prompts import resolve_version
from app.qa.run_config import resolve_run_config


def resolved(**overrides) -> ResolvedArch:
    return resolve_arch(QaArchSpec(**overrides), LLMModel.gpt_4o)


# --- the fingerprint ----------------------------------------------------------


def test_the_same_structure_fingerprints_the_same() -> None:
    """Otherwise every run lands in its own bucket and nothing groups."""
    assert structure_of(resolved())[2] == structure_of(resolved())[2]


def test_a_changed_loop_bound_is_a_different_structure() -> None:
    """A run allowed more tool calls per step is not the same agent.

    This is the case the hand-written label misses: nobody bumps `QA_ARCH_LABEL`
    for a number, so without the fingerprint both runs would be filed under
    `v2-tool-loop` and the difference would be read as noise.
    """
    assert structure_of(resolved())[2] != structure_of(
        resolved(tool_calls_per_step=16)
    )[2]


def test_a_changed_allowance_is_a_different_structure() -> None:
    assert structure_of(resolved())[2] != structure_of(resolved(max_searches_per_run=3))[2]


def test_the_issue_allowance_is_part_of_the_structure_but_not_of_the_budget() -> None:
    """Both halves matter and they pull apart.

    It has to reach the fingerprint, or a run allowed one issue and a run allowed
    ten are averaged together. It must NOT reach `tool_call_limit`, because the
    reports are made about the step being judged and are paid for out of that
    step's allowance — folding them into the base would widen the ceiling of
    every run, including the ones that never file one (the same reason captures
    are left out).
    """
    assert structure_of(resolved())[2] != structure_of(resolved(max_issues_per_run=3))[2]
    assert resolved(max_issues_per_run=3).tool_call_limit(4) == resolved().tool_call_limit(4)


def test_turning_vision_off_changes_both_the_tools_and_the_fingerprint() -> None:
    """A run without `capture_screen` is a different agent, not a setting."""
    seeing_tools, _, seeing = structure_of(resolved(vision=VisionMode.on))
    blind_tools, _, blind = structure_of(resolved(vision=VisionMode.off))

    assert "capture_screen" in seeing_tools
    assert "capture_screen" not in blind_tools
    assert seeing != blind


def test_turning_folding_off_changes_the_fingerprint() -> None:
    """Middleware is part of the call path, so it is part of the structure."""
    assert structure_of(resolved())[2] != structure_of(resolved(fold_stale_scenes=False))[2]


def test_the_fingerprint_ignores_the_model_and_the_prompt() -> None:
    """The axes are independent, and a digest that moved with all of them could
    not group "the same structure under two models" — the comparison this exists
    to make possible."""
    sonnet = resolve_run_config(model=LLMModel.claude_sonnet_5)
    gpt = resolve_run_config(model=LLMModel.gpt_4o)
    pinned = resolve_run_config(model=LLMModel.gpt_4o, prompt_version="v1")

    assert sonnet.agent_fingerprint == gpt.agent_fingerprint == pinned.agent_fingerprint
    assert pinned.prompt_version != gpt.prompt_version


def test_the_label_travels_with_the_run() -> None:
    assert resolve_run_config().agent_arch == QA_ARCH_LABEL
    assert resolve_run_config(arch=QaArchSpec(label="candidate-a")).agent_arch == "candidate-a"


def test_a_relabelled_structure_keeps_its_fingerprint() -> None:
    """A rename is not a structural change.

    Hashing the label would split one structure into two buckets on a rename, and
    the records could no longer show that nothing about the agent had moved.
    """
    assert structure_of(resolved())[2] == structure_of(resolved(label="candidate-a"))[2]


# --- resolution ---------------------------------------------------------------


def test_auto_follows_the_model() -> None:
    assert resolved(vision=VisionMode.auto).vision is get_model_spec(
        LLMModel.gpt_4o
    ).supports_vision


def test_vision_on_is_refused_when_the_model_cannot_see(monkeypatch) -> None:
    """Refused, not downgraded.

    A run that asked for vision, silently got none, and was filed as a vision run
    is a wrong data point — and wrong is worse than absent, because nobody
    re-checks the rows that are there.
    """
    blind = replace(get_model_spec(LLMModel.gpt_4o), input_modalities=("text",))
    monkeypatch.setattr(arch_module, "get_model_spec", lambda _model: blind)

    with pytest.raises(QaArchError, match="cannot read images"):
        resolve_arch(QaArchSpec(vision=VisionMode.on), LLMModel.gpt_4o)

    # `auto` still resolves, to the truth about the model.
    assert resolve_arch(QaArchSpec(vision=VisionMode.auto), LLMModel.gpt_4o).vision is False


def test_deleting_without_being_able_to_replace_is_refused() -> None:
    """`knowledge.py` exempts a replacement write from the record cap so that a
    correction cannot strand halfway. A spec that allows deletions and no records
    would put that hole back before the run even starts."""
    # Pydantic wraps the validator's error, which is what turns it into a 422 at
    # the API rather than a 500.
    with pytest.raises(ValidationError, match="loses knowledge"):
        QaArchSpec(max_forgets_per_run=1, max_records_per_run=0)


@pytest.mark.parametrize(
    "field, value",
    [
        ("tool_calls_per_step", 0),
        ("tool_calls_per_step", 1000),
        ("deadline_seconds", 0),
        ("deadline_seconds", 10_000),
        ("max_captures_per_run", -1),
    ],
)
def test_the_knobs_are_bounded(field, value) -> None:
    """They arrive over the API. An unbounded budget is a way to spend a model's
    context and a game's time that no scenario asked for."""
    with pytest.raises(ValueError):
        QaArchSpec(**{field: value})


# --- the budget ---------------------------------------------------------------


def test_the_default_budget_matches_what_runs_had_before() -> None:
    """The allowances are added to the base, not taken out of the steps: left
    inside, `search_knowledge` would spend its budget on the scenario and shorten
    every run by however much it looked things up."""
    arch = resolved()
    base = 10 + 6 + 5 + 2

    assert arch.tool_call_limit(1) == base + 15
    assert arch.tool_call_limit(4) == base + 60
    # A scenario with no steps still gets one step's worth rather than none.
    assert arch.tool_call_limit(0) == arch.tool_call_limit(1)


def test_a_wider_allowance_widens_the_budget() -> None:
    assert resolved(max_searches_per_run=10).tool_call_limit(1) == resolved().tool_call_limit(1) + 4


# --- what the default is ------------------------------------------------------


def test_asking_for_nothing_is_todays_structure() -> None:
    """Every existing caller omits `arch`; none of them may change behaviour."""
    assert DEFAULT_ARCH == QaArchSpec()
    assert resolve_run_config().arch == resolve_run_config(arch=DEFAULT_ARCH).arch


def test_the_resolved_prompt_version_is_never_the_alias() -> None:
    """`None` means "the newest", whose meaning changes the day a v4 lands."""
    config = resolve_run_config(prompt_version=None)

    assert config.prompt_version == resolve_version("qa_run")
    assert config.prompt_version is not None


# --- compaction as part of the structure --------------------------------------


def test_compaction_changes_the_structure() -> None:
    """A run whose context gets rewritten mid-flight is not the same agent as one
    whose does not — different middleware, and one more tool the model can call."""
    on_tools, on_middleware, on_print = structure_of(resolved(compaction=True))
    off_tools, off_middleware, off_print = structure_of(resolved(compaction=False))

    assert "compact_context" in on_tools
    assert "compact_context" not in off_tools
    assert "compaction" in on_middleware and "compaction" not in off_middleware
    assert on_print != off_print


def test_a_compaction_knob_moves_the_fingerprint() -> None:
    """How often it fires changes what the model reads, so it is structural too."""
    assert (
        structure_of(resolved(compaction=True, compaction_keep_messages=4))[2]
        != structure_of(resolved(compaction=True, compaction_keep_messages=8))[2]
    )


def test_omitted_compaction_knobs_take_the_deployment_setting() -> None:
    """`None` means "whatever this deployment is tuned to", and the resolved form
    records the number rather than the deferral."""
    settings = get_settings()
    arch = resolved()

    assert arch.compaction is settings.qa_compaction_enabled
    assert arch.compaction_keep_messages == settings.qa_compaction_keep_messages
    assert arch.compaction_trigger_fraction == settings.qa_compaction_trigger_fraction


def test_the_summarizer_is_recorded_apart_from_the_run_model() -> None:
    """Compaction summarizes with its own model and prompt, so a run's context can
    be rewritten by something the run's own model and prompt axes do not name."""
    on = resolve_run_config(arch=QaArchSpec(compaction=True))
    off = resolve_run_config(arch=QaArchSpec(compaction=False))

    assert on.compaction_model == get_settings().qa_compaction_model
    assert on.prompt_hashes["summary"]
    assert off.compaction_model is None
    assert "summary" not in off.prompt_hashes
