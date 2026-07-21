from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import from_url as redis_from_url

from app.api.routes import router as api_router
from app.api.sessions import router as sessions_router
from app.config import get_settings
from app.llm import OpenRouterClient
from app.sessions.redis_store import RedisSessionStore
from app.sessions.service import SessionService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # Real LLM client + Redis-backed session store wired once per process.
    # No LLM request is issued here.
    llm_client = OpenRouterClient()
    redis = redis_from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    store = RedisSessionStore(redis, settings.session_ttl_seconds)

    app.state.llm_client = llm_client
    app.state.session_service = SessionService(
        store=store,
        llm_client=llm_client,
        history_max_turns=settings.history_max_turns,
    )
    try:
        yield
    finally:
        await llm_client.close()
        await redis.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.include_router(api_router)
    app.include_router(sessions_router)
    return app


app = create_app()
