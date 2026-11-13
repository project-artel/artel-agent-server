from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from app.config import get_settings
from app.llm.models import (
    LLMModel,
    LLMProvider,
    ReasoningConfig,
    get_model_spec,
    validate_reasoning,
)
from app.llm.api_key import (
    KeyRefreshOnUnauthorized,
    get_llm_api_key_provider,
    is_key_rejected_by_anthropic,
    is_unauthorized,
)
from app.llm.claude_api import ClaudeApiChat
from app.llm.credentials import LlmKeyService, bound_llm_key, session_chat_models
from app.llm.usage import KeySource, UsageCallback


# Not a knob, but recorded with every run: a comparison between two models is
# only a comparison if the sampling was the same, and a number that lives only
# in this call is one nobody can check afterwards.
TEMPERATURE = 0.2

# 예전 Bedrock 경로의 접두(ARTEL-1438). Bedrock 은 더 부르지 않는다. 카탈로그에 이 접두가 남아 있어도
# OpenRouter 로 잘못 보내지 않고 거절한다 — OpenRouter 에 `bedrock/...` 를 보내면 엉뚱한 404 가 난다.
RETIRED_BEDROCK_PREFIX = "bedrock/"


class RetiredModelError(ValueError):
    """더는 부를 수 없는 경로의 model 이다. 지금은 `bedrock/` 뿐이다."""

# 모델 id 가 이것으로 시작하면 Claude API 로 간다. 뒤는 Anthropic model id 다.
# `anthropic/...` 은 OpenRouter 라 둘이 섞이지 않는다.
CLAUDE_PREFIX = "claude/"

# 모델 id 가 이것으로 시작하면 OpenAI API 로 간다. 뒤는 OpenAI model id 다.
# `openai/...` 은 OpenRouter 라 둘이 섞이지 않는다.
# 공용 key 가 없는 경로라 세션에 묶인 사용자 key 로만 부른다.
GPT_PREFIX = "gpt/"

# OpenAI API 주소. 넘기지 않으면 langchain-openai 가 `OPENAI_BASE_URL` 같은 환경 변수를 읽는데,
# 셸에 남은 값이 사용자 key 를 다른 곳으로 보내지 않게 고정한다.
OPENAI_API_BASE_URL = "https://api.openai.com/v1"

# Claude API 주소. 넘기지 않으면 langchain-anthropic 이 `ANTHROPIC_BASE_URL` 같은 환경 변수를
# 읽는데, 셸에 남은 값이 호출을 다른 곳으로 보내지 않게 고정한다.
CLAUDE_API_BASE_URL = "https://api.anthropic.com"

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
    """`model` 의 경로에서 쓸 수 있는 사용자 key 의 서비스."""
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
        # 128,000 token 출력은 streaming 으로만 받을 수 있다. 답을 chunk 로 받는다 — 통째로
        # 받으면 다 쓸 때까지 연결이 조용해 긴 답부터 timeout 에 걸린다.
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

    Chat Completions 가 아니라 Responses API 를 쓴다. Chat Completions 는 한 답의 reasoning 을
    그 답과 함께 버려, QA loop 의 다음 turn 은 앞 turn 이 왜 그렇게 했는지 모른 채 다시 생각한다.
    Responses API 는 reasoning item 을 돌려주고 다음 요청에 다시 실을 수 있다.

    - `store=False`: OpenAI 에 응답을 남기지 않는다. 사용자 key 로 도는 런의 대화를 그쪽 서버에
      두지 않고, QA loop 가 앞 메시지를 접고 요약하므로 서버에 쌓인 대화를 이어 쓸 수도 없다.
    - `include=["reasoning.encrypted_content"]`: 저장하지 않는 대신 reasoning 을 암호문으로 받아
      AIMessage 에 둔다. langchain-openai 는 `store=False` 일 때 이 암호문이 있는 reasoning 만 다음
      요청에 다시 싣는다.

    추론 강도는 `reasoning={"effort": ...}` 로 보낸다. 값은 catalog 의 effort 그대로다 — OpenAI
    SDK 의 `ReasoningEffort` 는 `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` 라
    catalog 의 다섯 값이 모두 그 안에 있다. 끄라고 했으면 `none` 이다. 고르지 않았으면 보내지
    않고 model 의 기본값을 따른다.

    `temperature` 는 보내지 않는다. OpenAI 의 추론 model 은 1 이 아닌 값을 거절하고,
    langchain-openai 가 그 값을 빼 주는 것은 이름이 `gpt-5` 로 시작하는 model 뿐이다.
    OpenRouter 경로는 OpenRouter 가 지원하지 않는 값을 빼 주므로 `TEMPERATURE` 를 보낸다.

    prompt cache는 OpenAI가 관리하므로 `cache_prompt`를 받지 않는다. `usage_metadata`의
    `cache_read`와 `cache_creation`을 기록하고, 입력 합계에 포함된 쓰기 토큰은 분리한다.
    비용은 catalog의 Standard / 기본 30m cache 단가로 추정한다.
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
        use_responses_api=True,
        # catalog의 Standard 단가와 맞춘다. 생략하면 사용자 Project의 auto 설정을 따른다.
        service_tier="default",
        store=False,
        include=["reasoning.encrypted_content"],
        reasoning={"effort": effort} if effort is not None else None,
        # OpenRouter 경로와 같은 timeout 과 재시도 수를 쓴다. 둘 다 같은 OpenAI 호환
        # client 이고, 이 값을 고른 이유는 `Settings.openrouter_timeout_seconds` 에 있다.
        timeout=settings.openrouter_timeout_seconds,
        max_retries=settings.openrouter_max_retries,
        # 응답의 `model_name` 에는 `gpt/` 가 없다. catalog id 로 기록하려고 slug 를 넘긴다.
        # Responses의 input_tokens에는 cache write도 포함된다. 원장에서는 이를 분리해
        # 일반 입력과 캐시 쓰기 단가를 같은 토큰에 두 번 적용하지 않는다.
        callbacks=[
            UsageCallback(slug=model, input_includes_cache_write=True, key_source="user")
        ],
    )


def build_chat_model(
    model: LLMModel,
    reasoning: ReasoningConfig | None = None,
    cache_prompt: bool = False,
) -> BaseChatModel:
    """Build a chat model, or return the cached one built from the same inputs.

    `claude/` 모델은 지금의 Claude API key 까지 cache key 에 들어간다. `ChatAnthropic` 은
    만들어질 때 키를 SDK client 에 고정하므로, 키가 바뀌면 같은 client 를 다시 쓸 수 없다.
    키를 cache key 에 넣으면 관리자가 키를 바꾼 뒤 처음 부르는 호출이 새 client 를 받고,
    재시작이 필요 없다. 이미 client 를 받아 간 run 은 끝날 때까지 그 client 의 키를 쓴다.

    공용 OpenRouter 키는 요청마다 읽으므로(`api_key` 에 callable 을 넘긴다) 키가
    cache key 에 들어가지 않는다.

    세션에 사용자 key 가 묶여 있고(`app/llm/credentials.py`) 그 서비스가 이 모델의 경로와
    맞으면 공용 key 대신 그것을 쓴다. 경로와 서비스의 짝은 `key_service_of` 가 정한다. 사용자
    key 로 만든 model 은 프로세스 cache 가 아니라 그 세션의 cache 에 둔다. 다른 세션과 나눠 쓰지
    않고, 세션이 끝나면 key 와 함께 사라진다. 묶인 key 가 없으면 동작은 공용 key 만 있던 때와
    같다.

    `claude/` 모델인데 사용자 key 도 저장된 Claude API key 도 없으면
    `ClaudeApiKeyMissingError`, `gpt/` 모델인데 사용자 key 가 없으면 `GptApiKeyMissingError`,
    `bedrock/` 모델이면 `RetiredModelError` 다.
    """
    if model.startswith(RETIRED_BEDROCK_PREFIX):
        raise RetiredModelError(
            f"Model '{model}' is on the Bedrock route, which is no longer called. "
            "Pick a Claude API, OpenAI or OpenRouter model."
        )
    user_key = _bound_key_for(model)
    key_source: KeySource = "user" if user_key is not None else "platform"
    provider_key: str | None = user_key
    if model.startswith(CLAUDE_PREFIX):
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
    if key_source == "user":
        cache = session_chat_models()
        built_key = (model, reasoning, cache_prompt, provider_key)
        if cache is None:
            # `bind_llm_key` 가 key 와 cache 를 함께 묶으므로 오지 않는 자리다. 오더라도 프로세스
            # cache 에는 넣지 않는다.
            return _build_chat_model.__wrapped__(model, reasoning, cache_prompt, provider_key, key_source)
        if built_key not in cache:
            cache[built_key] = _build_chat_model.__wrapped__(
                model, reasoning, cache_prompt, provider_key, key_source
            )
        return cache[built_key]
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

    `claude/` 로 시작하는 값은 Claude API 로, `gpt/` 로 시작하는 값은 OpenAI API 로,
    나머지는 OpenRouter 로 간다. `provider_key` 는
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


# 명시적 cache 경계를 붙일 만큼 긴 고정 부분의 최소 글자 수. Claude 는 1024 token 아래의 prefix 를 cache 하지 않는다.
MIN_CACHED_PREFIX_CHARS = 4_000


def prompt_messages(
    model: str, system: str, user: str, stable_prefix: str = "", images: list[dict] | None = None
) -> list[BaseMessage]:
    """`[system, user]` 메시지. Claude API 경로면 매 호출 같은 앞부분 끝에 cache 경계를 둔다.

    `cache_prompt=True` 의 top-level `cache_control` 은 마지막 block 에 경계를 하나 둔다. 대화가 자라는 tool loop 에서는
    앞 호출의 끝이 다음 호출의 중간이 되어 읽히지만, 끝(현재 화면)이 매번 다른 단발 호출은 매번 쓰기만 하고 읽지 못한다
    (PE 엔진 런 154~160 의 Claude 호출 전부가 cache 읽기 0 이었다). 그래서 고정된 앞부분 끝에 경계를 둔다: system 끝, 그리고
    `user` 가 `stable_prefix` 로 시작하면 그 끝. 다른 provider 는 content block 의 `cache_control` 을 모르므로 그대로 보낸다.
    """
    # `images` 는 요청 끝에 붙는 그림 content block 이다(PE 엔진의 화면 한 장). cache 경계 뒤라 매 호출 새로 읽힌다.
    images = images or []
    if not model.startswith(CLAUDE_PREFIX):
        user_message = HumanMessage(content=[{"type": "text", "text": user}, *images]) if images else HumanMessage(user)
        return [SystemMessage(system), user_message]
    system_message = SystemMessage(content=[_cached_block(system)]) if len(system) >= MIN_CACHED_PREFIX_CHARS else SystemMessage(system)
    if stable_prefix and len(stable_prefix) >= MIN_CACHED_PREFIX_CHARS and user.startswith(stable_prefix):
        rest = user[len(stable_prefix):]
        blocks = [_cached_block(stable_prefix)] + ([{"type": "text", "text": rest}] if rest else [])
        return [system_message, HumanMessage(content=blocks + images)]
    if images:
        return [system_message, HumanMessage(content=[{"type": "text", "text": user}, *images])]
    return [system_message, HumanMessage(user)]


def _cached_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def structured(
    model: LLMModel, schema: Any, reasoning: ReasoningConfig | None = None
) -> Runnable:
    """`schema` 모양으로 답하는 chain. 부르는 쪽은 provider 를 몰라도 된다.

    OpenRouter 에서 strict schema 를 지원하면 `json_schema + strict`, 지원하지 않으면
    `json_mode` 를 쓴다. `claude/` 는 언제나 `strict` 없는 `json_schema` 다. ChatAnthropic 은 `strict` 인자를 쓰지 않는다. 이 분기를 호출부마다 복사하면
    provider 가 바뀔 때 일부 stage 만 조용히 실패하므로 이 한 곳이 결정한다(ARTEL-806).

    `reasoning` 은 `build_chat_model` 에 그대로 넘긴다. 결정 질문처럼 얕은 호출은
    `ReasoningConfig(max_tokens=0)` 으로 추론을 끈다.
    """
    chat = build_chat_model(model) if reasoning is None else build_chat_model(model, reasoning)
    if select_structured_method(model) != "json_schema":
        return chat.with_structured_output(schema, method="json_mode")
    if model.startswith(CLAUDE_PREFIX):
        return chat.with_structured_output(schema, method="json_schema")
    return chat.with_structured_output(schema, method="json_schema", strict=True)
