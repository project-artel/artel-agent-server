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
