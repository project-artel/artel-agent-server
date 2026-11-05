import time
from collections.abc import Callable
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
    """Process-local store. Keeps ``ttl_seconds`` after the last save, like Redis.

    Expiration slides on ``save`` only; ``load`` does not refresh it, the same as
    ``RedisSessionStore``. ``ttl_seconds=None`` keeps records until deleted.
    """

    def __init__(
        self,
        ttl_seconds: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._data: dict[str, tuple[str, float | None]] = {}

    async def save(self, session_id: str, record: SessionRecord) -> None:
        self._drop_expired()
        expires_at = None if self._ttl is None else self._clock() + self._ttl
        self._data[session_id] = (record.model_dump_json(), expires_at)

    async def load(self, session_id: str) -> SessionRecord | None:
        self._drop_expired()
        entry = self._data.get(session_id)
        if entry is None:
            return None
        return SessionRecord.model_validate_json(entry[0])

    async def delete(self, session_id: str) -> None:
        self._data.pop(session_id, None)

    def _drop_expired(self) -> None:
        now = self._clock()
        for key in [k for k, (_, at) in self._data.items() if at is not None and at <= now]:
            del self._data[key]
