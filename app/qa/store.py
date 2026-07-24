from typing import Protocol

from redis.asyncio import Redis

from app.qa.schemas import QaSessionRecord


class QaSessionStore(Protocol):
    async def save(self, session_id: str, record: QaSessionRecord) -> None:
        """Persist a QA session record, refreshing its TTL (sliding expiration)."""

    async def load(self, session_id: str) -> QaSessionRecord | None:
        """Return the record, or None when the session is absent."""

    async def delete(self, session_id: str) -> None:
        """Evict a session immediately."""


class InMemoryQaSessionStore:
    """Non-persistent store for tests and single-process fallback (no TTL)."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def save(self, session_id: str, record: QaSessionRecord) -> None:
        self._data[session_id] = record.model_dump_json()

    async def load(self, session_id: str) -> QaSessionRecord | None:
        raw = self._data.get(session_id)
        if raw is None:
            return None
        return QaSessionRecord.model_validate_json(raw)

    async def delete(self, session_id: str) -> None:
        self._data.pop(session_id, None)


class RedisQaSessionStore:
    """Redis-backed QA session store with sliding TTL (refreshed on every save).

    Distinct ``qa:`` key namespace so QA and scenario sessions never collide.
    """

    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    @staticmethod
    def _key(session_id: str) -> str:
        return f"qa:{session_id}"

    async def save(self, session_id: str, record: QaSessionRecord) -> None:
        await self._redis.set(
            self._key(session_id),
            record.model_dump_json(),
            ex=self._ttl,
        )

    async def load(self, session_id: str) -> QaSessionRecord | None:
        raw = await self._redis.get(self._key(session_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return QaSessionRecord.model_validate_json(raw)

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))
