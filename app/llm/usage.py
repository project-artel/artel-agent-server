"""Per-call LLM token and cost collection, batched to Orchestration.

Every model call this server makes leaves through OpenRouter, which reports the
token counts and — when the request asks for it — the money it actually charged.
Nothing read those numbers, so LLM spend left no trace. This module reads them
where the calls really happen: a LangChain callback for chat models, an httpx
response hook for embeddings. Middleware would only have covered the two agents
built on ``create_agent``; the structured-output chains and the embedding client
never enter that graph.

Collection is deliberately lossy. The receiving endpoint has no idempotency key,
so a retried batch becomes duplicate rows — a failed send drops its batch and
logs one line, and a backlog past ``max_buffer`` drops its oldest. Accounting
must never be the reason a QA run dies, and must never be the reason this
process runs out of memory either.
"""

import asyncio
import contextlib
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Literal
from uuid import UUID

import httpx
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from app.config import get_settings

logger = logging.getLogger(__name__)

USAGE_PATH = "/internal/llm-usage"

# The receiving side answers 400 above this, so a backlog is cut into requests
# rather than sent as one oversized one.
MAX_RECORDS_PER_REQUEST = 200

Service = Literal["QA_RUN", "SCENARIO", "KNOWLEDGE_QUERY", "GAME_CONTEXT", "EMBEDDING"]


@dataclass(frozen=True)
class UsageScope:
    """What a model call was for, and which row it belongs to."""

    service: Service
    reference_id: int | None = None


# Chat models are @lru_cache'd and shared across calls, so what a call is for
# cannot travel as an argument. A ContextVar rides the asyncio task instead,
# including into the children of an asyncio.gather fan-out.
_scope: ContextVar[UsageScope | None] = ContextVar("llm_usage_scope", default=None)


def set_usage_scope(service: Service, reference_id: int | None = None) -> None:
    """Label every model call made from here on in this task."""
    _scope.set(UsageScope(service, reference_id))


def _isoformat(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _build_record(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    reasoning_tokens: int = 0,
    cost_usd: float | None = None,
    started_at: datetime,
    latency_ms: int,
) -> dict[str, Any] | None:
    """One record in the wire shape, or None when the call has no scope.

    An unscoped call cannot be attributed to anything, and ``service`` is not
    nullable on the receiving side, so it is dropped rather than guessed at.
    """
    scope = _scope.get()
    if scope is None:
        logger.debug("[llm-usage] dropped an unscoped call to %s", model)
        return None

    record: dict[str, Any] = {
        "service": scope.service,
        "referenceId": scope.reference_id,
        # OpenRouter slugs are "<provider>/<model>"; the receiving side wants
        # the two split so it can group spend by vendor.
        "provider": model.split("/")[0],
        "model": model,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cachedInputTokens": cached_input_tokens,
        "reasoningTokens": reasoning_tokens,
        "latencyMs": latency_ms,
        "calledAt": _isoformat(started_at),
    }
    # Omitted rather than zeroed when the provider did not price the call: a 0
    # would read as "this was free" instead of "nobody said".
    if cost_usd is not None:
        record["costUsd"] = cost_usd
    return record


class UsageBuffer:
    """Collects records and posts them in batches; drops rather than retries."""

    def __init__(
        self,
        base_url: str | None = None,
        flush_size: int = 20,
        flush_interval_seconds: float = 5.0,
        max_buffer: int = 1000,
        send: Any = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._flush_size = flush_size
        self._flush_interval = flush_interval_seconds
        self._max_buffer = max_buffer
        self._send = send or self._post
        self._records: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return self._base_url is not None

    async def add(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        async with self._lock:
            self._records.append(record)
            overflow = len(self._records) - self._max_buffer
            if overflow > 0:
                del self._records[:overflow]
                logger.warning(
                    "[llm-usage] buffer full, dropped %d oldest record(s)", overflow
                )
            if len(self._records) < self._flush_size:
                return
            batch = self._take_locked()
        # ponytail: the call that fills a batch pays for shipping it. Hand this
        # to a background task if that latency ever shows up in a run.
        await self._deliver(batch)

    async def flush(self) -> None:
        """Send everything held, however little that is."""
        if not self.enabled:
            return
        while True:
            async with self._lock:
                if not self._records:
                    return
                batch = self._take_locked()
            await self._deliver(batch)

    def start(self) -> None:
        """Begin the periodic flush; safe to call when collection is off."""
        if self.enabled and self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the worker and send what is left.

        The final flush is the point: without it every deploy loses whatever had
        not reached ``flush_size`` yet.
        """
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        await self.flush()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _take_locked(self) -> list[dict[str, Any]]:
        batch = self._records[:MAX_RECORDS_PER_REQUEST]
        del self._records[:MAX_RECORDS_PER_REQUEST]
        return batch

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            await self.flush()

    async def _deliver(self, batch: list[dict[str, Any]]) -> None:
        try:
            await self._send({"records": batch})
        except Exception as error:  # noqa: BLE001 - accounting never propagates
            # No retry on purpose: the endpoint has no idempotency key, so a
            # resend of a batch that did land writes the same spend twice.
            logger.warning(
                "[llm-usage] dropped %d record(s): %s", len(batch), error
            )

    async def _post(self, payload: dict[str, Any]) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)
        response = await self._client.post(
            f"{self._base_url}{USAGE_PATH}", json=payload
        )
        response.raise_for_status()


@lru_cache
def get_usage_buffer() -> UsageBuffer:
    """The process-wide buffer, off entirely without an Orchestration URL."""
    settings = get_settings()
    return UsageBuffer(
        base_url=settings.orchestration_base_url,
        flush_size=settings.llm_usage_flush_size,
        flush_interval_seconds=settings.llm_usage_flush_interval_seconds,
        max_buffer=settings.llm_usage_max_buffer,
    )


class UsageCallback(AsyncCallbackHandler):
    """Reads usage off finished chat completions.

    Nothing raised in here reaches the model call. A missing usage field is an
    accounting gap; an exception would be a failed QA run.
    """

    def __init__(self, buffer: UsageBuffer | None = None) -> None:
        self._buffer = buffer
        # ponytail: a run that ends without either end or error callback leaves
        # its entry here. Bound it (TTL or deque) if that ever adds up — one
        # abandoned run costs a timestamp pair.
        self._started: dict[UUID, tuple[float, datetime]] = {}

    @property
    def _target(self) -> UsageBuffer:
        return self._buffer or get_usage_buffer()

    async def on_chat_model_start(
        self, serialized: dict[str, Any], messages: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._started[run_id] = (time.monotonic(), datetime.now(UTC))

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        # Otherwise a failing model leaks one entry per call for the process life.
        self._started.pop(run_id, None)

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        started = self._started.pop(run_id, None)
        try:
            record = _record_from_response(response, started)
            if record is not None:
                await self._target.add(record)
        except Exception:  # noqa: BLE001 - accounting never propagates
            logger.warning("[llm-usage] could not read chat usage", exc_info=True)


def _record_from_response(
    response: LLMResult, started: tuple[float, datetime] | None
) -> dict[str, Any] | None:
    generations = response.generations[0] if response.generations else []
    message = getattr(generations[0], "message", None) if generations else None
    usage = getattr(message, "usage_metadata", None)
    if usage is None:
        return None

    llm_output = response.llm_output or {}
    # OpenRouter's own usage dict, which carries `cost` only because the request
    # asked for it (extra_body usage.include in chat_model.py).
    raw_usage = llm_output.get("token_usage") or {}
    model = (
        llm_output.get("model_name")
        or (message.response_metadata or {}).get("model_name")
        or ""
    )
    cost = raw_usage.get("cost")
    now = time.monotonic()

    return _build_record(
        model,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cached_input_tokens=(usage.get("input_token_details") or {}).get(
            "cache_read", 0
        ),
        reasoning_tokens=(usage.get("output_token_details") or {}).get("reasoning", 0),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        started_at=started[1] if started else datetime.now(UTC),
        latency_ms=int((now - started[0]) * 1000) if started else 0,
    )


_STARTED_EXTENSION = "artel_llm_usage_started"


def build_usage_http_client(
    buffer: UsageBuffer | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """An httpx client that reports the usage of every response it sees.

    Only ever handed to the embedding client. Chat responses can stream, and
    reading a streaming body out from under the SDK would break it; embeddings
    always come back whole, and httpx caches what ``aread`` pulled so the SDK
    still finds a readable body afterwards.
    """

    async def on_request(request: httpx.Request) -> None:
        # The response hook alone cannot time the call: `response.elapsed` is
        # only set once the body is closed, which is after the hook runs.
        request.extensions[_STARTED_EXTENSION] = (time.monotonic(), datetime.now(UTC))

    async def on_response(response: httpx.Response) -> None:
        try:
            if response.status_code != httpx.codes.OK:
                return
            await response.aread()
            body = response.json()
            usage = body.get("usage") or {}
            started = response.request.extensions.get(_STARTED_EXTENSION)
            cost = usage.get("cost")
            # OpenRouter echoes an embedding model without its provider prefix
            # ("text-embedding-3-large", not "openai/text-embedding-3-large"),
            # and a slug with no "/" makes provider come out as the model name.
            # The configured slug is the one that still carries the vendor.
            reported = body.get("model") or ""
            configured = get_settings().embedding_model
            record = _build_record(
                reported if "/" in reported else configured,
                input_tokens=usage.get("prompt_tokens", usage.get("total_tokens", 0)),
                # An embedding call produces vectors, not tokens.
                output_tokens=0,
                cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
                started_at=started[1] if started else datetime.now(UTC),
                latency_ms=int((time.monotonic() - started[0]) * 1000)
                if started
                else 0,
            )
            if record is not None:
                await (buffer or get_usage_buffer()).add(record)
        except Exception:  # noqa: BLE001 - accounting never propagates
            logger.warning("[llm-usage] could not read embedding usage", exc_info=True)

    return httpx.AsyncClient(
        transport=transport,
        event_hooks={"request": [on_request], "response": [on_response]},
    )
