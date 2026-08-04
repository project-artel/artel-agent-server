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

    extra_body: dict[str, object] = {}
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
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key or "missing",
        temperature=0.2,
        default_headers=headers or None,
        extra_body=extra_body or None,
    )


def select_structured_method(model: LLMModel) -> str:
    """Strict json_schema for capable models, json_mode as the fallback."""
    return "json_schema" if get_model_spec(model).supports_strict_json else "json_mode"
