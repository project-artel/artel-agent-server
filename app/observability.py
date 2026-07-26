"""LangSmith tracing wiring.

LangChain picks up tracing config from the process environment at call time,
while this application reads its configuration from `.env` through
pydantic-settings — which never writes to `os.environ`. Without this bridge a
local run stays untraced even with the keys filled in.
"""

import logging
import os

from app.config import Settings

logger = logging.getLogger(__name__)


def configure_langsmith(settings: Settings) -> bool:
    """Publish LangSmith settings into the process environment.

    Returns whether tracing ended up enabled.
    """
    if not settings.langsmith_tracing:
        os.environ["LANGSMITH_TRACING"] = "false"
        return False

    if not settings.langsmith_api_key:
        os.environ["LANGSMITH_TRACING"] = "false"
        logger.warning(
            "LANGSMITH_TRACING is on but LANGSMITH_API_KEY is missing; "
            "tracing stays disabled."
        )
        return False

    project = settings.langsmith_project or f"artel-agent-server-{settings.environment}"

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    os.environ["LANGSMITH_PROJECT"] = project

    logger.info("LangSmith tracing enabled (project=%s)", project)
    return True
