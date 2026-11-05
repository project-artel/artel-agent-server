import pytest

from app.config import Settings, get_settings
from app.qa.run_config import resolve_run_config


def test_settings_can_load_from_env_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                'APP_NAME="Test Server"',
                'APP_VERSION="9.9.9"',
                'APP_ENV="test"',
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
    # 옛 이름으로 적힌 .env 가 그대로 읽힌다. 이 줄이 하위호환의 전부다 —
    # 배포가 `LLM_*` 로 옮기기 전에도 같은 이미지가 돌아야 한다.
    assert settings.llm_base_url == "https://openrouter.test/api/v1"
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
    # 비어 있는 것이 기본이고, 그것은 "런의 모델을 따른다"는 뜻이다. 한 슬러그로
    # 고정해 두면 런이 다른 provider 로 도는 순간 압축만 그쪽에 매여, credit 이 없을 때
    # 조용히 실패한다 — 실제로 그렇게 됐다(ARTEL-776).
    assert settings.qa_compaction_model is None


def test_a_summarizer_outside_the_catalog_is_refused_when_a_run_opens(monkeypatch) -> None:
    """Settings load before the catalog, which lives in the orchestration server, is
    reachable, so the check moved to the place that uses the value: opening a QA
    session. The error names the setting so the operator knows what to fix."""
    assert Settings(_env_file=None, qa_compaction_model="openai/not-a-model")

    monkeypatch.setattr(get_settings(), "qa_compaction_model", "openai/not-a-model")
    with pytest.raises(ValueError, match="qa_compaction_model .*not in the catalog"):
        resolve_run_config()


def test_the_new_name_wins_over_the_old_one(tmp_path) -> None:
    """둘 다 있으면 `LLM_*` 가 이긴다.

    옮기는 중인 배포는 두 이름이 한동안 함께 있는다. 그때 옛 값이 이기면 옮긴
    사람은 바꾼 것이 안 먹는 이유를 찾느라 시간을 쓴다.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENROUTER_BASE_URL=https://old.test/api/v1\n"
        "LLM_BASE_URL=https://new.test/v1\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.llm_base_url == "https://new.test/v1"


def test_provider_keys_in_the_environment_are_not_settings(tmp_path, monkeypatch) -> None:
    """키는 admin page 에서 넣고 orchestration server 가 저장한다. `.env` 와 shell 의 키는
    읽지 않는다 — shell 이 `OPENROUTER_API_KEY` 를 export 해도 이 server 에 닿지 않는다."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "shell-key")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEY=file-key\nBEDROCK_API_KEY=file-bedrock-key\n", encoding="utf-8"
    )

    dumped = Settings(_env_file=env_file).model_dump()

    assert "llm_api_key" not in dumped
    assert "bedrock_api_key" not in dumped
    assert not {"shell-key", "file-key", "file-bedrock-key"} & {
        value for value in dumped.values() if isinstance(value, str)
    }
