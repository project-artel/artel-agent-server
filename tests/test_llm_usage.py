import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.llm.usage import (
    USAGE_PATH,
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
            # **실제 Bedrock 응답의 모양이다.** `cache_creation` 을 만들어 두고 거기에 0 을
            # 넣은 채 실제 쓰기량을 TTL 이 박힌 이름으로 보낸다. 종전 픽스처는
            # `cache_creation` 에 값을 넣은 모양이라, 현실에 없는 응답으로 통과하고 있었다
            # (ARTEL-793).
            "input_token_details": {
                "cache_read": 10240,
                "cache_creation": 0,
                "ephemeral_5m_input_tokens": 512,
            },
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
    assert record["cacheWriteTokens"] == 512
    assert record["reasoningTokens"] == 64
    assert record["costUsd"] == 0.021374
    # provider 가 청구한 값이다. 계산으로 덮지 않는다.
    assert record["costEstimated"] is False
    assert record["latencyMs"] >= 0
    assert record["calledAt"].endswith("Z")


def test_cache_write_is_read_from_whichever_name_the_provider_used() -> None:
    """이름이 provider 마다 다르다. 아는 것을 순서대로 보고 하나만 쓴다.

    Bedrock 은 둘 다 실어 보내면서 `cache_creation` 에 0 을 넣는다. 더하면 같은 토큰을
    두 번 세므로, 값이 있는 첫 이름 하나를 쓴다.
    """
    from app.llm.usage import _cache_write_of

    # 실측 모양 — 0 인 `cache_creation` 을 건너뛰고 TTL 이름을 읽는다.
    assert _cache_write_of(
        {"input_token_details": {"cache_creation": 0, "ephemeral_5m_input_tokens": 1729}}
    ) == 1729
    # 그 이름으로 주는 provider 도 있을 수 있다. 분기를 늘리지 않고 둘 다 본다.
    assert _cache_write_of({"input_token_details": {"cache_creation": 512}}) == 512
    # 아무 이름도 없으면 0 이다. 캐시를 안 쓴 호출이 그 모양이다.
    assert _cache_write_of({"input_token_details": {"cache_read": 1}}) == 0
    assert _cache_write_of({}) == 0


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
    # 금액이 없으면 출처도 없다. 받는 쪽이 둘의 유무를 묶어 검사한다.
    assert "costEstimated" not in record
    assert record["inputTokens"] == 12043


def test_a_bedrock_call_is_priced_from_the_catalog_when_nobody_billed_it() -> None:
    """Bedrock 은 청구액을 안 싣는다. 그 자리를 카탈로그 단가가 메운다.

    `cached_input_tokens` 는 `input_tokens` 에 **포함된** 값이라 정가에서 빼고 캐시 단가로
    다시 센다. 안 빼면 캐시로 아낀 만큼이 두 번 청구된 것으로 나온다.
    """
    sender = RecordingSender()
    buffer = _buffer(sender, flush_size=1)

    async def scenario() -> None:
        set_usage_scope("QA_RUN", 5)
        await _feed_chat(
            UsageCallback(buffer),
            _chat_result(
                cost=None,
                model="bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
            ),
        )

    asyncio.run(scenario())

    (record,) = sender.payloads[0]["records"]
    # input 12,043 중 10,240 이 캐시 읽기, 정가 대상은 1,803. 캐시 쓰기 512, 출력 318.
    expected = (
        1_803 * 1.00 + 10_240 * 0.10 + 512 * 1.25 + 318 * 5.00
    ) / 1_000_000
    assert record["costUsd"] == pytest.approx(expected)
    # 우리가 계산한 값이다. provider 가 청구한 값과 한 칸에 섞이면 안 된다.
    assert record["costEstimated"] is True


def test_an_unpriced_model_leaves_the_cost_empty_rather_than_guessing() -> None:
    """단가를 모르는 모델은 빈 칸으로 남는다.

    단가표는 자기가 낡은 것을 모른다. 모르는 모델에 아무 수나 세우면 그 숫자는
    틀렸는데 그럴듯해서 아무도 의심하지 않는다. 빈 칸은 "아무도 말 안 했다" 로
    정확히 읽힌다.
    """
    sender = RecordingSender()
    buffer = _buffer(sender, flush_size=1)

    async def scenario() -> None:
        set_usage_scope("QA_RUN", 3)
        await _feed_chat(
            UsageCallback(buffer),
            _chat_result(cost=None, model="vendor/a-model-nobody-priced"),
        )

    asyncio.run(scenario())

    (record,) = sender.payloads[0]["records"]
    assert "costUsd" not in record
    assert "costEstimated" not in record
    # 토큰은 그대로 남는다 — 값을 못 매긴 것이지 못 잰 것이 아니다.
    assert record["inputTokens"] == 12043
    assert record["cacheWriteTokens"] == 512


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


def test_post_sends_to_the_internal_llm_usage_path() -> None:
    """The real `_post` (no `send=` injection) must target USAGE_PATH.

    Every other test injects `send=`, which bypasses `_post` and the URL it
    builds from `USAGE_PATH` entirely. This is the only test that exercises
    that assembly, so it is what actually catches the path drifting.
    """
    buffer = UsageBuffer(base_url="http://orchestration.test")
    # `raise_for_status` needs a request attached to the response, same as a
    # real httpx transport would set.
    ok_response = httpx.Response(
        200, request=httpx.Request("POST", "http://orchestration.test")
    )

    async def scenario() -> AsyncMock:
        with patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(return_value=ok_response),
        ) as mock_post:
            await buffer._post({"records": []})
        await buffer.stop()
        return mock_post

    mock_post = asyncio.run(scenario())

    assert USAGE_PATH == "/internal/llm-usage"
    mock_post.assert_awaited_once_with(
        "http://orchestration.test/internal/llm-usage", json={"records": []}
    )


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


def test_a_bedrock_model_keeps_its_vendor_in_provider() -> None:
    """Bedrock 은 vendor 접두를 잃은 이름으로 답한다.

    `build_chat_model` 이 `bedrock/` 을 떼고 profile ID 만 클라이언트에 넘기므로,
    응답의 `model_name` 에는 슬래시가 없다. 그대로 두면 `provider` 가 모델 이름
    전체(43자)가 되어 받는 쪽의 `VARCHAR(40)` 을 넘고, 그 런의 사용량이 통째로
    버려진다 — 실제로 stage 에서 그렇게 됐다.

    카탈로그 값을 들고 있으면 그것으로 되살릴 수 있다.
    """
    sender = RecordingSender()
    buffer = _buffer(sender, flush_size=1)
    profile = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    async def scenario() -> None:
        set_usage_scope("QA_RUN", 7)
        await _feed_chat(
            UsageCallback(buffer, slug=f"bedrock/{profile}"),
            _chat_result(cost=None, model=profile),
        )

    asyncio.run(scenario())

    (record,) = sender.payloads[0]["records"]
    assert record["provider"] == "bedrock"
    assert record["model"] == f"bedrock/{profile}"
    assert len(record["provider"]) <= 40
