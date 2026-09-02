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
    only), image input via ``image`` in ``architecture.input_modalities``, and
    its window as ``context_length`` with ``top_provider.max_completion_tokens``.
    Re-verify all three against that endpoint before adding or renaming entries.
    """

    gpt_5_6_luna = "openai/gpt-5.6-luna"
    gpt_4o = "openai/gpt-4o"
    claude_sonnet_5 = "anthropic/claude-sonnet-5"
    claude_opus_4_8 = "anthropic/claude-opus-4.8"
    gemini_2_5_flash = "google/gemini-2.5-flash"
    gemini_2_5_pro = "google/gemini-2.5-pro"
    gemma_4_free = "google/gemma-4-31b-it:free"
    # OpenRouter 슬러그가 아니다. Bedrock 추론 프로파일 ID 를 `bedrock/` 로 접두한
    # LiteLLM 표기이고, `openrouter_base_url` 이 Bedrock 을 아는 게이트웨이를 가리킬 때만
    # 뜻이 통한다. 값에 리전 접두(`us.`)와 판(`-v1:0`)까지 적는 것은, 어느 프로파일로
    # 청구되는지가 이 문자열 하나로 결정되기 때문이다.
    claude_haiku_4_5_bedrock = "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"


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
    # What the model can be *sent*, not the window it advertises: the catalog's
    # `context_length` minus `top_provider.max_completion_tokens`, because the
    # completion has to fit in the same window as the prompt. Stored already
    # subtracted so the name means what it says and no caller has to remember
    # the reservation.
    #
    # This is what `QaCompactionMiddleware` measures its trigger fraction
    # against (`app/agents/qa/compaction.py`). A value that is too high is the
    # failure that matters: compaction would then fire after the provider has
    # already refused the call, which is the thing it exists to prevent.
    max_input_tokens: int
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

    # 이 모델을 쓰는 배치에서 지식창고를 **읽을 수 있나.**
    #
    # 모델의 능력이 아니라 그 모델이 사는 경로의 사실이다. Bedrock 에는
    # `text-embedding-3-large` 가 없고, 다른 임베딩으로 바꾸면 차원은 맞아도
    # (`vector(1024)`) 벡터 공간이 달라 기존 항목과의 비교가 **에러 없이** 틀린 답을
    # 낸다. 검색이 조용히 엉뚱한 것을 돌려주는 것은 검색이 없는 것보다 나쁘다.
    #
    # False 면 `resolve_arch` 가 지식 관련 한도를 전부 0 으로 눕힌다.
    knowledge_search: bool = True

    @property
    def supports_vision(self) -> bool:
        return "image" in self.input_modalities


MODEL_SPECS: dict[LLMModel, ModelSpec] = {
    LLMModel.claude_haiku_4_5_bedrock: ModelSpec(
        provider=LLMProvider.anthropic,
        supports_strict_json=True,
        label="Claude Haiku 4.5 (AWS Bedrock)",
        # Anthropic 이 공표한 200k 창에서 출력 64k 를 뺀 값이다. 다른 항목과 같은 규칙으로
        # 이미 빼서 적는다 — 압축이 이 수를 기준으로 발동하므로 높게 적는 쪽이 위험하다.
        max_input_tokens=136_000,
        input_modalities=("text", "image"),
        # 임베딩을 같은 공간에서 못 구한다. 위 `knowledge_search` 주석에 이유가 있다.
        knowledge_search=False,
    ),
    LLMModel.gpt_5_6_luna: ModelSpec(
        provider=LLMProvider.openai,
        supports_strict_json=True,
        label="GPT-5.6 Luna",
        max_input_tokens=922_000,
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
        max_input_tokens=111_616,
        input_modalities=("text", "image", "file"),
    ),
    LLMModel.claude_sonnet_5: ModelSpec(
        provider=LLMProvider.anthropic,
        supports_strict_json=True,
        label="Claude Sonnet 5",
        max_input_tokens=872_000,
        input_modalities=("text", "image", "file"),
        reasoning=ReasoningKind.effort,
        reasoning_efforts=tuple(ReasoningEffort),
    ),
    LLMModel.claude_opus_4_8: ModelSpec(
        provider=LLMProvider.anthropic,
        supports_strict_json=True,
        label="Claude Opus 4.8",
        max_input_tokens=872_000,
        input_modalities=("text", "image", "file"),
        reasoning=ReasoningKind.effort,
        reasoning_efforts=tuple(ReasoningEffort),
    ),
    LLMModel.gemini_2_5_flash: ModelSpec(
        provider=LLMProvider.google,
        supports_strict_json=True,
        label="Gemini 2.5 Flash",
        max_input_tokens=983_041,
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
        max_input_tokens=983_040,
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
        max_input_tokens=229_376,
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
            "max_input_tokens": spec.max_input_tokens,
            "supports_vision": spec.supports_vision,
            "knowledge_search": spec.knowledge_search,
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
