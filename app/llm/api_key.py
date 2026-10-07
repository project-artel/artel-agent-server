"""Where the LLM key comes from, and how long a fetched value is trusted.

Resolution order, first hit wins:

1. The key an administrator stored in the orchestration server, fetched from
   ``GET {ORCHESTRATION_BASE_URL}/internal/settings/llm``.
2. The ``LLM_API_KEY`` (alias ``OPENROUTER_API_KEY``) environment variable, read
   through ``Settings.llm_api_key``.

The fetched value is cached for ``llm_key_cache_ttl_seconds`` so a model call does
not pay a network round trip, and dropped early when a provider answers 401, so a
key an administrator just replaced takes effect on the next call instead of after
the TTL. Orchestration being down, slow or answering something unexpected is not an
error: the environment key answers, and the failure is cached for one TTL so an
outage costs one short wait per TTL rather than one per model call.

The key itself is never logged. Only the source and the HTTP status are.
"""

import logging
import threading
import time
from collections.abc import Callable
from functools import lru_cache

import httpx
from langchain_core.callbacks import AsyncCallbackHandler

from app.config import get_settings

logger = logging.getLogger(__name__)

SETTINGS_PATH = "/internal/settings/llm"

# What the OpenAI client is handed when no key exists anywhere. The request then
# fails with 401 at the provider, which names the real problem; an empty string
# would fail earlier with an error that does not.
MISSING_KEY_PLACEHOLDER = "missing"

UNAUTHORIZED = 401


class LlmApiKeyProvider:
    """Resolves the LLM key on demand. Safe to share across threads."""

    def __init__(
        self,
        *,
        orchestration_base_url: str | None,
        environment_key: str | None,
        ttl_seconds: float,
        timeout_seconds: float = 3.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = (orchestration_base_url or "").rstrip("/")
        self._environment_key = environment_key or None
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._cached_admin_key: str | None = None
        self._expires_at: float | None = None

    def get(self) -> str | None:
        """The key to use now, or None when neither source has one."""
        return self._admin_key() or self._environment_key

    def get_or_placeholder(self) -> str:
        """Same as `get`, shaped for a client constructor that insists on a string."""
        return self.get() or MISSING_KEY_PLACEHOLDER

    def invalidate(self) -> None:
        """Forget the cached value so the next `get` asks orchestration again."""
        with self._lock:
            self._expires_at = None

    def _admin_key(self) -> str | None:
        if not self._base_url:
            return None
        with self._lock:
            now = self._clock()
            if self._expires_at is not None and now < self._expires_at:
                return self._cached_admin_key
            # Holding the lock across the fetch makes concurrent callers wait for
            # one request instead of each sending their own.
            self._cached_admin_key = self._fetch()
            self._expires_at = self._clock() + self._ttl_seconds
            return self._cached_admin_key

    def _fetch(self) -> str | None:
        url = f"{self._base_url}{SETTINGS_PATH}"
        try:
            with httpx.Client(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = client.get(url)
        except httpx.HTTPError as error:
            logger.warning(
                "[llm-key] orchestration unreachable, using the environment key: %s",
                type(error).__name__,
            )
            return None

        if response.status_code != httpx.codes.OK:
            logger.warning(
                "[llm-key] orchestration answered %s, using the environment key",
                response.status_code,
            )
            return None

        try:
            body = response.json()
        except ValueError:
            logger.warning("[llm-key] orchestration answered a non-JSON body")
            return None

        key = body.get("apiKey") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key.strip():
            return None
        return key.strip()


def is_unauthorized(error: BaseException) -> bool:
    """True when a provider client error is an HTTP 401."""
    return getattr(error, "status_code", None) == UNAUTHORIZED


class KeyRefreshOnUnauthorized(AsyncCallbackHandler):
    """Drops the cached key when a chat call is rejected with 401.

    The call that failed is not retried here; the next one reads a fresh key.
    """

    def __init__(self, provider: LlmApiKeyProvider) -> None:
        self._provider = provider

    async def on_llm_error(self, error: BaseException, **kwargs: object) -> None:
        if is_unauthorized(error):
            self._provider.invalidate()


@lru_cache
def get_llm_api_key_provider() -> LlmApiKeyProvider:
    settings = get_settings()
    return LlmApiKeyProvider(
        orchestration_base_url=settings.orchestration_base_url,
        environment_key=settings.llm_api_key,
        ttl_seconds=settings.llm_key_cache_ttl_seconds,
        timeout_seconds=settings.llm_key_fetch_timeout_seconds,
    )
