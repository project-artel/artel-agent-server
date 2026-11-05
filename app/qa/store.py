import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from app.qa.schemas import QaSessionRecord

if TYPE_CHECKING:
    from redis.asyncio import Redis


class QaSessionStore(Protocol):
    async def save(self, session_id: str, record: QaSessionRecord) -> None:
        """Persist a QA session record, refreshing its TTL (sliding expiration)."""

    async def load(self, session_id: str) -> QaSessionRecord | None:
        """Return the record, or None when the session is absent."""

    async def delete(self, session_id: str) -> None:
        """Evict a session immediately."""


class InMemoryQaSessionStore:
    """Process-local store. Keeps ``ttl_seconds`` after the last save, like Redis.

    Expiration slides on ``save`` only; ``load`` does not refresh it, the same as
    ``RedisQaSessionStore``. ``ttl_seconds=None`` keeps records until deleted."""

    def __init__(
        self,
        ttl_seconds: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._data: dict[str, tuple[str, float | None]] = {}

    async def save(self, session_id: str, record: QaSessionRecord) -> None:
        self._drop_expired()
        expires_at = None if self._ttl is None else self._clock() + self._ttl
        self._data[session_id] = (record.model_dump_json(), expires_at)

    async def load(self, session_id: str) -> QaSessionRecord | None:
        self._drop_expired()
        entry = self._data.get(session_id)
        if entry is None:
            return None
        return QaSessionRecord.model_validate_json(entry[0])

    async def delete(self, session_id: str) -> None:
        self._data.pop(session_id, None)

    def _drop_expired(self) -> None:
        now = self._clock()
        for key in [k for k, (_, at) in self._data.items() if at is not None and at <= now]:
            del self._data[key]


class RedisQaSessionStore:
    """Redis-backed QA session store with sliding TTL (refreshed on every save).

    Distinct ``qa:`` key namespace so QA and scenario sessions never collide.
    """

    def __init__(self, redis: "Redis", ttl_seconds: int) -> None:
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
