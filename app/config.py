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
    knowledge_query_prompt_version: str | None = None

    # Embeddings. The slug is configuration rather than a constant because
    # swapping the model must not be a code change — but it is not a free swap
    # either: embedding_dimensions is pinned into Orchestration's vector(N)
    # column, so changing either value means a migration and a full re-index.
    #
    # openai/text-embedding-3-large scored best on the Korean retrieval set the
    # model was chosen with (ARTEL-184): 34/34 top-1, MRR 1.0. 1024 is a
    # Matryoshka truncation of its native 3072, which cost nothing measurable on
    # that set and keeps the vector under pgvector's 2000-dimension ceiling for
    # HNSW and IVFFlat indexes — 3072 would force halfvec on the storage side.
    embedding_model: str = "openai/text-embedding-3-large"
    embedding_dimensions: int = 1024
    # One request's worth of texts. Unbounded batches are how a backfill worker
    # turns a retry loop into a timeout and a bill.
    embedding_batch_limit: int = 128

    # Knowledge items per /knowledge-queries call. Each item is its own model
    # call, so this bounds fan-out, not payload size.
    knowledge_query_batch_limit: int = 32

    # /extract source fetch guards. allowed_hosts empty = no host restriction
    # (rely on the caller passing presigned URLs to the expected bucket).
    extract_max_bytes: int = 20 * 1024 * 1024
    extract_timeout_seconds: float = 30.0
    extract_allowed_hosts: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()
