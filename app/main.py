from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import from_url as redis_from_url

from app.agents import GameContextAgent
from app.api.extract import router as extract_router
from app.api.qa_sessions import router as qa_sessions_router
from app.api.routes import router as api_router
from app.api.sessions import router as sessions_router
from app.config import get_settings
from app.documents import ExtractionService
from app.logging_config import configure_logging
from app.observability import configure_langsmith
from app.qa.service import QaExecutionService
from app.qa.store import RedisQaSessionStore
from app.sessions.redis_store import RedisSessionStore
from app.sessions.service import SessionService


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
    # The runner is built per session, from that session's model and locale.
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
    try:
        yield
    finally:
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
    app.include_router(api_router)
    app.include_router(sessions_router)
    app.include_router(qa_sessions_router)
    app.include_router(extract_router)
    return app


app = create_app()
