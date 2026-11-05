from fastapi import Request

from app.qa.service import QaExecutionService
from app.sessions.service import SessionService


def get_session_service(request: Request) -> SessionService:
    """Return the scenario SessionService wired in the app lifespan."""
    return request.app.state.session_service


def get_qa_session_service(request: Request) -> QaExecutionService:
    """Return the QA execution service wired in the app lifespan."""
    return request.app.state.qa_session_service
