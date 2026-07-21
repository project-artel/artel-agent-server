from typing import Protocol

from app.sessions.schemas import SessionRecord


class SessionExpired(RuntimeError):
    """Raised when a session key is missing (expired, evicted, or never opened)."""


class SessionStore(Protocol):
    async def save(self, session_id: str, record: SessionRecord) -> None:
        """Persist a session record, refreshing its TTL (sliding expiration)."""

    async def load(self, session_id: str) -> SessionRecord | None:
        """Return the record, or None when the session is absent."""

    async def delete(self, session_id: str) -> None:
        """Evict a session immediately."""


class InMemorySessionStore:
    """Non-persistent store for tests and single-process fallback (no TTL)."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def save(self, session_id: str, record: SessionRecord) -> None:
        self._data[session_id] = record.model_dump_json()

    async def load(self, session_id: str) -> SessionRecord | None:
        raw = self._data.get(session_id)
        if raw is None:
            return None
        return SessionRecord.model_validate_json(raw)

    async def delete(self, session_id: str) -> None:
        self._data.pop(session_id, None)
