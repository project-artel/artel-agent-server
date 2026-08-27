from functools import lru_cache

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


class _PrefixCacheableChatOpenAI(ChatOpenAI):
    """`"content": null` 을 빈 문자열로 되돌린다. 접두 캐시가 거기서 멈추기 때문이다.

    langchain 이 일부러 넣는 동작이다.

        # langchain_openai/chat_models/base.py
        # If tool calls present, content null value should be None not empty string.
        if "function_call" in message_dict or "tool_calls" in message_dict:
            message_dict["content"] = message_dict["content"] or None

    그 `null` 하나가 OpenRouter 를 통한 접두 캐싱을 깬다. 캐시는 첫 도구 호출 직전에서
    멈추고, 그 뒤로 대화가 아무리 자라도 다시 안 잡힌다 — stage 런에서 컨텍스트가 25k 에서
    68k 로 가는 동안 `cache_read` 가 10,132 에 못 박혀 있었고, 매 턴 58k 를 새로 썼다.

    OpenRouter 에 직접 쏘아 갈랐다. 같은 모델·같은 메시지 목록으로 일곱 턴:

        content=null   4,510 고정
        content=""     4,724 → 6,389 (99%)

    두 갈래의 차이는 이 값 하나뿐이다. 모델에게는 같은 뜻이고, `""` 로 보낸 쪽도 정상
    응답을 돌려줬다.

    직렬화 뒤에 고치는 것이 요점이다. 미들웨어에서 메시지를 아무리 손봐도
    `_convert_message_to_dict` 가 그 뒤에서 다시 `None` 으로 만든다.

    langchain 이 이 동작을 고치면 이 클래스는 무해한 no-op 이 된다. 그때 지우면 된다.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # responses API 는 payload 모양이 달라 `messages` 가 없다. 건드리지 않는다.
        for message in payload.get("messages") or []:
            if message.get("role") == "assistant" and message.get("content") is None:
                message["content"] = ""

        return payload


@lru_cache
def build_chat_model(
    model: LLMModel,
    reasoning: ReasoningConfig | None = None,
    cache_prompt: bool = False,
) -> ChatOpenAI:
    """Build a chat model for an OpenRouter slug.

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

    return _PrefixCacheableChatOpenAI(
        model=model.value,
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key or "missing",
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
