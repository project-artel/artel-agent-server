"""The bridge between the scenario agent's case-search tool and the WebSocket.

A `search_test_cases` tool call looks synchronous — ask for cases, get cases. The
transport is not: the request goes out on the authoring session's socket as a
`test_case_search` frame and the answer arrives later as a separate inbound
`test_case_search_result` frame. This holds the future that makes one look like
the other, so `app/agents/scenario/tools.py` can simply `await`.

This mirrors `app/qa/channel.py`'s `search_knowledge` mechanics deliberately.
The difference is the wire: the QA session speaks the qa_try_id envelope
(`app/qa/envelope.py`); the authoring session speaks its own flat JSON frames
(`{"type": "turn"...}` / `{"type": "result"...}`), so the frames here are flat
too and this module owns their shapes rather than reaching into the QA envelope.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

# How long a case search waits before the agent is told nobody answered.
#
# Sized like the QA knowledge search: the round trip is not free on the far side
# (Orchestration resolves the run scope and queries its TestCase store), but a
# search only buys context for a decomposition the agent can also make without
# it, so it must never be the reason a turn hangs. Returned as a value, not
# raised — silence is one of three outcomes the tool decides between.
TEST_CASE_SEARCH_TIMEOUT_SECONDS = 20.0

Send = Callable[[dict], Awaitable[None]]


class TestCaseHit(BaseModel):
    """One TestCase the search matched.

    Fields mirror the orchestration TestCase model. `id` is a string on the wire
    (numeric on the far side); the plan the agent returns carries case ids as
    ints. Everything but `id` and `score` is defaulted so a lean result frame
    cannot fail to parse and drop the whole answer.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    category: str = ""
    title: str = ""
    precondition: str | None = None
    expected: str = ""
    verification_status: str = Field(default="", alias="verificationStatus")
    score: float = 0.0


class TestCaseSearchResult(BaseModel):
    """The `test_case_search_result` frame's body: the hits, newest scope first."""

    results: list[TestCaseHit] = Field(default_factory=list)


@dataclass(frozen=True)
class TestCaseSearchFailed:
    """Orchestration answered the search with an `error` frame.

    Distinct from an empty result (an ordinary answer: no case matched) and from
    no answer at all (a timeout). Only this one means the search itself could not
    run — a bad scope, a store that would not answer.
    """

    reason: str


class ScenarioChannel:
    """Sends case-search frames for one authoring session and resolves the reply.

    One channel per WebSocket connection. Only one turn runs at a time (a turn
    arriving mid-turn is rejected as busy by the handler), so a single waiter is
    enough — there is never a second search in flight to confuse it with.
    """

    def __init__(
        self,
        send: Send,
        search_timeout: float = TEST_CASE_SEARCH_TIMEOUT_SECONDS,
    ) -> None:
        self._send = send
        self._search_timeout = search_timeout
        self._search_waiter: (
            asyncio.Future[TestCaseSearchResult | TestCaseSearchFailed] | None
        ) = None
        self._pending_search_id: str | None = None

    # --- outbound -------------------------------------------------------------

    async def search_test_cases(
        self, query: str, category: str | None, limit: int
    ) -> TestCaseSearchResult | TestCaseSearchFailed | None:
        """Ask the run's TestCases a question. Three outcomes, kept apart.

        A payload (whose `results` may be empty, which is a real answer: nothing
        matched), a refusal (`TestCaseSearchFailed`), or silence (`None`).
        Returned rather than raised, for the same reason the QA search does it: a
        case lookup is a side errand to authoring, and none of these is a reason
        to fail the turn.

        `project_id`/`run_id` are deliberately NOT in the frame: Orchestration
        resolves the scope from the session binding, the same principle as the
        knowledge search.
        """
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[TestCaseSearchResult | TestCaseSearchFailed] = (
            loop.create_future()
        )
        self._search_waiter = waiter
        message_id = str(uuid4())
        self._pending_search_id = message_id
        await self._send(
            {
                "type": "test_case_search",
                "messageId": message_id,
                "query": query,
                "category": category,
                "limit": limit,
            }
        )
        try:
            return await asyncio.wait_for(waiter, timeout=self._search_timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._search_waiter = None
            self._pending_search_id = None

    # --- inbound --------------------------------------------------------------

    def deliver(self, raw: dict) -> bool:
        """Resolve the in-flight search from an inbound frame.

        True when the frame was a case-search reply this channel understands
        (answered or stale), so the handler does not report it as an unsupported
        frame. False when the type is unknown or the payload could not be read —
        an unreadable reply must not raise out through the socket and end the
        session, exactly as `app/qa/service.py:deliver` guards its own inbound.
        """
        message_type = raw.get("type")
        try:
            if message_type == "test_case_search_result":
                if self._answers_pending(raw):
                    self._resolve(TestCaseSearchResult.model_validate(raw))
                return True
            if message_type == "error":
                # A legitimate frame either way. Correlated, it releases the
                # search as a failure; uncorrelated, nothing is waiting for it, so
                # it is logged and dropped rather than reported back as a fault.
                if self._answers_pending(raw):
                    self._resolve(TestCaseSearchFailed(reason=_error_reason(raw)))
                else:
                    logger.warning(
                        "[scenario] inbound error answered no pending search: %r", raw
                    )
                return True
        except ValidationError as error:
            logger.warning(
                "[scenario] dropped an unreadable %s frame: %s\n  frame: %r",
                message_type,
                error,
                raw,
            )
            return False
        return False

    def _answers_pending(self, raw: dict) -> bool:
        """Whether this frame answers the search currently in flight.

        The reply echoes the request's messageId as its correlationId, so a
        mismatch means it answers something we no longer wait on — a search
        abandoned at its timeout, whose late reply must not resolve the next one.
        """
        if self._pending_search_id is None:
            return False
        return raw.get("correlationId") == self._pending_search_id

    def _resolve(
        self, answer: TestCaseSearchResult | TestCaseSearchFailed
    ) -> None:
        if self._search_waiter is not None and not self._search_waiter.done():
            self._search_waiter.set_result(answer)


def _error_reason(raw: dict) -> str:
    """Best-effort reason from an inbound error frame.

    The authoring session's own error frames carry `detail`; a generic one may
    carry `message`. Read field by field rather than through a model so the frame
    that exists to unblock a waiting tool is never dropped for a missing key.
    """
    for key in ("detail", "message"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return "no reason given"
