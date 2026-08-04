import asyncio
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.llm.usage import (
    UsageBuffer,
    UsageCallback,
    build_usage_http_client,
    set_usage_scope,
)


class RecordingSender:
    """Stands in for the POST; captures each body it was asked to send."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def __call__(self, payload: dict) -> None:
        self.payloads.append(payload)


def _buffer(sender: RecordingSender | None = None, **kwargs) -> UsageBuffer:
    return UsageBuffer(
        base_url="http://orchestration.test",
        send=sender or RecordingSender(),
        **kwargs,
    )


def _chat_result(
    *,
    cost: float | None = 0.021374,
    model: str = "anthropic/claude-sonnet-5",
) -> LLMResult:
    """A finished chat completion shaped the way OpenRouter answers one."""
    token_usage = {
        "prompt_tokens": 12043,
        "completion_tokens": 318,
        "total_tokens": 12361,
    }
    if cost is not None:
        token_usage["cost"] = cost
    message = AIMessage(
        content="done",
        usage_metadata={
            "input_tokens": 12043,
            "output_tokens": 318,
            "total_tokens": 12361,
            "input_token_details": {"cache_read": 10240},
            "output_token_details": {"reasoning": 64},
        },
    )
    return LLMResult(
        generations=[[ChatGeneration(message=message)]],
        llm_output={"token_usage": token_usage, "model_name": model},
    )


async def _feed_chat(callback: UsageCallback, result: LLMResult) -> None:
    run_id = uuid4()
    await callback.on_chat_model_start({}, [], run_id=run_id)
    await callback.on_llm_end(result, run_id=run_id)


# --- chat callback -----------------------------------------------------------


def test_chat_usage_becomes_one_record_in_the_wire_shape() -> None:
    sender = RecordingSender()
    buffer = _buffer(sender, flush_size=1)

    async def scenario() -> None:
        set_usage_scope("QA_RUN", 42)
        await _feed_chat(UsageCallback(buffer), _chat_result())

    asyncio.run(scenario())

    (record,) = sender.payloads[0]["records"]
    assert record["service"] == "QA_RUN"
    assert record["referenceId"] == 42
    assert record["provider"] == "anthropic"
    assert record["model"] == "anthropic/claude-sonnet-5"
    assert record["inputTokens"] == 12043
    assert record["outputTokens"] == 318
    assert record["cachedInputTokens"] == 10240
    assert record["reasoningTokens"] == 64
    assert record["costUsd"] == 0.021374
    assert record["latencyMs"] >= 0
    assert record["calledAt"].endswith("Z")


def test_cost_is_omitted_when_openrouter_did_not_report_one() -> None:
    sender = RecordingSender()
    buffer = _buffer(sender, flush_size=1)

    async def scenario() -> None:
        set_usage_scope("SCENARIO", 7)
        await _feed_chat(UsageCallback(buffer), _chat_result(cost=None))

    asyncio.run(scenario())

    (record,) = sender.payloads[0]["records"]
    # Absent, not zero: zero would read as "the call was free".
    assert "costUsd" not in record
    assert record["inputTokens"] == 12043


# --- embedding hook ----------------------------------------------------------


def _embedding_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                # OpenRouter reports an embedding model without the provider
                # prefix it was asked for — verified against the live endpoint.
                "model": "text-embedding-3-large",
                "data": [{"embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 87, "total_tokens": 87, "cost": 0.000012},
            },
        )

    return httpx.MockTransport(handler)


def test_embedding_response_is_recorded_with_no_output_tokens() -> None:
    sender = RecordingSender()
    buffer = _buffer(sender, flush_size=1)

    async def scenario() -> httpx.Response:
        set_usage_scope("EMBEDDING", 9)
        async with build_usage_http_client(buffer, _embedding_transport()) as client:
            return await client.post("http://openrouter.test/v1/embeddings", json={})

    response = asyncio.run(scenario())

    (record,) = sender.payloads[0]["records"]
    assert record["service"] == "EMBEDDING"
    assert record["referenceId"] == 9
    assert record["inputTokens"] == 87
    assert record["outputTokens"] == 0
    # The prefix-less slug in the response would make provider come out as the
    # model name, so the configured slug is what the record carries.
    assert record["model"] == "openai/text-embedding-3-large"
    assert record["provider"] == "openai"
    assert record["costUsd"] == 0.000012
    # The hook read the body; the SDK must still find it there afterwards.
    assert response.json()["data"] == [{"embedding": [0.1, 0.2]}]


# --- batching ----------------------------------------------------------------


def test_a_full_batch_leaves_as_a_single_request() -> None:
    sender = RecordingSender()
    buffer = _buffer(sender, flush_size=3)

    async def scenario() -> None:
        set_usage_scope("KNOWLEDGE_QUERY", 1)
        callback = UsageCallback(buffer)
        for _ in range(3):
            await _feed_chat(callback, _chat_result())

    asyncio.run(scenario())

    assert len(sender.payloads) == 1
    assert len(sender.payloads[0]["records"]) == 3


def test_shutdown_sends_the_partial_batch() -> None:
    sender = RecordingSender()
    buffer = _buffer(sender, flush_size=10)

    async def scenario() -> None:
        set_usage_scope("GAME_CONTEXT", 5)
        callback = UsageCallback(buffer)
        for _ in range(2):
            await _feed_chat(callback, _chat_result())
        assert sender.payloads == []  # nothing has reached flush_size
        await buffer.stop()

    asyncio.run(scenario())

    assert len(sender.payloads) == 1
    assert len(sender.payloads[0]["records"]) == 2


def test_a_failing_send_drops_its_batch_without_retrying() -> None:
    attempts: list[dict] = []

    async def failing_send(payload: dict) -> None:
        attempts.append(payload)
        raise httpx.ConnectError("orchestration is down")

    buffer = UsageBuffer(base_url="http://orchestration.test", flush_size=1, send=failing_send)

    async def scenario() -> None:
        set_usage_scope("QA_RUN", 42)
        await _feed_chat(UsageCallback(buffer), _chat_result())
        await buffer.stop()

    asyncio.run(scenario())

    # One attempt, and nothing left behind for the shutdown flush to resend:
    # the endpoint has no idempotency key, so a resend would double-count.
    assert len(attempts) == 1


def test_collection_is_off_without_an_orchestration_url() -> None:
    sender = RecordingSender()
    buffer = UsageBuffer(base_url=None, flush_size=1, send=sender)

    async def scenario() -> None:
        set_usage_scope("QA_RUN", 42)
        callback = UsageCallback(buffer)
        await _feed_chat(callback, _chat_result())
        buffer.start()
        await buffer.stop()

    asyncio.run(scenario())

    assert sender.payloads == []


def test_an_unscoped_call_is_dropped_rather_than_guessed_at() -> None:
    sender = RecordingSender()
    buffer = _buffer(sender, flush_size=1)

    async def scenario() -> None:
        # No set_usage_scope: `service` is not nullable on the receiving side.
        await _feed_chat(UsageCallback(buffer), _chat_result())

    asyncio.run(scenario())

    assert sender.payloads == []


def test_a_broken_response_never_reaches_the_caller() -> None:
    sender = RecordingSender()
    buffer = _buffer(sender, flush_size=1)

    async def scenario() -> None:
        set_usage_scope("QA_RUN", 42)
        # No usage_metadata at all — some providers answer this way.
        broken = LLMResult(
            generations=[[ChatGeneration(message=AIMessage("hi"))]],
            llm_output={"token_usage": None},
        )
        await _feed_chat(UsageCallback(buffer), broken)

    asyncio.run(scenario())  # a usage-less completion is a gap, not a failure

    assert sender.payloads == []


def test_the_oldest_records_go_when_the_buffer_fills() -> None:
    sender = RecordingSender()
    # flush_size above max_buffer, so nothing leaves on its own and the cap is
    # what the test observes.
    buffer = _buffer(sender, flush_size=99, max_buffer=2)

    async def scenario() -> None:
        set_usage_scope("QA_RUN", 42)
        for index in range(5):
            await buffer.add({"marker": index})
        await buffer.stop()

    asyncio.run(scenario())

    (payload,) = sender.payloads
    assert [record["marker"] for record in payload["records"]] == [3, 4]
