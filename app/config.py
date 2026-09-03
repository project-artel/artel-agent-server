from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
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
    # 채팅이 말을 거는 곳. **provider 이름을 달지 않는다** — 이 값은 OpenRouter 를
    # 가리킬 수도, 여러 provider 를 함께 서빙하는 gateway 를 가리킬 수도 있다.
    # 이름에 provider 를 박아 두면 다른 곳을 가리키는 순간 그 이름이 거짓이 되고,
    # 읽는 사람은 설정이 아니라 이름을 믿는다.
    #
    # `OPENROUTER_*` 를 함께 받는 것은 하위호환이다. 이 필드를 옮기는 배포와
    # 옮기지 않은 배포가 같은 이미지로 돌 수 있어야 한다. 새 이름이 우선한다.
    llm_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "OPENROUTER_API_KEY"),
    )
    llm_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        validation_alias=AliasChoices("LLM_BASE_URL", "OPENROUTER_BASE_URL"),
    )
    openrouter_site_url: str | None = None
    openrouter_app_title: str = "Artel Agent Server"
    # Per-request ceiling on a model call, and how many times the client retries.
    #
    # Not tuning knobs — the difference between a slow answer and no answer. The
    # OpenAI client defaults to 600 s with 2 retries, so a stalled upstream holds
    # one request for up to 30 minutes while every caller, including a person
    # watching a chat, waits with nothing to read. That happened: an authoring
    # turn was still "requesting" 500 s in and the only way out was reloading the
    # page (ARTEL-510).
    #
    # 180 s is above the slowest authoring turn measured (a 66-case project ran
    # ~70 s) with room for a reasoning model, and far below a wait a person
    # accepts without being told something is wrong.
    openrouter_timeout_seconds: float = 180.0
    openrouter_max_retries: int = 1

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
    # 압축을 어느 모델로 하나. **비우면 그 런이 쓰는 모델을 따른다.**
    #
    # 예전에는 한 슬러그로 고정돼 있었다(`openai/gpt-5.6-luna`). 런은 Bedrock 으로
    # 도는데 압축만 OpenRouter 로 나가는 상태가 되면, 그쪽 credit 이 없을 때 압축이
    # **조용히 실패한다** — 실제로 그렇게 됐다. 압축은 컨텍스트가 창을 넘기 전에 접는
    # 유일한 안전판이라, 그것이 안 도는 채로 도는 런은 길어지면 provider 가 거절해서
    # 통째로 죽는다.
    #
    # 런의 모델을 따르면 그 의존이 사라진다. 값싼 모델로 고정하고 싶은 배치는 여전히
    # 슬러그를 적으면 된다.
    qa_compaction_model: str | None = None

    @field_validator("qa_compaction_model")
    @classmethod
    def known_model(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None

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
    # 화면 제안을 판정하는 agent 의 프롬프트 (ARTEL-656). QA 런의 프롬프트와 따로
    # 버전을 매긴다 — 서로 다른 파일에 서로 다른 속도로 살고, 한쪽을 되돌리는 것이
    # 다른 쪽을 되돌리면 안 된다.
    screen_verdict_prompt_version: str | None = None

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
    # Embeddings authenticate on their own. Chat can move to another gateway or
    # provider for cost reasons — AWS Bedrock, a proxy, a second account — but
    # the embedding model must not move with it: `embedding_dimensions` is
    # pinned into Orchestration's vector(N) column, and a different model fills
    # that column from a different vector space. The stored items and the new
    # ones would then be compared without error and with wrong answers, which
    # is worse than having no search at all.
    #
    # Unset, both fall back to the chat credentials, so nothing changes for a
    # deployment that has not split them.
    # Bedrock 호출이 갈 리전. 모델 값의 `us.` 접두가 inference profile 을 정하고,
    # 이 값은 어느 엔드포인트에 말을 거는지를 정한다. 둘은 다른 축이다.
    bedrock_region: str = "us-west-2"

    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
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
