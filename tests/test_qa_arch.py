"""The agent's structure as a comparable thing.

What is being guarded here is a claim about the record, not about behaviour: that
two runs filed under the same structure really did have the same structure, and
that two runs filed apart really did differ. Every failure in this file is a
comparison that would have silently averaged two different agents.
"""

from dataclasses import replace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.agents.qa import arch as arch_module
from app.agents.qa.arch import (
    BASE_TOOL_CALLS,
    DEFAULT_ARCH,
    MAX_EXPANDS_PER_RUN,
    MAX_FORGETS_PER_RUN,
    MAX_LINKS_PER_RUN,
    MAX_RECORDS_PER_RUN,
    MAX_SEARCHES_PER_RUN,
    MAX_UNLINKS_PER_RUN,
    QA_ARCH_LABEL,
    QaArchError,
    QaArchSpec,
    ResolvedArch,
    TOOL_CALLS_PER_STEP,
    VisionMode,
    resolve_arch,
    structure_of,
)
from app.config import get_settings
from app.llm.models import LLMModel, get_model_spec
from app.prompts import resolve_version
from app.qa.run_config import resolve_run_config


def resolved(**overrides) -> ResolvedArch:
    return resolve_arch(QaArchSpec(**overrides), LLMModel.gpt_chat_latest)


# --- the fingerprint ----------------------------------------------------------


def test_the_same_structure_fingerprints_the_same() -> None:
    """Otherwise every run lands in its own bucket and nothing groups."""
    assert structure_of(resolved())[2] == structure_of(resolved())[2]


def test_a_changed_loop_bound_is_a_different_structure() -> None:
    """A run allowed more tool calls per step is not the same agent.

    This is the case the hand-written label misses: nobody bumps `QA_ARCH_LABEL`
    for a number, so without the fingerprint both runs would be filed under
    `v3-content-map-tools` and the difference would be read as noise.
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
    gpt = resolve_run_config(model=LLMModel.gpt_chat_latest)
    pinned = resolve_run_config(model=LLMModel.gpt_chat_latest, prompt_version="v1")

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
        LLMModel.gpt_chat_latest
    ).supports_vision


def test_vision_on_is_refused_when_the_model_cannot_see(monkeypatch) -> None:
    """Refused, not downgraded.

    A run that asked for vision, silently got none, and was filed as a vision run
    is a wrong data point — and wrong is worse than absent, because nobody
    re-checks the rows that are there.
    """
    blind = replace(get_model_spec(LLMModel.gpt_chat_latest), input_modalities=("text",))
    monkeypatch.setattr(arch_module, "get_model_spec", lambda _model: blind)

    with pytest.raises(QaArchError, match="cannot read images"):
        resolve_arch(QaArchSpec(vision=VisionMode.on), LLMModel.gpt_chat_latest)

    # `auto` still resolves, to the truth about the model.
    assert resolve_arch(QaArchSpec(vision=VisionMode.auto), LLMModel.gpt_chat_latest).vision is False


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
        ("tool_calls_per_step", 1_000_001),
        ("deadline_seconds", 0),
        ("deadline_seconds", 86_401),
        ("max_captures_per_run", -1),
    ],
)
def test_the_knobs_are_bounded(field, value) -> None:
    """They arrive over the API. The ceilings sit far above any real run, so what
    they catch is a malformed request rather than an ambitious one — but an
    unbounded call budget or a deadline that never fires still has to be refused."""
    with pytest.raises(ValueError):
        QaArchSpec(**{field: value})


# --- the budget ---------------------------------------------------------------


def test_the_default_budget_is_the_base_plus_the_run_scoped_allowances() -> None:
    """The allowances are added to the base, not taken out of the steps: left
    inside, `search_knowledge` would spend its budget on the scenario and shorten
    every run by however much it looked things up."""
    arch = resolved()
    # base + searches + records + forgets + links + unlinks + expands. The graph
    # allowances join the base for the same reason the others did: they are
    # run-scoped, not per-step, so leaving them inside the step allowance would
    # shorten every run by however much it walked the graph. Read off the
    # constants rather than written out: the numbers are ceilings now and move
    # together, and a copy here would only assert that somebody updated a copy.
    base = (
        BASE_TOOL_CALLS
        + MAX_SEARCHES_PER_RUN
        + MAX_RECORDS_PER_RUN
        + MAX_FORGETS_PER_RUN
        + MAX_LINKS_PER_RUN
        + MAX_UNLINKS_PER_RUN
        + MAX_EXPANDS_PER_RUN
    )

    assert arch.tool_call_limit(1) == base + TOOL_CALLS_PER_STEP
    assert arch.tool_call_limit(4) == base + TOOL_CALLS_PER_STEP * 4
    # A scenario with no steps still gets one step's worth rather than none.
    assert arch.tool_call_limit(0) == arch.tool_call_limit(1)


def test_an_allowance_moves_the_budget_with_it() -> None:
    """Narrowed rather than widened: the default now sits at the field's ceiling,
    and a rationed run is the thing a caller still asks for."""
    narrower = MAX_SEARCHES_PER_RUN - 4
    assert (
        resolved(max_searches_per_run=narrower).tool_call_limit(1)
        == resolved().tool_call_limit(1) - 4
    )


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

    # 설정을 비워 두면 런의 모델을 따른다. 그래도 축은 따로 남는다 — 아래가 그 자리다.
    assert on.compaction_model == on.model.value
    assert on.prompt_hashes["summary"]
    assert off.compaction_model is None
    assert "summary" not in off.prompt_hashes


def test_a_pinned_summarizer_stays_apart_from_the_run_model() -> None:
    """고정하면 런 모델과 갈린다. 압축이 별개 축이라는 것이 이 갈림으로 드러난다."""
    with patch.object(
        get_settings(), "qa_compaction_model", LLMModel.gemma_4_free.value
    ):
        resolved = resolve_run_config(
            arch=QaArchSpec(compaction=True), model=LLMModel.gpt_5_6_luna
        )

    assert resolved.compaction_model == LLMModel.gemma_4_free.value
    assert resolved.model is LLMModel.gpt_5_6_luna


# --- the label stays paired with the structure it names ------------------------

# Pinned next to the label they were computed under, so a diff to either one
# shows the other in the same hunk. `QA_ARCH_LABEL` moved once already without
# this pair moving with it: the content map tool set landed in
# `app/agents/qa/tools/capability_tools.py` (`list_scene_capabilities`,
# `record_capability_verdict`, `record_new_capability`) and changed
# `structure_of(...)`'s fingerprint, but `QA_ARCH_LABEL` stayed `v2-tool-loop` —
# so every run before and after the tools landed was filed under the same label
# while carrying two different fingerprints. That is exactly the failure the
# module docstring in `app/agents/qa/arch.py` names: "A label alone goes stale
# the first time someone changes a tool and forgets to bump it." This pair
# exists so the next such change fails a test instead of silently filing two
# structures under one name.
_LABEL_THIS_STRUCTURE_WAS_PINNED_UNDER = "v3-content-map-tools"

# The five knobs `_resolved()` in `arch.py` otherwise fills in from
# `get_settings()`: `compaction`, `compaction_trigger_fraction`,
# `compaction_keep_messages`, `compaction_min_new_messages` and
# `compaction_trim_tokens`. Left as `None` — this test's default — the
# structure below would depend on whatever `.env` or environment variables this
# machine happens to have, and the same source would then fingerprint
# differently on a machine with `QA_COMPACTION_ENABLED=false`. What this test
# checks is whether the structure moved, not what these five numbers are, so
# they only need to be *fixed*, not *right* — do not "correct" them to match a
# deployment's `.env`. `compaction=True` is kept on because `compact_context`
# being in the tool list is itself part of what this test pins.
_PINNED_COMPACTION_KNOBS = {
    "compaction": True,
    "compaction_trigger_fraction": 0.9,
    "compaction_keep_messages": 20,
    "compaction_min_new_messages": 4,
    "compaction_trim_tokens": 8000,
}
_EXPECTED_DEFAULT_TOOL_NAMES = (
    "observe_scene",
    "inspect_object",
    "search_knowledge",
    "record_knowledge",
    "update_knowledge",
    "forget_knowledge",
    "link_knowledge",
    "unlink_knowledge",
    "expand_knowledge",
    "include_screen_selector",
    "exclude_screen_selector",
    "list_scene_capabilities",
    "record_capability_verdict",
    "record_new_capability",
    "click_button",
    "enter_text",
    "press_key",
    "move_pointer",
    "click_at",
    "double_click_at",
    "hold_mouse_button",
    "release_mouse_button",
    "hold_key",
    "release_key",
    "set_input_axis",
    "set_input_button",
    "drag_pointer",
    "pause_game_time",
    "resume_game_time",
    "reset_game",
    "wait_for_operator",
    "report_step",
    "report_issue",
    "finish_run",
    "reply_to_operator",
    "capture_screen",
    "compact_context",
)
_EXPECTED_DEFAULT_FINGERPRINT = "e8e1d4764809"


def test_the_default_structure_is_pinned_to_the_label_that_names_it() -> None:
    """Catches the failure the module docstring warns about before a run does.

    `QA_ARCH_LABEL` is bumped by hand; nothing forces it to move the day a tool
    is added, renamed, or reshaped. This test is that force: it freezes what
    `structure_of(...)` returns today for the default spec, next to the label
    this freeze was taken under, so either one moving without the other fails
    here instead of silently splitting one label across two fingerprints in the
    record.

    Resolved with every knob given explicitly rather than through
    `default_resolved_arch()`, which defers the compaction knobs to
    `get_settings()` — see `_PINNED_COMPACTION_KNOBS` above for why that would
    make this test's pass/fail depend on the machine it runs on.

    A failure here is not a bug in this test — it is a prompt to decide something
    a diff cannot decide by itself:

    * If the tool names below changed, or a tool's argument schema changed
      without its name changing, the agent's structure moved. Bump
      `QA_ARCH_LABEL` in `app/agents/qa/arch.py`, add a sentence to the comment
      above it saying why (the same way v2 and v3 are explained there), then
      update `_LABEL_THIS_STRUCTURE_WAS_PINNED_UNDER` and
      `_EXPECTED_DEFAULT_FINGERPRINT` below to match.
    * If the tool names are unchanged and only `_EXPECTED_DEFAULT_FINGERPRINT`
      moved, something that is not a tool name still hashed differently — a
      middleware or a loop bound. Read `arch_fingerprint`'s `facts` to see what
      changed, then decide by the same rule: a real structural change gets a
      label bump, a change to a tool's description text or a docstring does
      not — update only `_EXPECTED_DEFAULT_FINGERPRINT` in that case.
    """
    resolved_arch = resolve_arch(
        QaArchSpec(vision=VisionMode.on, **_PINNED_COMPACTION_KNOBS),
        LLMModel.gpt_chat_latest,
    )
    names, _middleware, fingerprint = structure_of(resolved_arch)

    assert QA_ARCH_LABEL == _LABEL_THIS_STRUCTURE_WAS_PINNED_UNDER, (
        f"QA_ARCH_LABEL is now {QA_ARCH_LABEL!r}, but the fingerprint and tool "
        f"list pinned in this test were taken under "
        f"{_LABEL_THIS_STRUCTURE_WAS_PINNED_UNDER!r}. If the structure actually "
        "changed, update this constant to match — that is this test catching up "
        "to a label bump that already happened. If it did not, the label bump was "
        "the mistake."
    )
    assert names == _EXPECTED_DEFAULT_TOOL_NAMES, (
        "The default tool set no longer matches _EXPECTED_DEFAULT_TOOL_NAMES — a "
        "tool was added, removed, or renamed. See this test's docstring for what "
        "to do next."
    )
    assert fingerprint == _EXPECTED_DEFAULT_FINGERPRINT, (
        f"the pinned default structure now hashes to {fingerprint!r}, not the "
        f"{_EXPECTED_DEFAULT_FINGERPRINT!r} pinned here under "
        f"QA_ARCH_LABEL = {_LABEL_THIS_STRUCTURE_WAS_PINNED_UNDER!r}. The tool "
        "names above are unchanged, so the move came from something else — a "
        "middleware or a loop bound. See this test's docstring for what to do "
        "next."
    )
