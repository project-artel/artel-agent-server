from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LLMProvider(StrEnum):
    """Values are the author segment of an OpenRouter slug.

    Kept identical to the slug prefix so a spec's provider can be checked
    against the slug sitting next to it in `MODEL_SPECS` by reading, and so
    `build_chat_model`'s Anthropic-only cache branch cannot drift.
    """

    openai = "openai"
    anthropic = "anthropic"
    google = "google"
    xai = "x-ai"
    moonshotai = "moonshotai"
    zai = "z-ai"
    qwen = "qwen"


class LLMModel(StrEnum):
    """Selectable models. Values are OpenRouter model slugs, except where noted.

    Slugs and the capability flags below reflect the live
    ``GET https://openrouter.ai/api/v1/models`` catalog: a model advertises
    strict json_schema support via ``structured_outputs`` in its
    ``supported_parameters`` (``response_format`` alone means json_object mode
    only), image input via ``image`` in ``architecture.input_modalities``, and
    its window as ``context_length`` with ``top_provider.max_completion_tokens``.
    Re-verify all three against that endpoint before adding or renaming entries.
    """

    gpt_5_6_luna = "openai/gpt-5.6-luna"
    gpt_5_6_sol = "openai/gpt-5.6-sol"
    gpt_chat_latest = "openai/gpt-chat-latest"
    claude_sonnet_5 = "anthropic/claude-sonnet-5"
    claude_opus_5 = "anthropic/claude-opus-5"
    gemini_3_7_flash = "google/gemini-3.7-flash"
    gemini_2_5_pro = "google/gemini-2.5-pro"
    gemma_4_free = "google/gemma-4-31b-it:free"
    grok_4_6 = "x-ai/grok-4.6"
    kimi_k3 = "moonshotai/kimi-k3"
    glm_5_3_flash = "z-ai/glm-5.3-flash"
    qwen3_8_max = "qwen/qwen3.8-max"

    # 여기부터는 OpenRouter slug 가 아니다. `bedrock/` 접두를 뗀 나머지가 그대로
    # Bedrock 의 inference profile ID 이고, `build_chat_model` 이 그 접두를 보고
    # `ChatBedrockConverse` 로 간다. 리전 접두(`us.`)와 판(`-v1:0`)까지 적는 것은
    # 어느 프로파일로 청구되는지가 이 문자열 하나로 정해지기 때문이다.
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
class ModelPricing:
    """백만 토큰당 달러. provider 가 청구액을 안 줄 때 비용을 되짚는 데만 쓴다.

    적은 날짜와 출처를 함께 남긴다. **이 표는 스스로 낡는 것을 모른다** — provider 가
    단가를 바꿔도 여기는 그대로이고, 그때 나오는 숫자는 틀렸는데 그럴듯하다. 값을
    고칠 때 [as_of] 도 함께 고친다.
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float
    cache_read_per_mtok: float
    # 이 단가를 확인한 날과 어디서 봤는지. 숫자만 있으면 언제 것인지 알 수 없다.
    as_of: str
    source: str


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
    #
    # `reasoning_efforts` is the whole set the model accepts, and
    # `validate_reasoning` rejects anything outside it. Most of the catalog
    # takes fewer than all five: Gemini 3.7 Flash has no `max` or `xhigh`, and
    # asking for one is a provider 400 several turns into a run, not a
    # degradation to the nearest supported effort.
    reasoning: ReasoningKind | None = None
    reasoning_efforts: tuple[ReasoningEffort, ...] | None = None
    reasoning_min_tokens: int | None = None
    reasoning_max_tokens: int | None = None
    reasoning_step: int | None = None
    # 백만 토큰당 달러. **provider 가 청구액을 안 알려주는 모델에만 채운다.**
    #
    # OpenRouter 는 응답에 `cost` 를 실어 주므로 여기 값이 필요 없고, 있으면 오히려
    # 위험하다 — 청구액이 있는데 추정치로 덮으면 실제로 나간 돈을 잃는다.
    # `usage.py` 가 provider 가 준 값을 항상 먼저 쓴다.
    #
    # **없으면 `cost_usd` 를 비운다. 추정하지 않는다.** 단가는 provider 가 언제든 바꾸고
    # 이 표는 그것을 모른 채 그럴듯한 숫자를 계속 내놓는다. 빈 칸은 누구도 오해하지
    # 않지만 낡은 숫자는 아무도 의심하지 않는다.
    #
    # `cache_write` 는 캐시에 처음 실을 때, `cache_read` 는 캐시에서 읽어 올 때의 단가다.
    # 셋 다 input 계열이지만 배수가 달라 따로 적는다.
    pricing: ModelPricing | None = None
    # 사용자가 고르지 않았을 때 켤 예산. **provider 기본값이 없는 모델에만 쓴다.**
    #
    # OpenRouter 의 GPT-5.6 은 파라미터를 생략하면 자기 기본값(medium)으로 추론한다.
    # Bedrock 의 Anthropic 은 생략하면 아예 안 한다 — `budget_tokens` 가 필수이고
    # 그것을 안 주면 `thinking` 을 켤 수 없다. 그래서 같은 "안 골랐다"가 두 모델에서
    # 정반대로 작동했고, 한쪽만 추론하는 상태로 둘을 비교하고 있었다.
    #
    # 여기에 값이 있으면 고르지 않은 런도 그 예산으로 켠다. 없으면 provider 에게
    # 맡긴다 — OpenRouter 쪽이 그 경우다.
    reasoning_default_tokens: int | None = None

    @property
    def supports_vision(self) -> bool:
        return "image" in self.input_modalities


MODEL_SPECS: dict[LLMModel, ModelSpec] = {
    LLMModel.claude_haiku_4_5_bedrock: ModelSpec(
        provider=LLMProvider.anthropic,
        supports_strict_json=True,
        label="Claude Haiku 4.5 (AWS Bedrock)",
        # 실측한 창은 200,000 이다(195,011 통과, 그 위 `prompt is too long`). 거기서
        # 출력 예약 8,192 를 뺀다 — `max_tokens` 가 `budget_tokens` 보다 커야 해서
        # 예산 4,096 이 그 예약분을 정한다.
        #
        # 앞서 136,000 으로 적었던 것은 근거 없는 수였다. 그 값이 압축 임계를 정하므로
        # 200k 창을 68% 만 쓰고 있었다.
        max_input_tokens=191_808,
        input_modalities=("text", "image"),
        # Anthropic 은 `effort` 를 안 받는다. 토큰 예산으로만 켠다.
        reasoning=ReasoningKind.max_tokens,
        # Anthropic 최소치. 이보다 작으면 `thinking` 이 거절된다.
        reasoning_min_tokens=1_024,
        reasoning_max_tokens=32_000,
        # Luna 의 medium 이 런당 1,500~5,000 추론 토큰을 썼다(실측 3,300 호출).
        # 그 중간이다 — 두 모델을 나란히 재려면 이 축이 비슷해야 한다.
        reasoning_default_tokens=4_096,
        # Bedrock 은 응답에 청구액을 안 싣는다. 계정 단위 CloudWatch 로만 보이고 그것은
        # 런에 안 붙어서, 이 표가 없으면 런 하나가 얼마였는지 영영 못 본다.
        #
        # **캐시 쓰기는 TTL 에 따라 단가가 다르다** — 5 분 $1.25, 1 시간 $2.00. 여기 값이
        # 5 분인 것은 우리가 그 TTL 로 쓰고 있어서다(응답의 `ephemeral_5m_input_tokens`).
        # 캐시 TTL 을 바꾸면 이 수도 함께 바꿔야 하고, 안 바꾸면 계산이 조용히 낮게 나온다.
        #
        # 배치 추론은 절반이다($0.50 / $2.50). 우리는 안 쓰므로 안 적는다 — 쓰게 되면
        # 같은 호출에 단가가 둘이 되므로 이 자리가 아니라 호출 쪽이 골라야 한다.
        pricing=ModelPricing(
            input_per_mtok=1.00,
            output_per_mtok=5.00,
            cache_write_per_mtok=1.25,
            cache_read_per_mtok=0.10,
            as_of="2026-09-03",
            source="AWS 콘솔, Claude Haiku 4.5 / us-west-2 / on-demand",
        ),
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
    LLMModel.gpt_5_6_sol: ModelSpec(
        provider=LLMProvider.openai,
        supports_strict_json=True,
        label="GPT-5.6 Sol",
        max_input_tokens=922_000,
        input_modalities=("text", "image", "file"),
        # Six efforts like Luna, the unmodelled `none` included.
        reasoning=ReasoningKind.effort,
        reasoning_efforts=tuple(ReasoningEffort),
    ),
    # The one entry that does not reason, which is why it is here: `reasoning`
    # left None is a state the request validator and the catalog API both have
    # to keep answering for. The slug tracks whatever ChatGPT currently serves,
    # so its window is the one most worth re-checking against the catalog.
    LLMModel.gpt_chat_latest: ModelSpec(
        provider=LLMProvider.openai,
        supports_strict_json=True,
        label="GPT Chat Latest",
        max_input_tokens=272_000,
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
    LLMModel.claude_opus_5: ModelSpec(
        provider=LLMProvider.anthropic,
        supports_strict_json=True,
        label="Claude Opus 5",
        max_input_tokens=872_000,
        input_modalities=("text", "image", "file"),
        reasoning=ReasoningKind.effort,
        reasoning_efforts=tuple(ReasoningEffort),
    ),
    LLMModel.gemini_3_7_flash: ModelSpec(
        provider=LLMProvider.google,
        supports_strict_json=True,
        label="Gemini 3.7 Flash",
        max_input_tokens=983_040,
        input_modalities=("text", "image", "file", "audio", "video"),
        # An effort, where 2.5 Pro below takes a token budget: 3.x Flash
        # advertises `reasoning_effort` and three named efforts, and reasoning
        # is mandatory, so the run reasons at the provider's `medium` whenever
        # the request leaves it out.
        reasoning=ReasoningKind.effort,
        reasoning_efforts=(
            ReasoningEffort.high,
            ReasoningEffort.medium,
            ReasoningEffort.low,
        ),
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
    LLMModel.grok_4_6: ModelSpec(
        provider=LLMProvider.xai,
        supports_strict_json=True,
        label="Grok 4.6",
        # 50k out of a 500k window, because the top provider reserves 450k of
        # it for the completion. That is by far the smallest budget in the
        # catalog — an eighteenth of Luna's — so a QA run on this model
        # compacts many times where the others never trigger.
        max_input_tokens=50_000,
        input_modalities=("text", "image", "file"),
        reasoning=ReasoningKind.effort,
        reasoning_efforts=(
            ReasoningEffort.xhigh,
            ReasoningEffort.high,
            ReasoningEffort.medium,
            ReasoningEffort.low,
        ),
    ),
    LLMModel.kimi_k3: ModelSpec(
        provider=LLMProvider.moonshotai,
        supports_strict_json=True,
        label="Kimi K3",
        # The same reservation story as Grok: a 1,048,576 window with 943,718
        # of it held for the completion.
        max_input_tokens=104_858,
        input_modalities=("text", "image", "video"),
        reasoning=ReasoningKind.effort,
        reasoning_efforts=(
            ReasoningEffort.max,
            ReasoningEffort.high,
            ReasoningEffort.low,
        ),
    ),
    LLMModel.glm_5_3_flash: ModelSpec(
        provider=LLMProvider.zai,
        supports_strict_json=True,
        label="GLM-5.3 Flash",
        max_input_tokens=1_179_648,
        input_modalities=("text", "image", "video"),
        reasoning=ReasoningKind.effort,
        reasoning_efforts=(
            ReasoningEffort.max,
            ReasoningEffort.high,
            ReasoningEffort.low,
        ),
    ),
    LLMModel.qwen3_8_max: ModelSpec(
        provider=LLMProvider.qwen,
        supports_strict_json=True,
        label="Qwen3.8 Max",
        max_input_tokens=868_928,
        input_modalities=("text", "image", "video"),
        # The catalog lists a fifth effort, `minimal`, which `ReasoningEffort`
        # does not model; the four below are what a request may ask for.
        reasoning=ReasoningKind.effort,
        reasoning_efforts=(
            ReasoningEffort.xhigh,
            ReasoningEffort.high,
            ReasoningEffort.medium,
            ReasoningEffort.low,
        ),
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
    if reasoning.effort is not None:
        allowed = get_model_spec(model).reasoning_efforts
        if allowed is not None and reasoning.effort not in allowed:
            names = ", ".join(effort.value for effort in allowed)
            raise ValueError(
                f"Model '{model}' does not support reasoning.effort "
                f"'{reasoning.effort.value}'. Supported: {names}."
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
