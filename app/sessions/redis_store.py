from redis.asyncio import Redis

from app.sessions.schemas import SessionRecord


class RedisSessionStore:
    """Redis-backed session store with sliding TTL (refreshed on every save)."""

    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    @staticmethod
    def _key(session_id: str) -> str:
        return f"session:{session_id}"

    async def save(self, session_id: str, record: SessionRecord) -> None:
        await self._redis.set(
            self._key(session_id),
            record.model_dump_json(),
            ex=self._ttl,
        )

    async def load(self, session_id: str) -> SessionRecord | None:
        raw = await self._redis.get(self._key(session_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return SessionRecord.model_validate_json(raw)

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))
