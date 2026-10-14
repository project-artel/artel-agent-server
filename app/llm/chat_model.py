from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from botocore.config import Config as BotocoreConfig
from langchain_anthropic import ChatAnthropic
from langchain_aws import ChatBedrockConverse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from app.config import get_settings
from app.llm.models import (
    LLMModel,
    LLMProvider,
    ModelSpec,
    ReasoningConfig,
    ReasoningKind,
    get_model_spec,
    validate_reasoning,
)
from app.llm.api_key import (
    KeyRefreshOnUnauthorized,
    get_llm_api_key_provider,
    is_key_rejected_by_anthropic,
    is_key_rejected_by_bedrock,
    is_unauthorized,
)
from app.llm.claude_api import ClaudeApiChat
from app.llm.credentials import LlmKeyService, bound_llm_key
from app.llm.usage import KeySource, UsageCallback


# Not a knob, but recorded with every run: a comparison between two models is
# only a comparison if the sampling was the same, and a number that lives only
# in this call is one nobody can check afterwards.
TEMPERATURE = 0.2

# 모델 id 가 이것으로 시작하면 Bedrock 으로 간다. OpenRouter slug 에는 이
# 접두가 없으므로 둘이 섞이지 않는다.
BEDROCK_PREFIX = "bedrock/"

# 모델 id 가 이것으로 시작하면 Claude API 로 간다. 뒤는 Anthropic model id 다.
# `anthropic/...` 은 OpenRouter, `bedrock/us.anthropic...` 은 Bedrock 이라 셋이 섞이지 않는다.
CLAUDE_PREFIX = "claude/"

# 모델 id 가 이것으로 시작하면 OpenAI API 로 간다. 뒤는 OpenAI model id 다.
# `openai/...` 은 OpenRouter, `bedrock/us.openai...` 은 Bedrock 이라 셋이 섞이지 않는다.
# 공용 key 가 없는 경로라 세션에 묶인 사용자 key 로만 부른다.
GPT_PREFIX = "gpt/"

# OpenAI API 주소. 넘기지 않으면 langchain-openai 가 `OPENAI_BASE_URL` 같은 환경 변수를 읽는데,
# 셸에 남은 값이 사용자 key 를 다른 곳으로 보내지 않게 고정한다.
OPENAI_API_BASE_URL = "https://api.openai.com/v1"

# Claude API 주소. 넘기지 않으면 langchain-anthropic 이 `ANTHROPIC_BASE_URL` 같은 환경 변수를
# 읽는데, 셸에 남은 값이 호출을 다른 곳으로 보내지 않게 고정한다.
CLAUDE_API_BASE_URL = "https://api.anthropic.com"

# Converse API 가 캐시 경계를 읽는 표기. 이 block 까지가 되읽을 수 있는 prefix 다.
CACHE_POINT: dict[str, Any] = {"cachePoint": {"type": "default"}}

# Anthropic 은 thinking 이 켜진 호출의 temperature 를 1 로만 받는다. 실측:
# `temperature may only be set to 1 when thinking is enabled`.
#
# 그래서 추론을 켜면 `TEMPERATURE` 를 못 쓴다. 그 값이 "두 모델의 비교가 비교이려면
# 샘플링이 같아야 한다"는 이유로 고정돼 있으므로, 바꾸는 것이 아니라 **추론을 켠 호출은
# 다른 샘플링이었다**는 사실로 남긴다. 비교할 때 이 축이 함께 움직인 것을 알아야 한다.
THINKING_TEMPERATURE = 1.0

# 추론 예산 위에 답이 설 자리. `max_tokens` 는 예산보다 커야 하고, 그 합이
# `ModelSpec.max_input_tokens` 이 창에서 빼 둔 출력 예약분과 같아야 한다 — 두 수가
# 어긋나면 압축이 도는 임계와 provider 가 거절하는 지점이 어긋난다.
#
# 4,096 은 스트리밍 주석 자신이 적어 둔 실측과 어긋나 있었다 — 시나리오 서른 스텝짜리
# 답 하나가 8,000 token 이다. 그 값으로 맞춘다.
OUTPUT_RESERVE = 8_192

# 예산은 상한이 아니라 **목표치**다(Anthropic 문서). 작은 과제에선 지켜지지만
# (실측: 목표 1,024 에 사용 949), 88건 저작처럼 무거운 과제에선 넘는다 — 실측
# (2026-09-08): 목표 4,096 에 8,188 token 을 생각하고도 문장 중간에서 잘렸다.
# `max_tokens` 가 목표 바로 위에 있으면 생각이 창을 다 먹고 답도 도구 호출도 없이
# 끝난다. 그래서 생각 자리는 목표의 두 배로 잡는다 — 그 실측이 정한 배수다.
THINKING_HEADROOM = 2

# effort 로 추론하는 Bedrock 모델(Haiku 5.5, GPT-6.1 Sol)이 허용하는 최대 출력이다.
# 추론 token 도 이 안에 들어가므로 낮게 고정하면 답을 쓰기 전에 `max_tokens` 로 끝날 수
# 있다. token 예산으로 추론하는 Haiku 4.5 는 이 값이 아니라 예산에서 `max_tokens` 를
# 계산한다.
BEDROCK_MAX_OUTPUT_TOKENS = 128_000

# Claude API 의 최대 출력. 이 값은 streaming 으로만 받을 수 있다. catalog row 의
# `max_input_tokens` 872,000 은 1M context 에서 이 값을 뺀 것이다.
CLAUDE_MAX_OUTPUT_TOKENS = 128_000


class ClaudeApiKeyMissingError(RuntimeError):
    """`claude/` 모델을 부르려는데 저장된 Claude API key 가 없다.

    API 를 부르기 전에 던진다. key 없이 부르면 SDK 가 `ANTHROPIC_API_KEY` 환경 변수를
    찾거나 인증 실패로 끝나는데, 둘 다 key 를 넣을 곳이 admin page 라는 것을 알려 주지
    않는다.
    """


class GptApiKeyMissingError(RuntimeError):
    """`gpt/` 모델을 부르려는데 세션에 묶인 OpenAI key 가 없다.

    `gpt/` 에는 공용 key 가 없다. 사용자가 등록한 key 로 도는 QA 런만 이 경로를 쓴다.
    """


def key_service_of(model: str) -> LlmKeyService | None:
    """`model` 의 경로에서 쓸 수 있는 사용자 key 의 서비스. `bedrock/` 은 공용 key 뿐이라 None 이다."""
    if model.startswith(BEDROCK_PREFIX):
        return None
    if model.startswith(CLAUDE_PREFIX):
        return LlmKeyService.anthropic
    if model.startswith(GPT_PREFIX):
        return LlmKeyService.openai
    return LlmKeyService.openrouter


def _bound_key_for(model: str) -> str | None:
    """세션에 묶인 사용자 key 가 이 모델의 경로와 맞으면 그 key, 아니면 None 이다.

    맞지 않는 key 는 쓰지 않는다. `claude/` 런이라도 compaction 모델이 OpenRouter 에 있으면
    그 호출은 공용 OpenRouter key 로 간다 — Claude key 를 OpenRouter 에 보내면 401 이다.
    """
    bound = bound_llm_key()
    if bound is None or bound.service is not key_service_of(model):
        return None
    return bound.api_key


def _is_transient(message: BaseMessage) -> bool:
    """이번 요청에만 실렸고 저장되지 않는 메시지인가.

    `app/agents/qa/vision.py` 의 `TRANSIENT_MESSAGE_KEY` 를 읽는다. 그 이름을 여기서
    import 하지 않는 것은 `app/llm` 이 `app/agents` 를 모르게 두기 위해서다 —
    의존은 지금 한 방향이고, 캐시 경계를 찍자고 그것을 뒤집을 이유가 없다.
    """
    return bool((getattr(message, "additional_kwargs", None) or {}).get("artel_transient"))


class _CachingChatBedrockConverse(ChatBedrockConverse):
    """프롬프트 끝에 캐시 경계를 찍고 부른다.

    OpenRouter 는 요청 루트의 `cache_control` 하나를 받아 **자기가** 프롬프트 끝에
    경계를 놓아 주었다. Bedrock 에는 그런 대리인이 없고 경계가 content block 으로
    들어가야 하는데, 그 block 은 모델을 만드는 자리가 아니라 **부르는 자리**에서만
    끼울 수 있다. 그래서 클래스로 감싼다 — `create_agent` 든
    `with_structured_output` 이든 결국 여기를 지나므로, 끼우는 자리가 하나로 모인다.

    끝에 찍는 것은 대화가 자라는 호출을 위해서다. 다음 턴은 이번 프롬프트 전체를
    prefix 로 갖고 시작하므로 그것을 통째로 되읽는다. 실측에서 8,014 token 이
    두 번째 호출에 `cache_read` 로 돌아왔다.

    최소 길이(1,024 token)를 못 넘는 프롬프트는 경계가 무시된다. 에러가 아니라
    조용히 안 걸리는 것이라, 짧은 호출에 이 클래스를 써도 손해가 없다.
    """

    def _with_cache_point(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        if not messages:
            return messages
        # 뒤에서부터 "이번 요청에만 실린" 메시지를 건너뛰고 경계를 찍는다.
        #
        # 경계 뒤는 캐시에 안 들어간다. 매 턴 바뀌는 것을 경계 **앞**에 두면 그 뒤로
        # 되읽을 것이 사라지므로, 화면 그림처럼 한 번 실렸다 버려지는 메시지는 경계
        # 밖에 있어야 한다. 그러면 앞의 대화는 그대로 되읽히고 그림 한 장만 정가다.
        cut = len(messages)
        while cut > 0 and _is_transient(messages[cut - 1]):
            cut -= 1
        if cut == 0:
            return messages
        head, tail = messages[:cut], messages[cut:]
        last = head[-1]
        content = last.content
        blocks: list[Any] = (
            [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
        )
        # 이미 경계가 있으면 더하지 않는다. Anthropic 은 요청당 4개까지만 받고,
        # 재시도로 같은 메시지가 두 번 지나가는 일이 있다.
        if any(isinstance(b, dict) and "cachePoint" in b for b in blocks):
            return messages
        marked = last.model_copy(update={"content": [*blocks, CACHE_POINT]})
        return [*head[:-1], marked, *tail]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return super()._generate(self._with_cache_point(messages), stop, run_manager, **kwargs)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        yield from super()._stream(self._with_cache_point(messages), stop, run_manager, **kwargs)


@dataclass(frozen=True)
class _BedrockRequest:
    """Converse 요청에서 추론 설정에 따라 달라지는 세 값."""

    # `additionalModelRequestFields`. 실을 것이 없으면 None 이다.
    fields: dict[str, Any] | None
    # None 이면 sampling parameter 를 보내지 않는다.
    temperature: float | None
    # None 이면 model 의 기본 최대 출력을 쓴다.
    max_tokens: int | None


def _bedrock_request(spec: ModelSpec, reasoning: ReasoningConfig | None) -> _BedrockRequest:
    """추론을 싣는 표기가 provider 와 `ModelSpec.reasoning` 에 따라 셋으로 나뉜다.

    `anthropic` 이고 `max_tokens` 이면(Haiku 4.5) `thinking.budget_tokens` 로 켠다.
    고르지 않았으면 catalog 의 `reasoning_default_tokens` 로 켜고, 0 이면 끈다.

    `anthropic` 이고 `effort` 이면(Haiku 5.5) adaptive `thinking` 과
    `output_config.effort` 를 싣는다. Haiku 5.5 는 고정 `budget_tokens` 를 거절한다.
    아무 값도 고르지 않으면 provider 기본값인 medium 으로 생각한다.

    `openai` 는 `reasoning_effort` 하나를 받고 `thinking` 을 모른다. 추론을 끄는 값이
    없으므로, 끄라고 했거나 고르지 않았으면 아무것도 싣지 않는다 — 그때는 provider
    기본값으로 추론한다.
    """
    if spec.provider is LLMProvider.openai:
        if reasoning is None or reasoning.disabled or reasoning.effort is None:
            return _BedrockRequest(None, None, BEDROCK_MAX_OUTPUT_TOKENS)
        return _BedrockRequest(
            {"reasoning_effort": reasoning.effort.value}, None, BEDROCK_MAX_OUTPUT_TOKENS
        )

    if spec.reasoning is ReasoningKind.max_tokens:
        # 고르지 않았으면 카탈로그의 기본 예산으로 켠다. Bedrock 은 provider 기본값이
        # 없어서, 안 정하면 추론이 아예 일어나지 않는다.
        budget = (
            reasoning.max_tokens
            if reasoning is not None and reasoning.max_tokens is not None
            else spec.reasoning_default_tokens
        )
        # 0 은 "끈다" — None(미지정→기본 예산)과 다르다. `ReasoningConfig.disabled` 참조.
        if not budget:
            return _BedrockRequest(None, TEMPERATURE, None)
        return _BedrockRequest(
            {"thinking": {"type": "enabled", "budget_tokens": budget}},
            THINKING_TEMPERATURE,
            # `max_tokens` 가 예산보다 커야 한다(실측: `max_tokens must be greater than
            # thinking.budget_tokens`). 예산에 답할 자리를 더해 잡는다 — 이 값이 곧
            # `max_input_tokens` 이 빼 둔 출력 예약분이다.
            budget * THINKING_HEADROOM + OUTPUT_RESERVE,
        )

    # Haiku 5.5 는 sampling parameter 를 보내면 400 을 돌려준다.
    if reasoning is not None and reasoning.disabled:
        fields: dict[str, Any] | None = {"thinking": {"type": "disabled"}}
    elif reasoning is not None and reasoning.effort is not None:
        fields = {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": reasoning.effort.value},
        }
    else:
        fields = None
    return _BedrockRequest(fields, None, BEDROCK_MAX_OUTPUT_TOKENS)


@dataclass(frozen=True)
class _ClaudeRequest:
    """Claude API 요청에서 추론 설정에 따라 달라지는 두 값. None 이면 보내지 않는다."""

    thinking: dict[str, Any] | None
    output_config: dict[str, Any] | None


# `ReasoningConfig.disabled` 를 model 마다 어떻게 보내는가. Anthropic model id 로 찾는다.
#
# Opus 5.5 와 Fable 5.1 은 `{"type": "disabled"}` 를 거절하고 thinking 을 끌 방법이 없다.
# 가장 적게 생각하는 effort `low` 를 보낸다. Sonnet 5.5 도 `disabled` 를 거절하고
# `between_tools` 로 끈다(effort high 이하, 다른 thinking 필드 없이). Haiku 5.5 는
# `disabled` 를 받는다(effort high 이하). 네 model 모두 `budget_tokens` 는 400 이다.
_CLAUDE_THINKING_OFF: dict[str, _ClaudeRequest] = {
    "claude-opus-5-5": _ClaudeRequest(None, {"effort": "low"}),
    "claude-fable-5-1": _ClaudeRequest(None, {"effort": "low"}),
    "claude-sonnet-5-5": _ClaudeRequest({"type": "between_tools"}, None),
    "claude-haiku-5-5": _ClaudeRequest({"type": "disabled"}, None),
}


def _claude_request(model_id: str, reasoning: ReasoningConfig | None) -> _ClaudeRequest:
    """effort 를 고르면 adaptive `thinking` 과 `output_config.effort` 를 보낸다.

    고르지 않았으면 둘 다 보내지 않는다. 그때는 model 의 기본 동작을 따른다.
    끄라고 했으면 `_CLAUDE_THINKING_OFF` 를 따른다. 표에 없는 model 은 아무것도 보내지
    않는다 — 끄는 표기가 model 마다 달라서, 확인하지 않은 표기를 보내면 400 이 될 수 있다.
    """
    if reasoning is None:
        return _ClaudeRequest(None, None)
    if reasoning.disabled:
        return _CLAUDE_THINKING_OFF.get(model_id, _ClaudeRequest(None, None))
    if reasoning.effort is None:
        return _ClaudeRequest(None, None)
    return _ClaudeRequest({"type": "adaptive"}, {"effort": reasoning.effort.value})


def _claude(
    model: LLMModel,
    reasoning: ReasoningConfig | None,
    cache_prompt: bool,
    anthropic_api_key: str,
    key_source: KeySource,
) -> ChatAnthropic:
    """Claude API 경로. key 는 세션에 묶인 사용자 key, 없으면 관리자가 admin page 에 넣은 값이다.

    `temperature`, `top_p`, `top_k` 는 보내지 않는다. Opus 5.5 와 Fable 5.1 은 sampling
    parameter 를 거절하고, Sonnet 5.5 와 Haiku 5.5 는 기본값이 아닌 값을 거절한다.

    `ClaudeApiChat` 은 강제 `tool_choice` 를 `auto` 로 낮추고, 앞의 메시지가 바뀌어 무효가
    된 thinking block 을 요청에서 뺀다. 이유는 `app/llm/claude_api.py` 에 있다.
    """
    settings = get_settings()
    model_id = model.removeprefix(CLAUDE_PREFIX)
    request = _claude_request(model_id, reasoning)
    model_kwargs: dict[str, Any] = {}
    if cache_prompt:
        # Claude API 는 top-level `cache_control` 을 받아 마지막 cache 가능한 block 에
        # 경계를 놓는다. OpenRouter 의 `extra_body["cache_control"]` 과 같은 동작이다.
        model_kwargs["cache_control"] = {"type": "ephemeral"}

    return ClaudeApiChat(
        model=model_id,
        # key 를 넘기면 SDK 는 `ANTHROPIC_API_KEY` 와 `ANTHROPIC_AUTH_TOKEN` 을 읽지 않는다.
        api_key=anthropic_api_key,
        base_url=CLAUDE_API_BASE_URL,
        max_tokens=CLAUDE_MAX_OUTPUT_TOKENS,
        thinking=request.thinking,
        output_config=request.output_config,
        model_kwargs=model_kwargs,
        # 128,000 token 출력은 streaming 으로만 받을 수 있다. Bedrock 과 같은 이유로 답을
        # chunk 로 받는다 — 통째로 받으면 긴 답부터 timeout 에 걸린다.
        streaming=True,
        timeout=settings.anthropic_timeout_seconds,
        max_retries=settings.anthropic_max_retries,
        # 응답의 `model_name` 에는 `claude/` 가 없다. catalog id 로 기록하려고 slug 를 넘긴다.
        # langchain-anthropic 의 `input_tokens` 는 cache write 를 포함한다.
        callbacks=[
            UsageCallback(
                slug=model, input_includes_cache_write=True, key_source=key_source
            ),
            *_platform_key_refresh(key_source, rejected=is_key_rejected_by_anthropic),
        ],
    )


def _platform_key_refresh(
    key_source: KeySource, rejected: Callable[[BaseException], bool]
) -> list[KeyRefreshOnUnauthorized]:
    """공용 key 로 만든 client 에만 cache 된 공용 key 를 버리는 callback 을 단다.

    사용자 key 가 거절됐을 때 공용 key 를 다시 읽어 봐야 그 client 의 key 는 바뀌지 않는다.
    """
    if key_source == "user":
        return []
    return [KeyRefreshOnUnauthorized(get_llm_api_key_provider(), rejected=rejected)]


def _gpt(
    model: LLMModel,
    reasoning: ReasoningConfig | None,
    openai_api_key: str,
) -> ChatOpenAI:
    """OpenAI API 경로. key 는 세션에 묶인 사용자 key 뿐이다.

    Chat Completions API 를 쓴다. 추론은 `reasoning_effort` 하나로 싣고, 값은 catalog 의 effort
    를 그대로 보낸다 — OpenAI SDK 의 `ReasoningEffort` 는 `none`, `minimal`, `low`, `medium`,
    `high`, `xhigh`, `max` 라 catalog 의 다섯 값이 모두 그 안에 있다. 끄라고 했으면 `none`
    이다. 고르지 않았으면 보내지 않고 model 의 기본값을 따른다.

    `temperature` 는 보내지 않는다. OpenAI 의 추론 model 은 1 이 아닌 값을 거절하고,
    langchain-openai 가 그 값을 빼 주는 것은 이름이 `gpt-5` 로 시작하는 model 뿐이다.
    OpenRouter 경로는 OpenRouter 가 지원하지 않는 값을 빼 주므로 `TEMPERATURE` 를 보낸다.
    Bedrock 의 `openai` 경로와 같은 선택이다.

    prompt cache 는 OpenAI 가 스스로 하므로 `cache_prompt` 를 받지 않는다. 읽은 cache 는
    `usage_metadata` 의 `cache_read` 로 온다.
    """
    settings = get_settings()
    if reasoning is None:
        effort: str | None = None
    elif reasoning.disabled:
        effort = "none"
    else:
        effort = reasoning.effort.value if reasoning.effort is not None else None

    return ChatOpenAI(
        model=model.removeprefix(GPT_PREFIX),
        base_url=OPENAI_API_BASE_URL,
        api_key=openai_api_key,
        reasoning_effort=effort,
        # OpenRouter 경로와 같은 timeout 과 재시도 수를 쓴다. 둘 다 같은 OpenAI 호환
        # client 이고, 이 값을 고른 이유는 `Settings.openrouter_timeout_seconds` 에 있다.
        timeout=settings.openrouter_timeout_seconds,
        max_retries=settings.openrouter_max_retries,
        # 응답의 `model_name` 에는 `gpt/` 가 없다. catalog id 로 기록하려고 slug 를 넘긴다.
        callbacks=[UsageCallback(slug=model, key_source="user")],
    )


def _bedrock(
    model: LLMModel,
    reasoning: ReasoningConfig | None,
    cache_prompt: bool,
    bedrock_api_key: str | None,
) -> ChatBedrockConverse:
    """Bedrock 경로.

    인증은 `bedrock_api_key` 가 있으면 그것으로, None 이면 표준 AWS 자격증명 사슬(IAM
    role 등)로 간다. 키는 관리자가 admin page 에 넣고 orchestration server 가 암호화해
    저장한 값이다. `.env` 에서는 읽지 않는다.

    모델 id 에서 `bedrock/` 접두만 떼면 inference profile ID 가 된다. 그 문자열이 리전
    접두와 판을 담고 있어 어느 요율로 청구되는지까지 정한다.

    추론 설정, temperature, `max_tokens` 는 `_bedrock_request` 가 정한다. `cachePoint` 는
    `anthropic` 모델에만 찍는다. OpenAI 모델이 Converse 에서 `cachePoint` block 을
    받는지는 확인하지 못했다.
    """
    settings = get_settings()
    spec = get_model_spec(model)
    caches_prompt = cache_prompt and spec.provider is LLMProvider.anthropic
    factory = _CachingChatBedrockConverse if caches_prompt else ChatBedrockConverse
    request = _bedrock_request(spec, reasoning)

    return factory(
        model=model.removeprefix(BEDROCK_PREFIX),
        region_name=settings.bedrock_region,
        # admin page 에 저장된 키만 넘긴다. None 이면 표준 AWS 자격증명 사슬이 답한다.
        # `AWS_BEARER_TOKEN_BEDROCK` 은 그 사슬의 일부라, langchain-aws 1.8 은 None 을
        # 받아도 그 환경 변수를 읽는다. 예전 설정인 `BEDROCK_API_KEY` 는 읽지 않는다.
        bedrock_api_key=bedrock_api_key,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        additional_model_request_fields=request.fields,
        # **답을 토막으로 받는다.** 통째로 받으면 다 쓸 때까지 연결에 아무것도 안 오고, 그
        # 조용한 시간이 곧 답의 길이다 — 실측으로 출력 token 당 5.3ms 라 8,000 token 짜리
        # 답 하나가 42초다. 시나리오 하나가 서른 스텝을 넘는 것은 정상이므로, 기다림 한도를
        # 어디에 두든 통째로 받는 한 긴 답부터 죽는다. 같은 45초 한도에서 통째로 받으면
        # 실패하고 토막으로 받으면 통과했다.
        streaming=True,
        # 안 돌아오는 호출을 끊는다. 값을 고른 이유는 `Settings.bedrock_timeout_seconds` 에 있다.
        config=BotocoreConfig(
            read_timeout=settings.bedrock_timeout_seconds,
            connect_timeout=settings.bedrock_timeout_seconds,
            retries={"max_attempts": settings.bedrock_max_attempts, "mode": "standard"},
        ),
        # 카탈로그 값을 그대로 넘긴다. 응답은 `bedrock/` 을 뗀 이름으로 돌아오고,
        # 그대로 두면 `provider` 가 모델 이름 전체가 된다.
        #
        # 키가 거절되면(401, 403) cache 된 키를 버린다. 다음 `build_chat_model` 이 새 키를
        # 읽어 새 client 를 만든다.
        callbacks=[
            UsageCallback(slug=model),
            KeyRefreshOnUnauthorized(
                get_llm_api_key_provider(), rejected=is_key_rejected_by_bedrock
            ),
        ],
    )


def build_chat_model(
    model: LLMModel,
    reasoning: ReasoningConfig | None = None,
    cache_prompt: bool = False,
) -> BaseChatModel:
    """Build a chat model, or return the cached one built from the same inputs.

    Bedrock 모델은 지금의 Bedrock key 까지, `claude/` 모델은 지금의 Claude API key 까지
    cache key 에 들어간다. `ChatBedrockConverse` 는 만들어질 때 키를 한 번 받아 boto3
    client 에 고정하므로, 키가 바뀌면 같은 client 를 다시 쓸 수 없다. 키를 cache key 에 넣으면 관리자가 키를 바꾼 뒤 처음 부르는 호출이 새
    client 를 받고, 재시작이 필요 없다. 이미 client 를 받아 간 run 은 끝날 때까지 그
    client 의 키를 쓴다.

    `ChatAnthropic` 도 만들어질 때 키를 SDK client 에 고정하므로 같은 방법을 쓴다.

    공용 OpenRouter 키는 요청마다 읽으므로(`api_key` 에 callable 을 넘긴다) 키가
    cache key 에 들어가지 않는다.

    세션에 사용자 key 가 묶여 있고(`app/llm/credentials.py`) 그 서비스가 이 모델의 경로와
    맞으면 공용 key 대신 그것을 쓴다. 경로와 서비스의 짝은 `key_service_of` 가 정한다. 사용자
    key 는 언제나 cache key 에 들어간다 — 두 사용자가 client 하나를 나눠 쓰면 한 사람의 key
    로 다른 사람의 호출이 나간다. 묶인 key 가 없으면 동작은 공용 key 만 있던 때와 같다.

    `claude/` 모델인데 사용자 key 도 저장된 Claude API key 도 없으면
    `ClaudeApiKeyMissingError`, `gpt/` 모델인데 사용자 key 가 없으면 `GptApiKeyMissingError`
    다.
    """
    user_key = _bound_key_for(model)
    key_source: KeySource = "user" if user_key is not None else "platform"
    provider_key: str | None = user_key
    if model.startswith(BEDROCK_PREFIX):
        provider_key = get_llm_api_key_provider().get_bedrock_key()
    elif model.startswith(CLAUDE_PREFIX):
        if provider_key is None:
            provider_key = get_llm_api_key_provider().get_anthropic_key()
        if provider_key is None:
            raise ClaudeApiKeyMissingError(
                f"Model '{model}' needs a Claude API key, and none is stored. "
                "An administrator enters it on the admin page."
            )
    elif model.startswith(GPT_PREFIX):
        if provider_key is None:
            raise GptApiKeyMissingError(
                f"Model '{model}' is called with the user's own OpenAI API key, and no "
                "llm_key with service 'openai' is bound to this session."
            )
    return _build_chat_model(model, reasoning, cache_prompt, provider_key, key_source)


@lru_cache
def _build_chat_model(
    model: LLMModel,
    reasoning: ReasoningConfig | None,
    cache_prompt: bool,
    provider_key: str | None,
    key_source: KeySource,
) -> BaseChatModel:
    """Build a chat model.

    `bedrock/` 로 시작하는 값은 Bedrock 으로, `claude/` 로 시작하는 값은 Claude API 로,
    `gpt/` 로 시작하는 값은 OpenAI API 로, 나머지는 OpenRouter 로 간다. `provider_key` 는
    그 경로에서 쓰는 key 다. OpenRouter 경로에서는 사용자 key 일 때만 값이 있고, None 이면
    공용 key 를 요청마다 읽는다.
    OpenRouter 경로의 동작은 그대로다 — credit 이 없으면 그쪽이 실패하는 것도
    포함해서, 이 변경이 손대지 않는다.

    ChatOpenAI targets any OpenAI-compatible endpoint; pointed at OpenRouter it
    serves every provider (OpenAI/Anthropic/Google/...) via the model slug, so a
    single client class covers the whole catalog.

    `cache_prompt` asks for Anthropic prompt caching. It is off by default and
    opt-in per call site because caching pays off for a call *pattern*, not for a
    model: only a caller that sends the same prefix again can read back what the
    first call wrote. An agent loop does (each turn resends the whole
    conversation); a one-shot extraction over a fresh document does not, and
    would pay the write premium on every request forever.
    """
    # 갈라지기 전에 검증한다. 분기 뒤에 두면 한쪽만 검증을 받고, 못 하는 추론을 요청한
    # 호출이 그 경로에서만 조용히 통과한다.
    reasoning = validate_reasoning(model, reasoning)

    if model.startswith(BEDROCK_PREFIX):
        # Bedrock 은 OpenAI 호환이 아니다(SigV4 + Converse). 아래 `extra_body` 는
        # 전부 OpenRouter 표기라 여기서 갈라지는 편이 옮기는 것보다 정직하다.
        return _bedrock(model, reasoning, cache_prompt, provider_key)
    if model.startswith(CLAUDE_PREFIX):
        # `build_chat_model` 이 key 가 없으면 여기까지 오지 않게 한다.
        assert provider_key is not None
        return _claude(model, reasoning, cache_prompt, provider_key, key_source)
    if model.startswith(GPT_PREFIX):
        # `build_chat_model` 이 key 가 없으면 여기까지 오지 않게 한다.
        assert provider_key is not None
        return _gpt(model, reasoning, provider_key)

    settings = get_settings()
    headers: dict[str, str] = {}
    if settings.openrouter_site_url:
        headers["HTTP-Referer"] = settings.openrouter_site_url
    if settings.openrouter_app_title:
        headers["X-Title"] = settings.openrouter_app_title

    # OpenRouter prices the call itself and reports what it charged, but only
    # when asked — without usage.include the response carries token counts and
    # no `cost`. Merged rather than assigned: reasoning shares this field.
    extra_body: dict[str, object] = {"usage": {"include": True}}
    # disabled(max_tokens=0)면 아예 안 싣는다 — OpenRouter 는 안 실으면 안 켠다.
    if reasoning is not None and not reasoning.disabled:
        extra_body["reasoning"] = reasoning.as_openrouter()
    if cache_prompt and get_model_spec(model).provider is LLMProvider.anthropic:
        # OpenRouter caches OpenAI and Google prompts by itself; Anthropic only
        # caches what the request marks. At the request root OpenRouter places the
        # breakpoint itself, at the end of the rendered prompt, and a later call
        # reads back whatever prefix it still shares.
        #
        # That placement is why this is opt-in. Measured against Sonnet 5 on a
        # ~3.5k-token prompt: a call that appends to the previous one reads the
        # whole prefix back and costs $0.0007, while a call that *replaces* the
        # last message shares nothing past the breakpoint, reads nothing, rewrites
        # the cache, and costs $0.0088 — worse than the $0.0070 it would have cost
        # with no caching at all. Growing conversations win; varying one-shots lose.
        #
        # The prefix also has to clear the model's minimum (Sonnet 1024 tokens,
        # Opus 4096) or nothing caches, silently and at no extra charge.
        extra_body["cache_control"] = {"type": "ephemeral"}

    # 공용 key 는 문자열이 아니라 callable 로 넘긴다. 이 client 는 프로세스 동안 cache 되고,
    # 관리자가 key 를 바꾸면 재시작 없이 바뀌어야 한다. client 가 요청마다 부른다.
    # 사용자 key 는 그 값으로 만든 client 에 고정한다 — cache key 에 key 가 들어 있다.
    api_key = (
        provider_key
        if provider_key is not None
        else get_llm_api_key_provider().get_openrouter_key_or_placeholder
    )

    return ChatOpenAI(
        model=model,
        base_url=settings.llm_base_url,
        api_key=api_key,
        temperature=TEMPERATURE,
        default_headers=headers or None,
        extra_body=extra_body,
        # A call that never comes back has to end as a failure, not as a wait.
        # Why these values: see Settings.openrouter_timeout_seconds.
        timeout=settings.openrouter_timeout_seconds,
        max_retries=settings.openrouter_max_retries,
        callbacks=[
            UsageCallback(key_source=key_source),
            *_platform_key_refresh(key_source, rejected=is_unauthorized),
        ],
    )


# Tests and scripts clear the client cache through the public name.
build_chat_model.cache_clear = _build_chat_model.cache_clear  # type: ignore[attr-defined]


def select_structured_method(model: LLMModel) -> str:
    """Strict json_schema for capable models, json_mode as the fallback.

    `claude/` 모델은 catalog 값과 상관없이 `json_schema` 다. ChatAnthropic 에는
    `json_mode` 가 없고, 나머지 방식인 `function_calling` 은 tool 을 강제하는데 Opus 5.5,
    Sonnet 5.5, Fable 5.1 이 그것을 400 으로 거절한다. `json_schema` 는
    `output_config.format` 으로 간다.
    """
    if model.startswith(CLAUDE_PREFIX):
        return "json_schema"
    return "json_schema" if get_model_spec(model).supports_strict_json else "json_mode"


def structured(model: LLMModel, schema: Any) -> Runnable:
    """`schema` 모양으로 답하는 chain. 부르는 쪽은 provider 를 몰라도 된다.

    OpenRouter 에서 strict schema 를 지원하면 `json_schema + strict`, 지원하지 않으면
    `json_mode` 를 쓴다. Bedrock model card 가 structured outputs 를 지원하지 않는다고
    하면 `function_calling` 으로 내린다. `claude/` 는 언제나 `strict` 없는 `json_schema`
    다. ChatAnthropic 은 `strict` 인자를 쓰지 않는다. 이 분기를 호출부마다 복사하면
    provider 가 바뀔 때 일부 stage 만 조용히 실패하므로 이 한 곳이 결정한다(ARTEL-806).
    """
    chat = build_chat_model(model)
    if select_structured_method(model) != "json_schema" and model.startswith(
        BEDROCK_PREFIX
    ):
        return chat.with_structured_output(schema, method="function_calling")
    if select_structured_method(model) != "json_schema":
        return chat.with_structured_output(schema, method="json_mode")
    if model.startswith((BEDROCK_PREFIX, CLAUDE_PREFIX)):
        return chat.with_structured_output(schema, method="json_schema")
    return chat.with_structured_output(schema, method="json_schema", strict=True)
