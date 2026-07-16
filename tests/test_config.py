from app.config import Settings


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
