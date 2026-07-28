from dataclasses import dataclass
from enum import StrEnum


class LLMProvider(StrEnum):
    openai = "openai"
    anthropic = "anthropic"
    google = "google"


class LLMModel(StrEnum):
    """Selectable models. Values are OpenRouter model slugs.

    Slugs and the ``supports_strict_json`` flags below reflect the live
    ``GET https://openrouter.ai/api/v1/models`` catalog: a model advertises
    strict json_schema support via ``structured_outputs`` in its
    ``supported_parameters`` (``response_format`` alone means json_object mode
    only). Re-verify against that endpoint before adding or renaming entries.
    """

    gpt_4o_mini = "openai/gpt-4o-mini"
    gpt_4o = "openai/gpt-4o"
    claude_sonnet_5 = "anthropic/claude-sonnet-5"
    claude_opus_4_8 = "anthropic/claude-opus-4.8"
    gemini_2_5_flash = "google/gemini-2.5-flash"
    gemini_2_5_pro = "google/gemini-2.5-pro"
    gemma_4_free = "google/gemma-4-31b-it:free"


@dataclass(frozen=True)
class ModelSpec:
    provider: LLMProvider
    # True when the model advertises `structured_outputs` (strict json_schema).
    # Models with only `response_format` fall back to json_object mode.
    supports_strict_json: bool
    label: str
    # True when the model accepts image blocks. A model without it is not a
    # failure: the QA run drops to text-only rather than refusing to start, and
    # the capture tool is left out of its toolset so it cannot ask for one.
    supports_vision: bool = True


MODEL_SPECS: dict[LLMModel, ModelSpec] = {
    LLMModel.gpt_4o_mini: ModelSpec(
        provider=LLMProvider.openai,
        supports_strict_json=True,
        label="GPT-4o mini",
    ),
    LLMModel.gpt_4o: ModelSpec(
        provider=LLMProvider.openai,
        supports_strict_json=True,
        label="GPT-4o",
    ),
    LLMModel.claude_sonnet_5: ModelSpec(
        provider=LLMProvider.anthropic,
        supports_strict_json=True,
        label="Claude Sonnet 5",
    ),
    LLMModel.claude_opus_4_8: ModelSpec(
        provider=LLMProvider.anthropic,
        supports_strict_json=True,
        label="Claude Opus 4.8",
    ),
    LLMModel.gemini_2_5_flash: ModelSpec(
        provider=LLMProvider.google,
        supports_strict_json=True,
        label="Gemini 2.5 Flash",
    ),
    LLMModel.gemini_2_5_pro: ModelSpec(
        provider=LLMProvider.google,
        supports_strict_json=True,
        label="Gemini 2.5 Pro",
    ),
    # json_object-only (no `structured_outputs`): exercises the strict fallback.
    # Text-only as well, which is what keeps the vision path's fallback honest.
    LLMModel.gemma_4_free: ModelSpec(
        provider=LLMProvider.google,
        supports_strict_json=False,
        label="Gemma 4 (free)",
        supports_vision=False,
    ),
}


DEFAULT_MODEL: LLMModel = LLMModel.gpt_4o_mini


def get_model_spec(model: LLMModel) -> ModelSpec:
    try:
        return MODEL_SPECS[model]
    except KeyError as exc:
        raise ValueError(f"No spec registered for model '{model}'.") from exc


def list_models() -> list[dict[str, str | bool]]:
    """Model catalog for frontend selection UIs."""
    return [
        {
            "id": model.value,
            "label": spec.label,
            "provider": spec.provider.value,
            "supports_strict_json": spec.supports_strict_json,
            "supports_vision": spec.supports_vision,
        }
        for model, spec in MODEL_SPECS.items()
    ]
