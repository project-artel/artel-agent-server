from functools import lru_cache
from typing import Any

from langchain_aws import ChatBedrockConverse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from app.config import get_settings
from app.llm.models import (
    LLMModel,
    LLMProvider,
    ReasoningConfig,
    get_model_spec,
    validate_reasoning,
)
from app.llm.usage import UsageCallback


# Not a knob, but recorded with every run: a comparison between two models is
# only a comparison if the sampling was the same, and a number that lives only
# in this call is one nobody can check afterwards.
TEMPERATURE = 0.2

# `LLMModel` 값이 이것으로 시작하면 Bedrock 으로 간다. OpenRouter slug 에는 이
# 접두가 없으므로 둘이 섞이지 않는다.
BEDROCK_PREFIX = "bedrock/"

# Converse API 가 캐시 경계를 읽는 표기. 이 block 까지가 되읽을 수 있는 prefix 다.
CACHE_POINT: dict[str, Any] = {"cachePoint": {"type": "default"}}


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
        last = messages[-1]
        content = last.content
        blocks: list[Any] = (
            [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
        )
        # 이미 경계가 있으면 더하지 않는다. Anthropic 은 요청당 4개까지만 받고,
        # 재시도로 같은 메시지가 두 번 지나가는 일이 있다.
        if any(isinstance(b, dict) and "cachePoint" in b for b in blocks):
            return messages
        return [*messages[:-1], last.model_copy(update={"content": [*blocks, CACHE_POINT]})]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return super()._generate(self._with_cache_point(messages), stop, run_manager, **kwargs)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        yield from super()._stream(self._with_cache_point(messages), stop, run_manager, **kwargs)


def _bedrock(model: LLMModel, cache_prompt: bool) -> ChatBedrockConverse:
    """Bedrock 경로. 인증은 표준 AWS 자격증명 사슬을 그대로 쓴다.

    `LLMModel` 값에서 `bedrock/` 접두만 떼면 inference profile ID 가 된다. 그
    문자열이 리전 접두와 판을 담고 있어 어느 요율로 청구되는지까지 정한다.
    """
    settings = get_settings()
    factory = _CachingChatBedrockConverse if cache_prompt else ChatBedrockConverse
    return factory(
        model=model.value.removeprefix(BEDROCK_PREFIX),
        region_name=settings.bedrock_region,
        temperature=TEMPERATURE,
        # 카탈로그 값을 그대로 넘긴다. 응답은 `bedrock/` 을 뗀 이름으로 돌아오고,
        # 그대로 두면 `provider` 가 모델 이름 전체가 된다.
        callbacks=[UsageCallback(slug=model.value)],
    )


@lru_cache
def build_chat_model(
    model: LLMModel,
    reasoning: ReasoningConfig | None = None,
    cache_prompt: bool = False,
) -> BaseChatModel:
    """Build a chat model.

    `bedrock/` 로 시작하는 값은 Bedrock 으로, 나머지는 OpenRouter 로 간다.
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
    if model.value.startswith(BEDROCK_PREFIX):
        # Bedrock 은 OpenAI 호환이 아니다(SigV4 + Converse). 아래 `extra_body` 는
        # 전부 OpenRouter 표기라 여기서 갈라지는 편이 옮기는 것보다 정직하다.
        return _bedrock(model, cache_prompt)

    settings = get_settings()
    headers: dict[str, str] = {}
    if settings.openrouter_site_url:
        headers["HTTP-Referer"] = settings.openrouter_site_url
    if settings.openrouter_app_title:
        headers["X-Title"] = settings.openrouter_app_title
    reasoning = validate_reasoning(model, reasoning)

    # OpenRouter prices the call itself and reports what it charged, but only
    # when asked — without usage.include the response carries token counts and
    # no `cost`. Merged rather than assigned: reasoning shares this field.
    extra_body: dict[str, object] = {"usage": {"include": True}}
    if reasoning is not None:
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

    return ChatOpenAI(
        model=model.value,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "missing",
        temperature=TEMPERATURE,
        default_headers=headers or None,
        extra_body=extra_body,
        # A call that never comes back has to end as a failure, not as a wait.
        # Why these values: see Settings.openrouter_timeout_seconds.
        timeout=settings.openrouter_timeout_seconds,
        max_retries=settings.openrouter_max_retries,
        callbacks=[UsageCallback()],
    )


def select_structured_method(model: LLMModel) -> str:
    """Strict json_schema for capable models, json_mode as the fallback."""
    return "json_schema" if get_model_spec(model).supports_strict_json else "json_mode"
