from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LLMProvider(StrEnum):
    openai = "openai"
    anthropic = "anthropic"
    google = "google"


class LLMModel(StrEnum):
    """Selectable models. Values are OpenRouter model slugs.

    Slugs and the capability flags below reflect the live
    ``GET https://openrouter.ai/api/v1/models`` catalog: a model advertises
    strict json_schema support via ``structured_outputs`` in its
    ``supported_parameters`` (``response_format`` alone means json_object mode
    only), and image input via ``image`` in ``architecture.input_modalities``.
    Re-verify both against that endpoint before adding or renaming entries.
    """

    gpt_5_6_luna = "openai/gpt-5.6-luna"
    gpt_4o = "openai/gpt-4o"
    claude_sonnet_5 = "anthropic/claude-sonnet-5"
    claude_opus_4_8 = "anthropic/claude-opus-4.8"
    gemini_2_5_flash = "google/gemini-2.5-flash"
    gemini_2_5_pro = "google/gemini-2.5-pro"
    gemma_4_free = "google/gemma-4-31b-it:free"


class ReasoningKind(StrEnum):
    effort = "effort"
    max_tokens = "max_tokens"


class ReasoningEffort(StrEnum):
    max = "max"
    xhigh = "xhigh"
    high = "high"
    medium = "medium"
    low = "low"


class ReasoningConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    effort: ReasoningEffort | None = None
    max_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_one_budget(self) -> "ReasoningConfig":
        if (self.effort is None) == (self.max_tokens is None):
            raise ValueError("Set exactly one of reasoning.effort or reasoning.max_tokens.")
        return self

    def as_openrouter(self) -> dict[str, str | int | bool]:
        budget: dict[str, str | int | bool]
        if self.effort is not None:
            budget = {"effort": self.effort.value}
        else:
            budget = {"max_tokens": self.max_tokens}
        return {**budget, "exclude": True}


@dataclass(frozen=True)
class ModelSpec:
    provider: LLMProvider
    # True when the model advertises `structured_outputs` (strict json_schema).
    # Models with only `response_format` fall back to json_object mode.
    supports_strict_json: bool
    label: str
    # True when the model accepts image blocks — `image` in the catalog's
    # `architecture.input_modalities`. Verify it there like `supports_strict_json`
    # rather than assuming: Gemma 4 was carried here as text-only on the strength of
    # a ticket description and is in fact `image,text,video`, which silently cost it
    # the capture tool.
    #
    # A model without vision is not a failure: the QA run drops to text-only rather
    # than refusing to start, and the capture tool is left out of its toolset so it
    # cannot ask for a picture nothing can read. Every model in the catalog below
    # currently sees, so that path is covered by tests rather than by a live model.
    input_modalities: tuple[str, ...] = ("text", "image")
    # Verified against the live catalog's `reasoning` object. None means the
    # model must not receive OpenRouter's reasoning parameter.
    reasoning: ReasoningKind | None = None
    reasoning_efforts: tuple[ReasoningEffort, ...] | None = None
    reasoning_min_tokens: int | None = None
    reasoning_max_tokens: int | None = None
    reasoning_step: int | None = None

    @property
    def supports_vision(self) -> bool:
        return "image" in self.input_modalities


MODEL_SPECS: dict[LLMModel, ModelSpec] = {
    LLMModel.gpt_5_6_luna: ModelSpec(
        provider=LLMProvider.openai,
        supports_strict_json=True,
        label="GPT-5.6 Luna",
        input_modalities=("text", "image", "file"),
        # The catalog also advertises a sixth effort, `none`, which
        # `ReasoningEffort` does not model; the five below are the whole enum.
        reasoning=ReasoningKind.effort,
        reasoning_efforts=tuple(ReasoningEffort),
    ),
    LLMModel.gpt_4o: ModelSpec(
        provider=LLMProvider.openai,
        supports_strict_json=True,
        label="GPT-4o",
        input_modalities=("text", "image", "file"),
    ),
    LLMModel.claude_sonnet_5: ModelSpec(
        provider=LLMProvider.anthropic,
        supports_strict_json=True,
        label="Claude Sonnet 5",
        input_modalities=("text", "image", "file"),
        reasoning=ReasoningKind.effort,
        reasoning_efforts=tuple(ReasoningEffort),
    ),
    LLMModel.claude_opus_4_8: ModelSpec(
        provider=LLMProvider.anthropic,
        supports_strict_json=True,
        label="Claude Opus 4.8",
        input_modalities=("text", "image", "file"),
        reasoning=ReasoningKind.effort,
        reasoning_efforts=tuple(ReasoningEffort),
    ),
    LLMModel.gemini_2_5_flash: ModelSpec(
        provider=LLMProvider.google,
        supports_strict_json=True,
        label="Gemini 2.5 Flash",
        input_modalities=("text", "image", "file", "audio", "video"),
        reasoning=ReasoningKind.max_tokens,
        reasoning_min_tokens=0,
        reasoning_max_tokens=24576,
        reasoning_step=128,
    ),
    LLMModel.gemini_2_5_pro: ModelSpec(
        provider=LLMProvider.google,
        supports_strict_json=True,
        label="Gemini 2.5 Pro",
        input_modalities=("text", "image", "file", "audio", "video"),
        reasoning=ReasoningKind.max_tokens,
        reasoning_min_tokens=128,
        reasoning_max_tokens=32768,
        reasoning_step=128,
    ),
    # json_object-only (no `structured_outputs`): exercises the strict fallback.
    LLMModel.gemma_4_free: ModelSpec(
        provider=LLMProvider.google,
        supports_strict_json=False,
        label="Gemma 4 (free)",
        input_modalities=("text", "image", "video"),
    ),
}


DEFAULT_MODEL: LLMModel = LLMModel.gpt_5_6_luna


def get_model_spec(model: LLMModel) -> ModelSpec:
    try:
        return MODEL_SPECS[model]
    except KeyError as exc:
        raise ValueError(f"No spec registered for model '{model}'.") from exc


def validate_reasoning(
    model: LLMModel, reasoning: ReasoningConfig | None
) -> ReasoningConfig | None:
    if reasoning is None:
        return None

    supported = get_model_spec(model).reasoning
    requested = (
        ReasoningKind.effort
        if reasoning.effort is not None
        else ReasoningKind.max_tokens
    )
    if supported is None:
        raise ValueError(f"Model '{model}' does not support configurable reasoning.")
    if requested != supported:
        raise ValueError(
            f"Model '{model}' requires reasoning.{supported.value}, "
            f"not reasoning.{requested.value}."
        )
    if reasoning.max_tokens is not None:
        minimum = get_model_spec(model).reasoning_min_tokens
        maximum = get_model_spec(model).reasoning_max_tokens
        if minimum is not None and reasoning.max_tokens < minimum:
            raise ValueError(
                f"Model '{model}' requires reasoning.max_tokens >= {minimum}."
            )
        if maximum is not None and reasoning.max_tokens > maximum:
            raise ValueError(
                f"Model '{model}' requires reasoning.max_tokens <= {maximum}."
            )
    return reasoning


def list_models() -> list[dict[str, Any]]:
    """Model catalog for frontend selection UIs."""
    return [
        {
            "id": model.value,
            "label": spec.label,
            "provider": spec.provider.value,
            "supports_strict_json": spec.supports_strict_json,
            "supports_vision": spec.supports_vision,
            "input_modalities": list(spec.input_modalities),
            "multimodal": len(spec.input_modalities) > 1,
            "reasoning": (
                {
                    "kind": spec.reasoning.value,
                    "efforts": (
                        [effort.value for effort in spec.reasoning_efforts]
                        if spec.reasoning_efforts
                        else None
                    ),
                    "min_tokens": spec.reasoning_min_tokens,
                    "max_tokens": spec.reasoning_max_tokens,
                    "step": spec.reasoning_step,
                }
                if spec.reasoning
                else None
            ),
        }
        for model, spec in MODEL_SPECS.items()
    ]
