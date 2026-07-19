from fastapi import FastAPI

from app.api.routes import router as api_router
from app.config import get_settings


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
    )
    app.include_router(api_router)
    return app


app = create_app()
