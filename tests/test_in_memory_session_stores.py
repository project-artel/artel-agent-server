import asyncio
import sys

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.qa.run_config import resolve_run_config
from app.qa.schemas import QaSessionRecord
from app.qa.store import InMemoryQaSessionStore
from app.sessions.schemas import SessionRecord
from app.sessions.store import InMemorySessionStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _qa_record() -> QaSessionRecord:
    return QaSessionRecord(
        qa_run_id=1,
        game_instance_id=2,
        scenarios=[],
        run_config=resolve_run_config(),
    )


def _stores(clock: FakeClock):
    return [
        (InMemorySessionStore(60, clock), SessionRecord()),
        (InMemoryQaSessionStore(60, clock), _qa_record()),
    ]


def test_records_expire_after_ttl_seconds() -> None:
    asyncio.run(_test_records_expire_after_ttl_seconds())


async def _test_records_expire_after_ttl_seconds() -> None:
    clock = FakeClock()
    for store, record in _stores(clock):
        await store.save("a", record)
        clock.now += 59
        assert await store.load("a") is not None
        clock.now += 1
        assert await store.load("a") is None


def test_save_refreshes_expiration_but_load_does_not() -> None:
    asyncio.run(_test_save_refreshes_expiration_but_load_does_not())


async def _test_save_refreshes_expiration_but_load_does_not() -> None:
    clock = FakeClock()
    for store, record in _stores(clock):
        await store.save("a", record)
        clock.now += 40
        await store.load("a")
        await store.save("a", record)
        clock.now += 40
        assert await store.load("a") is not None
        clock.now += 20
        assert await store.load("a") is None


def test_without_ttl_records_stay_until_deleted() -> None:
    asyncio.run(_test_without_ttl_records_stay_until_deleted())


async def _test_without_ttl_records_stay_until_deleted() -> None:
    clock = FakeClock()
    for store, record in [
        (InMemorySessionStore(clock=clock), SessionRecord()),
        (InMemoryQaSessionStore(clock=clock), _qa_record()),
    ]:
        await store.save("a", record)
        clock.now += 10**9
        assert await store.load("a") is not None
        await store.delete("a")
        assert await store.load("a") is None


def test_memory_mode_lifespan_never_builds_a_redis_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SESSION_STORE", "memory")
    get_settings.cache_clear()
    sys.modules.pop("redis.asyncio", None)
    try:
        with TestClient(create_app()) as client:
            assert client.get("/health").json() == {"status": "ok"}
            service = client.app.state.session_service
            assert isinstance(service._store, InMemorySessionStore)
            assert isinstance(
                client.app.state.qa_session_service._store, InMemoryQaSessionStore
            )
        assert "redis.asyncio" not in sys.modules
    finally:
        get_settings.cache_clear()
