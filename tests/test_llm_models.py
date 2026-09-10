"""The model catalog's numbers, and what depends on them being right."""

from app.llm.models import MODEL_SPECS, LLMModel, list_models


def test_every_model_in_the_enum_has_a_spec() -> None:
    """`get_model_spec` raises for a member with no entry in `MODEL_SPECS`, and the
    first thing that calls it is a request naming that model — so an enum entry
    added without a spec reaches a caller as a 500 rather than a startup failure.
    Adding a slug and forgetting the spec is the easy half of the mistake."""
    assert set(LLMModel) == set(MODEL_SPECS)


def test_every_model_declares_an_input_budget() -> None:
    """`QaCompactionMiddleware` takes its threshold from this number. A model added
    without one cannot be constructed at all, which is the point — the alternative,
    an optional field left unset, would disable compaction for that model silently
    and surface as a provider 400 in the middle of a run.
    """
    for model, spec in MODEL_SPECS.items():
        assert spec.max_input_tokens > 0, model


def test_the_catalog_tells_a_selection_ui_the_budget() -> None:
    """Which model a run picks changes how much room it has; a UI that cannot say
    so is asking the operator to guess."""
    entries = {entry["id"]: entry for entry in list_models()}

    assert set(entries) == {model.value for model in MODEL_SPECS}
    for model, spec in MODEL_SPECS.items():
        assert entries[model.value]["max_input_tokens"] == spec.max_input_tokens
