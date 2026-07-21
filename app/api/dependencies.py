from fastapi import Request

from app.llm import LLMClient
from app.sessions.service import SessionService


def get_llm_client(request: Request) -> LLMClient:
    """Return the process-wide LLM client wired in the app lifespan."""
    return request.app.state.llm_client


def get_session_service(request: Request) -> SessionService:
    """Return the scenario SessionService wired in the app lifespan."""
    return request.app.state.session_service
