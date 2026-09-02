import sys
import types

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm.models import LLMModel


def test_settings_can_load_from_env_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                'APP_NAME="Test Server"',
                'APP_VERSION="9.9.9"',
                'APP_ENV="test"',
                'OPENROUTER_API_KEY="test-key"',
                'OPENROUTER_BASE_URL="https://openrouter.test/api/v1"',
                'OPENROUTER_SITE_URL="https://example.test"',
                'OPENROUTER_APP_TITLE="Test Title"',
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.app_name == "Test Server"
    assert settings.app_version == "9.9.9"
    assert settings.environment == "test"
    assert settings.openrouter_api_key == "test-key"
    assert settings.openrouter_base_url == "https://openrouter.test/api/v1"
    assert settings.openrouter_site_url == "https://example.test"
    assert settings.openrouter_app_title == "Test Title"


def test_prompt_versions_default_to_unset() -> None:
    """Unset means "the newest version on disk", which is what a fresh deploy wants."""
    settings = Settings(_env_file=None)

    assert settings.qa_prompt_version is None
    assert settings.scenario_prompt_version is None
    assert settings.game_context_prompt_version is None
    # Versioned apart from the run's own prompt: the summarizer is a different
    # call to a different model, and pinning one back must not pin the other.
    assert settings.qa_compaction_prompt_version is None


def test_prompt_versions_can_be_pinned_per_agent(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                'QA_PROMPT_VERSION="v2"',
                'SCENARIO_PROMPT_VERSION="v1"',
                'GAME_CONTEXT_PROMPT_VERSION="v3"',
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.qa_prompt_version == "v2"
    assert settings.scenario_prompt_version == "v1"
    assert settings.game_context_prompt_version == "v3"


def test_compaction_defaults_are_safe_to_deploy_with() -> None:
    """On by default, because a run that hits the provider's limit is worse than a
    run that compacts when it did not strictly need to."""
    settings = Settings(_env_file=None)

    assert settings.qa_compaction_enabled is True
    assert settings.qa_compaction_trigger_fraction == 0.9
    assert settings.qa_compaction_keep_messages == 20
    assert settings.qa_compaction_min_new_messages == 4
    assert settings.qa_compaction_trim_tokens == 8000
    assert settings.qa_compaction_model == LLMModel.gpt_5_6_luna.value


def test_a_summarizer_outside_the_catalog_is_refused_at_startup() -> None:
    """The slug cannot be typed as `LLMModel` here — importing `app.llm` from this
    module would import `chat_model`, which imports this module back. The validator
    is what keeps a typo from surviving until the first QA run compacts."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, qa_compaction_model="openai/not-a-model")


def test_llm_backend_defaults_to_openrouter() -> None:
    """A deploy that never sets `LLM_BACKEND` must keep calling OpenRouter — the
    Claude subscription path is local-only and opt-in."""
    settings = Settings(_env_file=None)

    assert settings.llm_backend == "openrouter"


def test_llm_backend_rejects_a_name_outside_the_two_accepted() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_backend="anthropic_direct")


def test_claude_subscription_fallback_model_defaults_to_sonnet_5() -> None:
    settings = Settings(_env_file=None)

    assert settings.claude_subscription_fallback_model == "claude-sonnet-5"


def test_build_chat_model_dispatches_to_the_claude_subscription_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`build_chat_model` must not need `claude-agent-sdk` installed to be tested —
    this stands a fake module in for `app.llm.claude_subscription` in `sys.modules`
    so the lazy `import` inside the function resolves to it, and asserts the call is
    routed there instead of building a `ChatOpenAI`."""
    from app.llm import chat_model as chat_model_module

    calls: list[tuple[LLMModel, object, bool]] = []

    class StubChatModel:
        pass

    def build_claude_subscription_chat_model(model, reasoning=None, cache_prompt=False):
        calls.append((model, reasoning, cache_prompt))
        return StubChatModel()

    stub_module = types.ModuleType("app.llm.claude_subscription")
    stub_module.build_claude_subscription_chat_model = build_claude_subscription_chat_model
    monkeypatch.setitem(sys.modules, "app.llm.claude_subscription", stub_module)
    monkeypatch.setattr(
        chat_model_module,
        "get_settings",
        lambda: Settings(_env_file=None, llm_backend="claude_subscription"),
    )

    chat_model_module.build_chat_model.cache_clear()
    try:
        result = chat_model_module.build_chat_model(LLMModel.gpt_5_6_luna)
    finally:
        chat_model_module.build_chat_model.cache_clear()

    assert isinstance(result, StubChatModel)
    assert calls == [(LLMModel.gpt_5_6_luna, None, False)]
