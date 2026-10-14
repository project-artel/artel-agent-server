from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


class LLMProvider(StrEnum):
    """Values are the author segment of an OpenRouter slug.

    Kept identical to the slug prefix so a catalog row's provider can be checked
    against its id by reading, and so `build_chat_model`'s Anthropic-only cache
    branch and `_bedrock`'s provider branch cannot drift.
    """

    openai = "openai"
    anthropic = "anthropic"
    google = "google"
    xai = "x-ai"
    moonshotai = "moonshotai"
    zai = "z-ai"
    qwen = "qwen"


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
    # 0 = **추론을 끈다.** None(미지정)과 다르다 — 미지정은 카탈로그 기본 예산으로
    # 켜진다(Bedrock 경로). 라우터처럼 판단이 얕은 호출이 끄는 데 쓴다: 669토큰
    # 분류에 추론이 13초·수백 토큰을 쓰던 실측(2026-09-09)이 이 값의 이유다.
    max_tokens: int | None = Field(default=None, ge=0)

    @property
    def disabled(self) -> bool:
        return self.max_tokens == 0

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
    # `long_context_threshold_tokens` 를 넘는 prompt 전체에 적용되는 두 번째 단가다.
    # None 이면 context 길이와 무관하게 위 단가 하나만 쓴다.
    long_context_threshold_tokens: int | None = None
    long_context_input_per_mtok: float | None = None
    long_context_output_per_mtok: float | None = None
    long_context_cache_write_per_mtok: float | None = None
    long_context_cache_read_per_mtok: float | None = None


@dataclass(frozen=True)
class ModelSpec:
    """orchestration server 의 `llm_model` 테이블 row 하나.

    `app/llm/catalog.py` 가 `GET /internal/llm-models` 응답으로 만든다. 값은 row 를
    추가하는 migration 에서 serving provider 의 catalog 와 대조한다 — OpenRouter 의
    `GET /api/v1/models`, `bedrock/` row 는 AWS model card.
    """

    # OpenRouter slug, 또는 `bedrock/` 뒤에 Bedrock inference profile ID.
    id: str
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
    # cannot ask for a picture nothing can read. Every model in the catalog
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
    # 지금 이 값을 채우는 항목은 없다. Gemini 2.5 Pro 가 유일했고 그것을 카탈로그에서
    # 뺐다. 필드는 남긴다 — catalog 의 `reasoning.step` 이 계속 실리고, 예산을 단위로
    # 받는 모델이 다시 들어오면 그 자리다.
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
    # 관리자가 끈 모델은 False 다. 사용자가 고르는 요청(`EnabledLLMModel`)만 이 값을 보고
    # 거절한다. router·compaction·사용량 단가처럼 내부에서 쓰는 쪽은 꺼진 row 도 그대로 쓴다.
    enabled: bool = True

    @property
    def supports_vision(self) -> bool:
        return "image" in self.input_modalities


def get_model_spec(model: str) -> ModelSpec:
    """`model` 의 catalog row. catalog 에 없으면 `ValueError` 다.

    `enabled` 는 보지 않는다. 꺼진 모델을 거절하는 것은 `EnabledLLMModel` 의 일이다.
    catalog 를 한 번도 못 받았으면 `ModelCatalogUnavailableError` 가 그대로 나간다 —
    `ValueError` 가 아니므로 요청 검증에서 422 로 바뀌지 않는다.
    """
    from app.llm.catalog import get_model_catalog

    catalog = get_model_catalog()
    spec = catalog.get(model)
    if spec is None:
        known = ", ".join(catalog)
        raise ValueError(f"Model '{model}' is not in the catalog. Known: {known}.")
    return spec


def require_known_model(model: str) -> str:
    """catalog 에 있는 id 면 그대로 돌려준다. `enabled` 는 보지 않는다."""
    get_model_spec(model)
    return model


def require_enabled_model(model: str) -> str:
    """catalog 에 있고 관리자가 끄지 않은 id 면 그대로 돌려준다."""
    if not get_model_spec(model).enabled:
        raise ValueError(f"Model '{model}' is disabled by an administrator.")
    return model


# 모델 id. OpenRouter slug 이거나 `bedrock/` 뒤에 Bedrock inference profile ID 를 붙인
# 문자열이다. id 목록은 orchestration server 의 `llm_model` 테이블에 있으므로 enum 이
# 아니라, pydantic 이 값을 읽을 때 catalog 와 대조하는 문자열이다. 꺼진 row 도 통과한다.
LLMModel = Annotated[str, AfterValidator(require_known_model)]

# 사용자가 고른 모델을 싣는 요청 필드의 타입. 관리자가 끈 row 도 거절해서 요청이 422 로
# 끝난다.
EnabledLLMModel = Annotated[str, AfterValidator(require_enabled_model)]


DEFAULT_MODEL: str = "openai/gpt-5.6-luna"


def validate_reasoning(
    model: LLMModel, reasoning: ReasoningConfig | None
) -> ReasoningConfig | None:
    if reasoning is None:
        return None
    if reasoning.disabled:
        # 0 = 끔 — kind·예산 범위 검사의 대상이 아니다(요청에 추론이 아예 안 실린다).
        # 추론을 지원하지 않는 모델에도 유효하다: "끄겠다"는 어느 모델에서든 참이다.
        return reasoning

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
