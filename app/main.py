from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import from_url as redis_from_url

from app.api.routes import router as api_router
from app.api.sessions import router as sessions_router
from app.config import get_settings
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
    try:
        yield
    finally:
        await redis.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
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
    return app


app = create_app()
