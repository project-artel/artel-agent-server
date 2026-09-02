from functools import lru_cache
from typing import Literal

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

    # `build_chat_model` 이 매 chat model 호출을 어느 backend 로 보낼지 고른다
    # (app/llm/chat_model.py). 기본값 `openrouter` 는 그대로 둔다.
    #
    # `claude_subscription` 은 이 머신에 로그인된 `claude` CLI credential 을 그대로
    # 써서 API key 없이 개발자 개인 Claude 구독(Pro/Max/Team/Enterprise 의 월간
    # credit, 5시간 rate-limit window)에서 사용량을 뺀다. 로컬 테스트 전용 경로다 —
    # 배포 환경에서 켜면 서버 트래픽이 사람 한 명의 개인 구독 rate limit 을 나눠 쓰게
    # 되므로 절대 켜서는 안 된다.
    llm_backend: Literal["openrouter", "claude_subscription"] = "openrouter"
    # `claude_subscription` backend 아래에서, 호출이 요청한 `LLMModel` 이 Anthropic
    # slug 가 아닐 때 대신 쓰는 Claude 모델 이름이다. 장식이 아니다 — `DEFAULT_MODEL`
    # 이 `openai/gpt-5.6-luna` 라서, 이 backend 에서는 대체가 예외가 아니라 대부분의
    # 호출이 거치는 흔한 경로다.
    #
    # `LLMModel` catalog 의 OpenRouter slug (`anthropic/claude-sonnet-5`) 가 아니라
    # Claude Agent SDK 가 그대로 받는 맨 모델 이름(`claude-sonnet-5`)이다. SDK 가
    # 실제로 인식하는 이름의 목록은 `claude-agent-sdk` 안에만 있고, 그 패키지는
    # dev-only dependency 라서 이 모듈에서 import 해 검증할 수 없다 — 여기서 억지로
    # 만든 검증은 `claude-agent-sdk` 가 이미 하는 검사를 흉내만 내고 그 결과와
    # 어긋날 수 있다. 그래서 이 필드는 `qa_compaction_model` 과 달리 validator 를
    # 두지 않는다.
    claude_subscription_fallback_model: str = "claude-sonnet-5"

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
