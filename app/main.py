from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agents import ScenarioAgent
from app.api.routes import router as api_router
from app.config import get_settings
from app.llm import OpenRouterClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Real LLM client wired once per process. No request is issued here;
    # callers reach it via AgentContext(llm=app.state.llm_client).
    llm_client = OpenRouterClient()
    app.state.llm_client = llm_client
    app.state.scenario_agent = ScenarioAgent()
    try:
        yield
    finally:
        await llm_client.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.include_router(api_router)
    return app


app = create_app()
