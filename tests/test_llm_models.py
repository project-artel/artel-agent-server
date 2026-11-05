"""The model catalog's numbers, and what depends on them being right."""

from app.llm.catalog import get_model_catalog


def test_every_model_declares_an_input_budget() -> None:
    """`QaCompactionMiddleware` takes its threshold from this number. A row
    without one would disable compaction for that model silently and surface as a
    provider 400 in the middle of a run.
    """
    for model, spec in get_model_catalog().items():
        assert spec.max_input_tokens > 0, model
