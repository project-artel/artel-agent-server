from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.agents import GameContextAgent, KnowledgeQueryAgent, StepPhrasingAgent
from app.agents.project_agent import ProjectAgent
from app.api.embeddings import router as embeddings_router
from app.api.demonstrations import router as demonstrations_router
from app.api.extract import router as extract_router
from app.api.knowledge_queries import router as knowledge_queries_router
from app.api.llm_cache import router as llm_cache_router
from app.api.macros import router as macros_router
from app.api.project_agent import router as project_agent_router
from app.api.qa_sessions import router as qa_sessions_router
from app.api.routes import router as api_router
from app.api.sessions import router as sessions_router
from app.api.specs_v2 import router as specs_v2_router
from app.api.step_phrasing import router as step_phrasing_router
from app.config import get_settings
from app.documents import ExtractionService
from app.demonstrations.agent import DemonstrationExtractionAgent
from app.llm import build_embedding_client
from app.llm.catalog import ModelCatalogUnavailableError
from app.llm.usage import get_usage_buffer
from app.logging_config import configure_logging
from app.observability import configure_langsmith
from app.prompts import validate_prompts
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore, QaSessionStore, RedisQaSessionStore
from app.sessions.redis_store import RedisSessionStore
from app.sessions.service import SessionService
from app.sessions.store import InMemorySessionStore, SessionStore


# Every business route lives here. The prefix is the trust boundary: what is
# under it is unauthenticated server-to-server traffic from orchestration, and
# what is outside it is not. `/health` and the docs entry points stay outside
# because they are container and tooling surface, not product API. See
# `.agents/docs/project.md`, "API 표면과 신뢰 경계".
INTERNAL_PREFIX = "/internal"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # Session stores wired once per process. Chat models are built lazily per
    # model slug (OpenRouter) inside the agent — no client here. With
    # SESSION_STORE=memory no Redis client is created or imported.
    redis = None
    store: SessionStore
    qa_store: QaSessionStore
    if settings.session_store == "redis":
        from redis.asyncio import from_url as redis_from_url

        redis = redis_from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        store = RedisSessionStore(redis, settings.session_ttl_seconds)
        # QA execution sessions share the Redis client (distinct `qa:` namespace).
        qa_store = RedisQaSessionStore(redis, settings.session_ttl_seconds)
    else:
        store = InMemorySessionStore(settings.session_ttl_seconds)
        qa_store = InMemoryQaSessionStore(settings.session_ttl_seconds)
    app.state.session_service = SessionService(
        store=store,
        history_max_turns=settings.history_max_turns,
    )

    # The runner is built per session, from the config that session resolved at
    # open — see `app/qa/run_config.py`.
    app.state.qa_session_service = QaExecutionService(store=qa_store)

    # Stateless game_context extraction: shared HTTP client for source fetches.
    http_client = httpx.AsyncClient()
    app.state.extraction_service = ExtractionService(
        agent=GameContextAgent(),
        http_client=http_client,
        max_bytes=settings.extract_max_bytes,
        timeout=settings.extract_timeout_seconds,
        allowed_hosts=settings.extract_allowed_hosts,
    )

    # Knowledge indexing support, both stateless: this server can produce
    # vectors and search queries, and Orchestration stores what comes back.
    app.state.embedding_client = build_embedding_client(settings)
    app.state.knowledge_query_agent = KnowledgeQueryAgent()
    app.state.knowledge_query_batch_limit = settings.knowledge_query_batch_limit
    app.state.step_phrasing_agent = StepPhrasingAgent()
    app.state.project_agent = ProjectAgent()
    app.state.demonstration_extraction_agent = DemonstrationExtractionAgent()

    # LLM spend collection. No-op without ORCHESTRATION_BASE_URL; stop() sends
    # the partial batch, without which every deploy loses one.
    usage_buffer = get_usage_buffer()
    usage_buffer.start()
    try:
        yield
    finally:
        await usage_buffer.stop()
        await http_client.aclose()
        if redis is not None:
            await redis.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    # Before anything else: until this runs, every `logger.*` call in the
    # application is discarded, including the ones that report startup failing —
    # which is why it precedes the tracing setup rather than following it.
    configure_logging(settings.log_level)
    # Before any chat model is built, so every LangChain call is traced.
    configure_langsmith(settings)
    # Read and check every prompt file now. A prompt whose placeholders have
    # drifted, or a *_PROMPT_VERSION naming a version nobody created, must stop
    # the process here rather than surface as a failed run an hour into a shift.
    validate_prompts()
    app = FastAPI(
        title="Artel Agent Server API",
        description=(
            "API contract for Artel scenario generation, QA execution, "
            "and bug report workflows."
        ),
        version=settings.app_version,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    # `/health` only. Outside the prefix on purpose: the container health check
    # and monitoring reach it, and it carries nothing that needs the boundary.
    app.include_router(api_router)
    # Everything else. The prefix is applied at mount time, so the route modules
    # themselves stay free of it — a router does not decide where it is mounted.
    app.include_router(sessions_router, prefix=INTERNAL_PREFIX)
    app.include_router(qa_sessions_router, prefix=INTERNAL_PREFIX)
    app.include_router(extract_router, prefix=INTERNAL_PREFIX)
    app.include_router(embeddings_router, prefix=INTERNAL_PREFIX)
    app.include_router(knowledge_queries_router, prefix=INTERNAL_PREFIX)
    app.include_router(specs_v2_router, prefix=INTERNAL_PREFIX)
    app.include_router(step_phrasing_router, prefix=INTERNAL_PREFIX)
    app.include_router(project_agent_router, prefix=INTERNAL_PREFIX)
    app.include_router(llm_cache_router, prefix=INTERNAL_PREFIX)
    app.include_router(macros_router, prefix=INTERNAL_PREFIX)
    app.include_router(demonstrations_router, prefix=INTERNAL_PREFIX)
    app.add_exception_handler(ModelCatalogUnavailableError, _model_catalog_unavailable)
    app.add_exception_handler(RequestValidationError, _validation_error_without_api_keys)
    return app


# 422 응답의 `input` 에서 값을 가리는 field 이름. `POST /internal/qa-sessions` 의
# `llm_key.api_key` 가 그 하나다.
_REDACTED_FIELDS = frozenset({"api_key"})
_REDACTED = "**********"


def _without_api_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _REDACTED if key in _REDACTED_FIELDS else _without_api_keys(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_api_keys(item) for item in value]
    return value


async def _validation_error_without_api_keys(
    _request: Request, error: Exception
) -> JSONResponse:
    """FastAPI 기본 422 와 같은 모양이되, `input` 에 실린 사용자 API key 를 가린다.

    body 전체를 보는 `model_validator` 가 실패하면 pydantic 은 그 오류의 `input` 에 body
    전체를 싣고, FastAPI 기본 처리는 그것을 그대로 응답에 넣는다. 그러면 reasoning 하나가
    틀린 요청의 422 에 사용자 key 가 찍혀 orchestration 으로 돌아간다.
    """
    assert isinstance(error, RequestValidationError)
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(_without_api_keys(list(error.errors())))},
    )


async def _model_catalog_unavailable(
    _request: Request, error: Exception
) -> JSONResponse:
    # catalog 를 한 번도 못 읽었으면 모델 id 를 검사할 수 없다. 요청이 틀린 것이 아니라
    # orchestration server 에 닿지 못한 것이므로 422 가 아니라 503 이다.
    return JSONResponse(status_code=503, content={"detail": str(error)})


app = create_app()
