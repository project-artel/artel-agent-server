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
    model: LLMModel, reasoning: ReasoningConfig | None = None
) -> ChatOpenAI:
    """Build a chat model for an OpenRouter slug.

    ChatOpenAI targets any OpenAI-compatible endpoint; pointed at OpenRouter it
    serves every provider (OpenAI/Anthropic/Google/...) via the model slug, so a
    single client class covers the whole catalog.
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
    if get_model_spec(model).provider is LLMProvider.anthropic:
        # OpenRouter caches OpenAI and Google prompts by itself; Anthropic only
        # caches what the request marks. At the request root OpenRouter places the
        # breakpoint for us and moves it as the conversation grows, so nothing has
        # to reach into message content. Cache reads bill at ~0.1x, writes ~1.25x,
        # and the prefix has to clear the model's minimum (Sonnet 1024 tokens,
        # Opus 4096) or it silently does not cache at all.
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
