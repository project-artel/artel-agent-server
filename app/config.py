from functools import lru_cache

from pydantic import Field, field_validator
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

    # What build this is, injected by the image build (see Dockerfile/Jenkinsfile).
    # These are what makes a past run reproducible: the agent's structure is code,
    # so it is identified by its commit and redeployed from its image rather than
    # kept as a parallel copy in the tree. Unset in local runs, and reported as
    # null rather than guessed — a wrong sha is worse than a missing one.
    git_sha: str | None = None
    image_tag: str | None = None

    redis_url: str = "redis://localhost:6379/0"
    session_ttl_seconds: int = 3600
    history_max_turns: int = 100_000

    # Default prompt version per agent, matching a directory under
    # app/prompts/<agent>/. Unset means the highest version present, so a new
    # version ships by being added; pin one here to hold an agent back or to
    # run a candidate. A value naming a directory that does not exist fails at
    # startup.
    qa_prompt_version: str | None = None
    # Versioned separately from `qa_prompt_version`: the summarizing prompt is a
    # different call to a different model, and rolling one back must not roll
    # back the other.
    qa_compaction_prompt_version: str | None = None
    # Compaction of the QA run's conversation; see app/agents/qa/compaction.py.
    # `enabled` is a kill switch rather than a feature flag: a run that breaks on
    # compaction has to be recoverable without a deploy.
    #
    # The trigger is a fraction of the model's `max_input_tokens`, measured over
    # the messages as `fold_stale_scenes` leaves them — what is actually sent.
    #
    # `keep_messages` bounds how many messages survive verbatim, NOT how large
    # they are. If runs ever show compaction firing on consecutive turns, the fix
    # is a fractional keep, which bounds the preserved tail by size instead.
    #
    # `min_new_messages` is the thrash guard, and it is about cost rather than
    # correctness: each compaction pays for a summary call and then invalidates
    # the whole Anthropic prompt cache, since it rewrites the prefix that cache
    # is keyed on.
    #
    # `trim_tokens` is how much history the summarizer itself reads. LangChain
    # defaults to 4000, which drops most of a QA run before the summary is even
    # written.
    #
    # The summarizer model is fixed rather than "whatever the run uses": the job
    # is compression, and paying a frontier model's rate for it on every trigger
    # buys nothing the run can use. It is an OpenRouter slug rather than an
    # `LLMModel` because this module cannot import `app.llm` — importing any part
    # of that package runs its `__init__`, which pulls in `chat_model`, which
    # imports this module back. The validator below closes the same gap the type
    # would have: a slug outside the catalog fails on the first `get_settings()`.
    qa_compaction_enabled: bool = True
    qa_compaction_trigger_fraction: float = 0.9
    qa_compaction_keep_messages: int = 20
    qa_compaction_min_new_messages: int = 4
    qa_compaction_trim_tokens: int = 8000
    qa_compaction_model: str = "openai/gpt-5.6-luna"

    @field_validator("qa_compaction_model")
    @classmethod
    def known_model(cls, value: str) -> str:
        from app.llm.models import LLMModel

        try:
            LLMModel(value)
        except ValueError as error:
            known = ", ".join(model.value for model in LLMModel)
            raise ValueError(
                f"qa_compaction_model '{value}' is not in the catalog. Known: {known}."
            ) from error
        return value
    scenario_prompt_version: str | None = None
    game_context_prompt_version: str | None = None
    knowledge_query_prompt_version: str | None = None

    # Stamped onto every `test-case.v1` record. Spec discovery is deterministic
    # today — no prompt is loaded and no model is called — so both are `None`
    # unless an operator sets them. They are configuration rather than constants
    # because a stored test case has to say what produced it, and the day a
    # wording pass is added the records already have somewhere to say so.
    spec_prompt_version: str | None = None
    spec_model: str | None = None

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
    # One request's worth of texts. LangChain still slices the batch into
    # chunks of this size, so raising it raises what one request carries.
    embedding_batch_limit: int = 100_000

    # Knowledge items per /knowledge-queries call. Each item is its own model
    # call, so this bounds fan-out, not payload size.
    knowledge_query_batch_limit: int = 100_000

    # Where LLM usage records are shipped. Unset switches collection off
    # entirely — a local run or a test has nowhere to send them, and an
    # unreachable endpoint would only produce a warning per batch.
    orchestration_base_url: str | None = None
    # A batch leaves once either bound is hit. The size keeps a busy QA run from
    # holding a long backlog; the interval keeps a quiet process from holding a
    # short one until shutdown. Neither is a correctness bound: the endpoint has
    # no idempotency key, so nothing is ever resent.
    llm_usage_flush_size: int = 20
    llm_usage_flush_interval_seconds: float = 5.0
    # Ceiling on records held while Orchestration is unreachable. Past it the
    # oldest go, because an outage must cost accuracy rather than the process.
    llm_usage_max_buffer: int = 1_000_000

    # /extract source fetch guards. allowed_hosts empty = no host restriction
    # (rely on the caller passing presigned URLs to the expected bucket).
    extract_max_bytes: int = 2 * 1024 * 1024 * 1024
    extract_timeout_seconds: float = 3600.0
    extract_allowed_hosts: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()
