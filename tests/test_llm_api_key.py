"""Where the provider keys come from: the orchestration server only, with a TTL."""

import asyncio

import httpx
import pytest

from app.config import Settings
from app.llm.api_key import (
    KeyRefreshOnUnauthorized,
    LlmApiKeyProvider,
    is_key_rejected_by_anthropic,
    is_key_rejected_by_bedrock,
)

BASE_URL = "http://orchestration:8081"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def build_provider(handler, *, ttl=30.0, clock=None, base_url=BASE_URL):
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    provider = LlmApiKeyProvider(
        orchestration_base_url=base_url,
        ttl_seconds=ttl,
        transport=httpx.MockTransport(recording),
        clock=clock or FakeClock(),
    )
    return provider, calls


def answer(openrouter=None, bedrock=None):
    return lambda request: httpx.Response(
        200, json={"openrouterApiKey": openrouter, "bedrockApiKey": bedrock}
    )


def test_both_keys_are_read_from_the_settings_endpoint():
    provider, calls = build_provider(answer("or-key", "br-key"))

    assert provider.get_openrouter_key() == "or-key"
    assert provider.get_bedrock_key() == "br-key"
    assert calls[0].url.path == "/internal/settings/llm"
    # One response carries both keys, so reading both costs one request.
    assert len(calls) == 1


def test_either_key_may_be_null():
    provider, _ = build_provider(answer("or-key", None))

    assert provider.get_openrouter_key() == "or-key"
    assert provider.get_bedrock_key() is None


def test_blank_keys_count_as_no_key():
    provider, _ = build_provider(answer("   ", ""))

    assert provider.get_openrouter_key() is None
    assert provider.get_bedrock_key() is None


def test_the_old_response_shape_gives_no_key():
    provider, _ = build_provider(lambda request: httpx.Response(200, json={"apiKey": "old"}))

    assert provider.get_openrouter_key() is None


def test_environment_keys_are_not_read(monkeypatch):
    """The keys live only in the database. A key exported by the shell must not leak in."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-or-key")
    monkeypatch.setenv("LLM_API_KEY", "env-llm-key")
    monkeypatch.setenv("BEDROCK_API_KEY", "env-br-key")
    provider, _ = build_provider(answer(None, None))

    assert provider.get_openrouter_key() is None
    assert provider.get_bedrock_key() is None
    assert provider.get_openrouter_key_or_placeholder() == "missing"
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "llm_api_key")
    assert not hasattr(settings, "bedrock_api_key")


def test_cache_holds_until_ttl_then_refetches():
    clock = FakeClock()
    keys = iter(["first", "second"])
    provider, calls = build_provider(
        lambda request: httpx.Response(200, json={"openrouterApiKey": next(keys)}),
        clock=clock,
    )

    assert provider.get_openrouter_key() == "first"
    clock.now += 29
    assert provider.get_openrouter_key() == "first"
    assert len(calls) == 1

    clock.now += 2
    assert provider.get_openrouter_key() == "second"
    assert len(calls) == 2


def test_invalidate_forces_a_refetch_of_both_keys_before_the_ttl():
    answers = iter([("old-or", "old-br"), ("new-or", "new-br")])

    def handler(request):
        openrouter, bedrock = next(answers)
        return httpx.Response(
            200, json={"openrouterApiKey": openrouter, "bedrockApiKey": bedrock}
        )

    provider, calls = build_provider(handler)

    assert provider.get_bedrock_key() == "old-br"
    provider.invalidate()
    assert provider.get_bedrock_key() == "new-br"
    assert provider.get_openrouter_key() == "new-or"
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
def test_unexpected_answers_before_any_load_give_no_key(handler):
    provider, _ = build_provider(handler)

    assert provider.get_openrouter_key() is None
    assert provider.get_bedrock_key() is None


def test_a_failed_refresh_keeps_the_last_keys():
    clock = FakeClock()
    state = {"down": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["down"]:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"openrouterApiKey": "or", "bedrockApiKey": "br"})

    provider, _ = build_provider(handler, clock=clock)
    assert provider.get_bedrock_key() == "br"

    state["down"] = True
    clock.now += 31
    assert provider.get_openrouter_key() == "or"
    assert provider.get_bedrock_key() == "br"


def test_orchestration_down_is_asked_once_per_ttl():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    clock = FakeClock()
    provider, calls = build_provider(refuse, clock=clock)

    assert provider.get_openrouter_key() is None
    assert provider.get_bedrock_key() is None
    assert len(calls) == 1

    clock.now += 31
    assert provider.get_openrouter_key() is None
    assert len(calls) == 2


def test_recovery_after_an_outage_picks_up_the_keys():
    clock = FakeClock()
    outage = {"down": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if outage["down"]:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"openrouterApiKey": "admin-key"})

    provider, _ = build_provider(handler, clock=clock)
    assert provider.get_openrouter_key() is None

    outage["down"] = False
    clock.now += 31
    assert provider.get_openrouter_key() == "admin-key"


def test_no_orchestration_url_means_no_key_and_no_request():
    provider, calls = build_provider(answer("or", "br"), base_url=None)

    assert provider.get_openrouter_key() is None
    assert provider.get_bedrock_key() is None
    assert calls == []


def test_no_openrouter_key_gives_a_placeholder():
    provider, _ = build_provider(answer(None, None))

    assert provider.get_openrouter_key_or_placeholder() == "missing"


def test_keys_are_never_logged_or_printed(caplog):
    caplog.set_level("DEBUG")
    provider, _ = build_provider(answer("secret-or-key", "secret-br-key"))
    provider.get_openrouter_key()

    # pydantic quotes the offending input in its error, and here that input is a key.
    bad_body, _ = build_provider(
        lambda request: httpx.Response(200, json={"openrouterApiKey": ["secret-in-list"]})
    )
    bad_body.get_openrouter_key()

    failing, _ = build_provider(lambda request: httpx.Response(500, text="secret-in-body"))
    failing.get_openrouter_key()

    assert "secret" not in caplog.text
    assert "secret" not in repr(provider._keys)


def test_unauthorized_chat_error_invalidates_the_cache():
    class Unauthorized(Exception):
        status_code = 401

    class ServerError(Exception):
        status_code = 500

    keys = iter(["old", "new", "newer"])
    provider, calls = build_provider(
        lambda request: httpx.Response(200, json={"openrouterApiKey": next(keys)})
    )
    callback = KeyRefreshOnUnauthorized(provider)

    assert provider.get_openrouter_key() == "old"
    asyncio.run(callback.on_llm_error(ServerError()))
    assert provider.get_openrouter_key() == "old"
    asyncio.run(callback.on_llm_error(Unauthorized()))
    assert provider.get_openrouter_key() == "new"


def _botocore_error(status: int) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": "UnrecognizedClientException", "Message": "bad token"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "ConverseStream",
    )


@pytest.mark.parametrize(("status", "rejected"), [(401, True), (403, True), (400, False), (500, False)])
def test_bedrock_rejection_is_read_off_botocore_errors(status, rejected):
    assert is_key_rejected_by_bedrock(_botocore_error(status)) is rejected


def test_bedrock_rejection_invalidates_the_cache():
    keys = iter(["old", "new"])
    provider, _ = build_provider(
        lambda request: httpx.Response(200, json={"bedrockApiKey": next(keys)})
    )
    callback = KeyRefreshOnUnauthorized(provider, rejected=is_key_rejected_by_bedrock)

    assert provider.get_bedrock_key() == "old"
    asyncio.run(callback.on_llm_error(_botocore_error(403)))
    assert provider.get_bedrock_key() == "new"


def test_the_claude_api_key_is_read_from_the_same_response():
    provider, calls = build_provider(
        lambda request: httpx.Response(
            200,
            json={
                "openrouterApiKey": "or-key",
                "bedrockApiKey": None,
                "anthropicApiKey": " sk-ant-key ",
            },
        )
    )

    assert provider.get_anthropic_key() == "sk-ant-key"
    assert provider.get_openrouter_key() == "or-key"
    assert len(calls) == 1
    assert "sk-ant" not in repr(provider._keys)


def test_a_missing_claude_api_key_is_none():
    provider, _ = build_provider(answer("or-key", "br-key"))

    assert provider.get_anthropic_key() is None


def _anthropic_error(status: int) -> Exception:
    import anthropic

    response = httpx.Response(
        status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    error_class = {
        401: anthropic.AuthenticationError,
        403: anthropic.PermissionDeniedError,
        500: anthropic.InternalServerError,
    }[status]
    return error_class("rejected", response=response, body=None)


@pytest.mark.parametrize(("status", "rejected"), [(401, True), (403, False), (500, False)])
def test_claude_api_rejection_is_an_authentication_error(status, rejected):
    assert is_key_rejected_by_anthropic(_anthropic_error(status)) is rejected


def test_langchain_anthropics_own_authentication_error_counts():
    """langchain-anthropic re-raises the SDK error as its own subclass."""
    from langchain_anthropic.chat_models import AnthropicAuthenticationError

    original = _anthropic_error(401)
    wrapped = AnthropicAuthenticationError(
        message="invalid x-api-key", response=original.response, body=None
    )

    assert is_key_rejected_by_anthropic(wrapped) is True


def test_claude_api_rejection_invalidates_the_cache():
    keys = iter(["old", "new"])
    provider, _ = build_provider(
        lambda request: httpx.Response(200, json={"anthropicApiKey": next(keys)})
    )
    callback = KeyRefreshOnUnauthorized(provider, rejected=is_key_rejected_by_anthropic)

    assert provider.get_anthropic_key() == "old"
    asyncio.run(callback.on_llm_error(_anthropic_error(500)))
    assert provider.get_anthropic_key() == "old"
    asyncio.run(callback.on_llm_error(_anthropic_error(401)))
    assert provider.get_anthropic_key() == "new"


def test_chat_client_sends_the_key_the_provider_currently_holds(monkeypatch):
    """The cached chat client reads the key per request, so a replaced key is used."""
    from app.config import get_settings
    from app.llm import api_key as api_key_module
    from app.llm.chat_model import build_chat_model
    from app.llm.models import DEFAULT_MODEL

    keys = iter(["first-key", "second-key"])
    provider, _ = build_provider(
        lambda request: httpx.Response(200, json={"openrouterApiKey": next(keys)})
    )
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


def test_the_typesafe_key_is_read_with_the_others():
    provider, calls = build_provider(
        lambda request: httpx.Response(
            200, json={"openrouterApiKey": "or-key", "typesafeApiKey": " ts-key "}
        )
    )

    assert provider.get_typesafe_key() == "ts-key"
    assert provider.get_openrouter_key() == "or-key"
    assert len(calls) == 1
    assert "ts-key" not in repr(provider._current())


def test_a_body_without_the_typesafe_key_means_none():
    provider, _ = build_provider(answer("or-key"))

    assert provider.get_typesafe_key() is None
