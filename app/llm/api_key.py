"""Where the provider keys come from, and how long a fetched value is trusted.

The keys live only in the orchestration server, stored encrypted after an
administrator enters them on the admin page. This module reads them from
``GET {ORCHESTRATION_BASE_URL}/internal/settings/llm``, which answers
``{ "openrouterApiKey": ..., "bedrockApiKey": ..., "anthropicApiKey": ...,
"typesafeApiKey": ... }`` with any value possibly null. There is no environment fallback: a key in ``.env`` is
not read.

The fetched keys are cached for ``llm_key_cache_ttl_seconds`` so a model call does
not pay a network round trip. They are dropped early when a provider rejects a
key (401 from OpenRouter or the Claude API, 401 or 403 from Bedrock) or when
orchestration calls ``POST /internal/llm-cache/invalidate``, so a key an
administrator just replaced takes effect on the next call instead of after the
TTL.

Orchestration being down, slow or answering something unexpected keeps the last
keys that loaded and pushes their expiry out by one TTL, so an outage costs one
short wait per TTL rather than one per model call. Before any answer has loaded,
every key is None.

The keys themselves are never logged. Only the HTTP status and error type are.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

import anthropic
import httpx
from langchain_core.callbacks import AsyncCallbackHandler
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from app.config import get_settings

logger = logging.getLogger(__name__)

SETTINGS_PATH = "/internal/settings/llm"

# What the OpenAI client is handed when no OpenRouter key exists. The request then
# fails with 401 at the provider, which names the real problem; an empty string
# would fail earlier with an error that does not.
MISSING_KEY_PLACEHOLDER = "missing"

UNAUTHORIZED = 401
FORBIDDEN = 403


class _LlmSettingsBody(BaseModel):
    """The body of `GET /internal/settings/llm`."""

    model_config = ConfigDict(alias_generator=to_camel, extra="ignore", frozen=True)

    openrouter_api_key: str | None = None
    bedrock_api_key: str | None = None
    anthropic_api_key: str | None = None
    typesafe_api_key: str | None = None


@dataclass(frozen=True)
class ProviderKeys:
    openrouter: str | None = None
    bedrock: str | None = None
    anthropic: str | None = None
    typesafe: str | None = None

    def __repr__(self) -> str:
        # The dataclass default would print every key into any log or traceback
        # that shows this object.
        return (
            f"ProviderKeys(openrouter={'set' if self.openrouter else None}, "
            f"bedrock={'set' if self.bedrock else None}, "
            f"anthropic={'set' if self.anthropic else None}, "
            f"typesafe={'set' if self.typesafe else None})"
        )


NO_KEYS = ProviderKeys()


def _clean(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


class LlmApiKeyProvider:
    """Resolves the provider keys on demand. Safe to share across threads."""

    def __init__(
        self,
        *,
        orchestration_base_url: str | None,
        ttl_seconds: float,
        timeout_seconds: float = 3.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = (orchestration_base_url or "").rstrip("/")
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._keys = NO_KEYS
        self._expires_at: float | None = None

    def get_openrouter_key(self) -> str | None:
        """The OpenRouter key to use now, or None when none is stored."""
        return self._current().openrouter

    def get_openrouter_key_or_placeholder(self) -> str:
        """Same as `get_openrouter_key`, shaped for a client that insists on a string."""
        return self.get_openrouter_key() or MISSING_KEY_PLACEHOLDER

    def get_bedrock_key(self) -> str | None:
        """The Bedrock API key to use now, or None so the AWS credential chain answers."""
        return self._current().bedrock

    def get_anthropic_key(self) -> str | None:
        """The Claude API key to use now, or None when none is stored."""
        return self._current().anthropic

    def get_typesafe_key(self) -> str | None:
        """The TypeSafe API key for calling Jev directly, or None when none is stored."""
        return self._current().typesafe

    def invalidate(self) -> None:
        """Forget the cached keys' freshness so the next call asks orchestration again.

        The keys themselves are kept, so a refetch that fails still has something to use.
        """
        with self._lock:
            self._expires_at = None

    def _current(self) -> ProviderKeys:
        if not self._base_url:
            return NO_KEYS
        with self._lock:
            now = self._clock()
            if self._expires_at is not None and now < self._expires_at:
                return self._keys
            # Holding the lock across the fetch makes concurrent callers wait for
            # one request instead of each sending their own.
            fetched = self._fetch()
            if fetched is not None:
                self._keys = fetched
            self._expires_at = self._clock() + self._ttl_seconds
            return self._keys

    def _fetch(self) -> ProviderKeys | None:
        url = f"{self._base_url}{SETTINGS_PATH}"
        try:
            with httpx.Client(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = client.get(url)
        except httpx.HTTPError as error:
            logger.warning(
                "[llm-key] orchestration unreachable, keeping the last keys: %s",
                type(error).__name__,
            )
            return None

        if response.status_code != httpx.codes.OK:
            logger.warning(
                "[llm-key] orchestration answered %s, keeping the last keys",
                response.status_code,
            )
            return None

        try:
            body = _LlmSettingsBody.model_validate_json(response.content)
        except ValidationError:
            # `error` is not logged: pydantic puts the offending input in it, and
            # that input can be a key.
            logger.warning("[llm-key] orchestration answered an unreadable body")
            return None

        return ProviderKeys(
            openrouter=_clean(body.openrouter_api_key),
            bedrock=_clean(body.bedrock_api_key),
            anthropic=_clean(body.anthropic_api_key),
            typesafe=_clean(body.typesafe_api_key),
        )


def _status_code(error: BaseException) -> int | None:
    """The HTTP status of a provider client error, from either client library.

    The OpenAI client puts it on `status_code`. botocore's `ClientError` puts it
    under `response["ResponseMetadata"]["HTTPStatusCode"]`.
    """
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        metadata = response.get("ResponseMetadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("HTTPStatusCode"), int):
            return metadata["HTTPStatusCode"]
    return None


def is_unauthorized(error: BaseException) -> bool:
    """True when a provider client error is an HTTP 401."""
    return _status_code(error) == UNAUTHORIZED


def is_key_rejected_by_bedrock(error: BaseException) -> bool:
    """True when Bedrock answered 401 or 403.

    Bedrock answers a bad API key with 403 as often as 401. A 403 can also mean
    the account lacks access to the model; dropping the cache then costs one
    extra fetch and nothing else.
    """
    return _status_code(error) in (UNAUTHORIZED, FORBIDDEN)


def is_key_rejected_by_anthropic(error: BaseException) -> bool:
    """True when the Claude API answered 401.

    langchain-anthropic re-raises the SDK's `anthropic.AuthenticationError` as a
    subclass of it, so the `isinstance` check covers both. The status check covers
    an error that reaches the callback without either class.
    """
    return isinstance(error, anthropic.AuthenticationError) or is_unauthorized(error)


class KeyRefreshOnUnauthorized(AsyncCallbackHandler):
    """Drops the cached keys when a chat call is rejected for its key.

    The call that failed is not retried here; the next one reads a fresh key.
    `rejected` decides which errors count, so the OpenRouter, Bedrock and Claude
    API clients can share this class.
    """

    def __init__(
        self,
        provider: LlmApiKeyProvider,
        rejected: Callable[[BaseException], bool] = is_unauthorized,
    ) -> None:
        self._provider = provider
        self._rejected = rejected

    async def on_llm_error(self, error: BaseException, **kwargs: object) -> None:
        if self._rejected(error):
            self._provider.invalidate()


@lru_cache
def get_llm_api_key_provider() -> LlmApiKeyProvider:
    settings = get_settings()
    return LlmApiKeyProvider(
        orchestration_base_url=settings.orchestration_base_url,
        ttl_seconds=settings.llm_key_cache_ttl_seconds,
        timeout_seconds=settings.llm_key_fetch_timeout_seconds,
    )
