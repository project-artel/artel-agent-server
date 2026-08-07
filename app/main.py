from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import from_url as redis_from_url

from app.agents import GameContextAgent, KnowledgeQueryAgent
from app.api.embeddings import router as embeddings_router
from app.api.extract import router as extract_router
from app.api.knowledge_queries import router as knowledge_queries_router
from app.api.models import router as models_router
from app.api.qa_sessions import router as qa_sessions_router
from app.api.routes import router as api_router
from app.api.sessions import router as sessions_router
from app.api.specs_v2 import router as specs_v2_router
from app.config import get_settings
from app.documents import ExtractionService
from app.llm import build_embedding_client
from app.llm.usage import get_usage_buffer
from app.logging_config import configure_logging
from app.observability import configure_langsmith
from app.prompts import validate_prompts
from app.qa.service import QaExecutionService
from app.qa.store import RedisQaSessionStore
from app.sessions.redis_store import RedisSessionStore
from app.sessions.service import SessionService


# Every business route lives here. The prefix is the trust boundary: what is
# under it is unauthenticated server-to-server traffic from orchestration, and
# what is outside it is not. `/health` and the docs entry points stay outside
# because they are container and tooling surface, not product API. See
# `.agents/docs/project.md`, "API 표면과 신뢰 경계".
INTERNAL_PREFIX = "/internal"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # Redis-backed session store wired once per process. Chat models are built
    # lazily per model slug (OpenRouter) inside the agent — no client here.
    redis = redis_from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    store = RedisSessionStore(redis, settings.session_ttl_seconds)
    app.state.session_service = SessionService(
        store=store,
        history_max_turns=settings.history_max_turns,
    )

    # QA execution sessions share the Redis client (distinct `qa:` namespace).
    qa_store = RedisQaSessionStore(redis, settings.session_ttl_seconds)
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

    # LLM spend collection. No-op without ORCHESTRATION_BASE_URL; stop() sends
    # the partial batch, without which every deploy loses one.
    usage_buffer = get_usage_buffer()
    usage_buffer.start()
    try:
        yield
    finally:
        await usage_buffer.stop()
        await http_client.aclose()
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
    app.include_router(models_router, prefix=INTERNAL_PREFIX)
    app.include_router(specs_v2_router, prefix=INTERNAL_PREFIX)
    return app


app = create_app()
