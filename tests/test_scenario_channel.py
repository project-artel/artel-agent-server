"""The case-search channel: correlation, and the three search outcomes.

Mirrors `tests/test_qa_channel.py`'s coverage of `search_knowledge` for the
scenario session's `search_test_cases`.
"""

import asyncio

from app.sessions.channel import (
    ScenarioChannel,
    TestCaseSearchFailed,
    TestCaseSearchResult,
)


def make_channel(timeout: float = 1.0) -> tuple[ScenarioChannel, list[dict]]:
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    return ScenarioChannel(send, search_timeout=timeout), sent


def test_search_goes_out_as_a_test_case_search_frame() -> None:
    async def run() -> None:
        channel, sent = make_channel()

        async def answer() -> None:
            await asyncio.sleep(0)
            channel.deliver(
                {
                    "type": "test_case_search_result",
                    "correlationId": sent[0]["messageId"],
                    "results": [
                        {
                            "id": "42",
                            "category": "SHOP",
                            "title": "Buy with gold",
                            "precondition": "Has 100 gold",
                            "expected": "Item is purchased",
                            "verificationStatus": "VERIFIED",
                            "score": 0.9,
                        }
                    ],
                }
            )

        asyncio.create_task(answer())
        result = await channel.search_test_cases("buy item", None, 10)

        assert isinstance(result, TestCaseSearchResult)
        assert result.results[0].id == "42"
        assert result.results[0].verification_status == "VERIFIED"
        assert sent[0]["type"] == "test_case_search"
        assert sent[0]["query"] == "buy item"
        assert sent[0]["category"] is None
        assert sent[0]["limit"] == 10

    asyncio.run(run())


def test_an_error_frame_resolves_the_search_as_failed() -> None:
    async def run() -> None:
        channel, sent = make_channel()

        async def answer() -> None:
            await asyncio.sleep(0)
            channel.deliver(
                {
                    "type": "error",
                    "correlationId": sent[0]["messageId"],
                    "detail": "scope not found",
                }
            )

        asyncio.create_task(answer())
        result = await channel.search_test_cases("q", None, 5)

        assert isinstance(result, TestCaseSearchFailed)
        assert result.reason == "scope not found"

    asyncio.run(run())


def test_silence_resolves_to_none() -> None:
    async def run() -> None:
        channel, _ = make_channel(timeout=0.05)
        assert await channel.search_test_cases("q", None, 5) is None

    asyncio.run(run())


def test_a_foreign_correlation_does_not_resolve_the_search() -> None:
    async def run() -> None:
        channel, _ = make_channel(timeout=0.05)

        async def answer() -> None:
            await asyncio.sleep(0)
            # Belongs to some earlier search; must not resolve this one.
            channel.deliver(
                {
                    "type": "test_case_search_result",
                    "correlationId": "someone-else",
                    "results": [],
                }
            )

        asyncio.create_task(answer())
        # Times out rather than resolving on the stale frame.
        assert await channel.search_test_cases("q", None, 5) is None

    asyncio.run(run())


def test_deliver_reports_known_and_unknown_types() -> None:
    channel, _ = make_channel()

    # Known reply types are accepted even with nothing in flight (stale, dropped).
    assert channel.deliver({"type": "test_case_search_result", "results": []}) is True
    assert channel.deliver({"type": "error", "detail": "x"}) is True
    # An unknown type is not this channel's to handle.
    assert channel.deliver({"type": "turn"}) is False


def test_deliver_drops_an_unreadable_result_frame() -> None:
    """A malformed reply must be rejected, not raise out of the socket."""

    async def run() -> None:
        channel, sent = make_channel(timeout=0.05)
        # Put a search in flight so the frame is matched and validated.
        task = asyncio.create_task(channel.search_test_cases("q", None, 5))
        await asyncio.sleep(0)

        handled = channel.deliver(
            {
                "type": "test_case_search_result",
                "correlationId": sent[0]["messageId"],
                "results": [{"category": "SHOP"}],  # missing required `id`
            }
        )
        assert handled is False
        # The waiter was not resolved by the bad frame, so it still times out.
        assert await task is None

    asyncio.run(run())
