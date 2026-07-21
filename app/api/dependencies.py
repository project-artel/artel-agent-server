from fastapi import Request

from app.sessions.service import SessionService


def get_session_service(request: Request) -> SessionService:
    """Return the scenario SessionService wired in the app lifespan."""
    return request.app.state.session_service
