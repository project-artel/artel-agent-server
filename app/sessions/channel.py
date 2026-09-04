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

from app.agents.scenario.schemas import CaseGuard

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


class UncoveredScene(BaseModel):
    """미커버가 남은 씬 하나와 건수. 사람이 아는 말로 답하기 위한 축이다."""

    scene: str = ""
    count: int = 0


class UncoveredCases(BaseModel):
    """The `uncovered_cases_result` frame: which cases no scenario has reached yet.

    Fetched rather than pushed. This shrinks as authoring covers cases, so a value
    sent at session open is wrong by the second turn, and a value re-sent every turn
    either bloats the turn message or (worse, if it sat in the system prompt) throws
    the whole cached case list away. A tool call pays only when someone asks.
    """

    ids: list[int] = Field(default_factory=list)
    scenes: list[UncoveredScene] = Field(default_factory=list)


class ScenarioPath(BaseModel):
    """The `find_path_result` frame: what is needed between two cases.

    Three answers to one question — is a step needed in between?

        KNOWN         yes, and these capabilities are it
        NOT_REQUIRED  no, they follow directly
        UNKNOWN       yes, but the route is not known

    The third is half of why this exists. The scene spec comes from evidence and
    observation, neither of which is exhaustive, so a missing edge does not mean
    the route is absent — it means nobody has recorded it. `blocked_by` names what
    stands in the way (a scene pair or a variable), and that name is what the user
    can be asked about.
    """

    result: str = "UNKNOWN"
    capability_ids: list[int] = Field(default_factory=list, alias="capabilityIds")
    actions: list[str] = Field(default_factory=list)
    # The same operations, normalized for machines: `key:Return`, `click:Canvas/Start`.
    # Written into a step's `input` verbatim — nobody should re-derive an operation by
    # parsing the sentence, because then rewording a step breaks whoever runs it.
    inputs: list[str] = Field(default_factory=list)
    # Whether the two cases chain in the order asked. `REVERSED` means they chain the
    # other way round: the cases themselves declare the states, so this is a fact about
    # them, not a preference. Orchestration will still fill the gap — but a scenario that
    # runs is not the same as one that verifies what the cases meant.
    ordering: str = "NO_OPINION"
    blocked_by: str | None = Field(default=None, alias="blockedBy")
    # 조작으로 지시할 수는 없어도 사람은 지나갈 수 있는가. `UNKNOWN` 이 서로 다른 두 상황을
    # 덮고 있어 이 칸이 그것을 가른다 — 그 화면에 서 있으면 저절로 바뀌는 값(전투를 이긴다,
    # 컷신을 끝까지 본다)과, 아무 데서도 안 바뀌는 값이다. 앞엣것은 시나리오를 실행하는 사람이
    # 멈춰 서는 자리이고, 뒤엣것은 사용자에게 물을 것이다. 무엇이 어디서 바뀌는지는 `note` 가
    # 스텝 옆에 적을 문장으로 말해 준다.
    playable: bool = False
    note: str = ""

    model_config = ConfigDict(populate_by_name=True)


class CaseOperation(BaseModel):
    """One operation a case is made of, as the scene spec records it."""

    capability_id: int = Field(alias="capabilityId")
    input: str
    label: str | None = None
    summary: str = ""
    given: str | None = None
    status: str = ""
    # `evidence` — the code this case points at. `effect` — a capability that touches the
    # same value, so there may be several. Collapsing the two would hide the difference
    # between "exactly this" and "probably one of these".
    matched_by: str = Field(default="", alias="matchedBy")

    model_config = ConfigDict(populate_by_name=True)


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
        self._uncovered_waiter: asyncio.Future[UncoveredCases] | None = None
        self._pending_uncovered_id: str | None = None
        self._path_waiter: asyncio.Future[ScenarioPath] | None = None
        self._pending_path_id: str | None = None
        self._pending_facts_id: str | None = None

    # --- outbound -------------------------------------------------------------

    async def fetch_uncovered(self) -> UncoveredCases | None:
        """Ask which cases nothing has covered yet. `None` when nobody answered.

        Scope comes from the session binding on the far side, like the case search —
        the frame carries no project or run id.
        """
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[UncoveredCases] = loop.create_future()
        self._uncovered_waiter = waiter
        message_id = str(uuid4())
        self._pending_uncovered_id = message_id
        await self._send({"type": "uncovered_cases", "messageId": message_id})
        try:
            return await asyncio.wait_for(waiter, timeout=self._search_timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._uncovered_waiter = None
            self._pending_uncovered_id = None

    async def fetch_path(self, from_case_id: int, to_case_id: int) -> ScenarioPath | None:
        """Ask what is needed between two cases. `None` when nobody answered.

        The route is computed on the far side, not here. Reading a graph and
        walking it are the kind of work a model does badly — measured: handing the
        same scene spec to the model as prompt text left it *worse* than having
        none at all, and moving the walk behind this call took it to zero.

        Scope comes from the session binding, like the case search — the frame
        carries no project or run id.
        """
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[ScenarioPath] = loop.create_future()
        self._path_waiter = waiter
        message_id = str(uuid4())
        self._pending_path_id = message_id
        await self._send({
            "type": "find_path",
            "messageId": message_id,
            "from_case_id": from_case_id,
            "to_case_id": to_case_id,
        })
        try:
            return await asyncio.wait_for(waiter, timeout=self._search_timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._path_waiter = None
            self._pending_path_id = None

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

    async def report(self, stage: str) -> None:
        """Say where the turn is. Fire-and-forget: nothing waits for an answer.

        The far side sees every tool call as a frame, but not the model turns in
        between — and those are most of the wall clock. Without this, a turn that
        thinks for forty seconds and calls one tool looks the same as one that
        died right after the tool.

        Never raises. A progress line is worth less than the turn it would kill,
        and a socket that has gone away will fail again on the result frame, where
        the failure actually means something.
        """
        try:
            await self._send({"type": "progress", "stage": stage})
        except Exception:  # noqa: BLE001 - see docstring
            logger.debug("[scenario] progress frame dropped (%s)", stage, exc_info=True)

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
            if message_type == "uncovered_cases_result":
                waiter = self._uncovered_waiter
                if (
                    waiter is not None
                    and not waiter.done()
                    and raw.get("correlationId") == self._pending_uncovered_id
                ):
                    waiter.set_result(UncoveredCases.model_validate(raw))
                return True
            if message_type == "find_path_result":
                waiter = self._path_waiter
                if (
                    waiter is not None
                    and not waiter.done()
                    and raw.get("correlationId") == self._pending_path_id
                ):
                    waiter.set_result(ScenarioPath.model_validate(raw))
                return True
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
