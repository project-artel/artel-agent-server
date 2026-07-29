from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Artel Agent Server"
    app_version: str = "0.1.0"
    environment: str = Field(default="local", validation_alias="APP_ENV")
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_site_url: str | None = None
    openrouter_app_title: str = "Artel Agent Server"

    # LangSmith tracing. Disabled unless both the flag and the key are set, so
    # a deploy without credentials degrades to "no traces" instead of failing.
    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_project: str | None = None

    # Applied to the root logger at startup; see app/logging_config.py.
    log_level: str = "INFO"

    redis_url: str = "redis://localhost:6379/0"
    session_ttl_seconds: int = 3600
    history_max_turns: int = 10

    # Default prompt version per agent, matching a directory under
    # app/prompts/<agent>/. Unset means the highest version present, so a new
    # version ships by being added; pin one here to hold an agent back or to
    # run a candidate. A value naming a directory that does not exist fails at
    # startup.
    qa_prompt_version: str | None = None
    scenario_prompt_version: str | None = None
    game_context_prompt_version: str | None = None

    # /extract source fetch guards. allowed_hosts empty = no host restriction
    # (rely on the caller passing presigned URLs to the expected bucket).
    extract_max_bytes: int = 20 * 1024 * 1024
    extract_timeout_seconds: float = 30.0
    extract_allowed_hosts: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()
