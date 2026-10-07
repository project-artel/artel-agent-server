"""Where the LLM key comes from: administrator value, then environment, with a TTL."""

import asyncio

import httpx
import pytest

from app.llm.api_key import KeyRefreshOnUnauthorized, LlmApiKeyProvider

BASE_URL = "http://orchestration:8081"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def build_provider(handler, *, environment_key="env-key", ttl=30.0, clock=None, base_url=BASE_URL):
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    provider = LlmApiKeyProvider(
        orchestration_base_url=base_url,
        environment_key=environment_key,
        ttl_seconds=ttl,
        transport=httpx.MockTransport(recording),
        clock=clock or FakeClock(),
    )
    return provider, calls


def answer(key, source="admin"):
    return lambda request: httpx.Response(200, json={"apiKey": key, "source": source})


def test_admin_key_wins_over_environment_key():
    provider, calls = build_provider(answer("admin-key"))

    assert provider.get() == "admin-key"
    assert calls[0].url.path == "/internal/settings/llm"


def test_environment_key_answers_when_admin_has_no_key():
    provider, _ = build_provider(answer(None, source="none"))

    assert provider.get() == "env-key"


def test_blank_admin_key_counts_as_no_key():
    provider, _ = build_provider(answer("   "))

    assert provider.get() == "env-key"


def test_cache_holds_until_ttl_then_refetches():
    clock = FakeClock()
    keys = iter(["first", "second"])
    provider, calls = build_provider(
        lambda request: httpx.Response(200, json={"apiKey": next(keys)}), clock=clock
    )

    assert provider.get() == "first"
    clock.now += 29
    assert provider.get() == "first"
    assert len(calls) == 1

    clock.now += 2
    assert provider.get() == "second"
    assert len(calls) == 2


def test_invalidate_forces_a_refetch_before_the_ttl():
    keys = iter(["old", "new"])
    provider, calls = build_provider(
        lambda request: httpx.Response(200, json={"apiKey": next(keys)})
    )

    assert provider.get() == "old"
    provider.invalidate()
    assert provider.get() == "new"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(500),
        lambda request: httpx.Response(401),
        lambda request: httpx.Response(200, text="not json"),
        lambda request: httpx.Response(200, json=["unexpected"]),
    ],
    ids=["server-error", "unauthorized", "non-json", "wrong-shape"],
)
def test_unexpected_answers_fall_back_to_environment_key(handler):
    provider, _ = build_provider(handler)

    assert provider.get() == "env-key"


def test_orchestration_down_falls_back_and_is_asked_once_per_ttl():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    clock = FakeClock()
    provider, calls = build_provider(refuse, clock=clock)

    assert provider.get() == "env-key"
    assert provider.get() == "env-key"
    assert len(calls) == 1

    clock.now += 31
    assert provider.get() == "env-key"
    assert len(calls) == 2


def test_recovery_after_an_outage_picks_up_the_admin_key():
    clock = FakeClock()
    outage = {"down": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if outage["down"]:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"apiKey": "admin-key"})

    provider, _ = build_provider(handler, clock=clock)
    assert provider.get() == "env-key"

    outage["down"] = False
    clock.now += 31
    assert provider.get() == "admin-key"


def test_no_orchestration_url_means_environment_only_and_no_request():
    provider, calls = build_provider(answer("admin-key"), base_url=None)

    assert provider.get() == "env-key"
    assert calls == []


def test_no_key_anywhere_gives_none_and_a_placeholder():
    provider, _ = build_provider(answer(None), environment_key=None)

    assert provider.get() is None
    assert provider.get_or_placeholder() == "missing"


def test_key_is_never_logged(caplog):
    caplog.set_level("DEBUG")
    provider, _ = build_provider(lambda request: httpx.Response(500, text="secret-admin-key"))
    provider.get()

    assert "secret-admin-key" not in caplog.text
    assert "env-key" not in caplog.text


def test_unauthorized_chat_error_invalidates_the_cache():
    class Unauthorized(Exception):
        status_code = 401

    class ServerError(Exception):
        status_code = 500

    keys = iter(["old", "new", "newer"])
    provider, calls = build_provider(
        lambda request: httpx.Response(200, json={"apiKey": next(keys)})
    )
    callback = KeyRefreshOnUnauthorized(provider)

    assert provider.get() == "old"
    asyncio.run(callback.on_llm_error(ServerError()))
    assert provider.get() == "old"
    asyncio.run(callback.on_llm_error(Unauthorized()))
    assert provider.get() == "new"


def test_chat_client_sends_the_key_the_provider_currently_holds(monkeypatch):
    """The cached chat client reads the key per request, so a replaced key is used."""
    from app.config import get_settings
    from app.llm import api_key as api_key_module
    from app.llm.chat_model import build_chat_model
    from app.llm.models import DEFAULT_MODEL

    keys = iter(["first-key", "second-key"])
    provider, _ = build_provider(lambda request: httpx.Response(200, json={"apiKey": next(keys)}))
    monkeypatch.setattr(api_key_module, "get_llm_api_key_provider", lambda: provider)
    monkeypatch.setattr("app.llm.chat_model.get_llm_api_key_provider", lambda: provider)
    get_settings.cache_clear()
    build_chat_model.cache_clear()

    seen: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    chat = build_chat_model(DEFAULT_MODEL)
    chat.root_async_client._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    chat.async_client._client = chat.root_async_client._client
    chat.root_async_client.max_retries = 0

    for _ in range(2):
        with pytest.raises(Exception):
            asyncio.run(chat.ainvoke("hi"))

    build_chat_model.cache_clear()
    assert seen == ["Bearer first-key", "Bearer second-key"]
