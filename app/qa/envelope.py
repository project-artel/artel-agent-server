"""QA WebSocket envelope: the Agent <-> Orchestration wire contract.

One envelope shape for every frame; the `type` selects the payload. Agent-facing
only — Orchestration owns the SDK JSON-RPC translation, qa_log, and SSE. See the
"QA Agent Session and WebSocket Protocol Plan".
"""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class MessageType(StrEnum):
    # Orchestration -> Agent
    GAME_STATE = "GAME_STATE"
    ACTION_RESULT = "ACTION_RESULT"
    CANCEL = "CANCEL"
    # The answer to a KNOWLEDGE_SEARCH, correlated by that frame's messageId.
    KNOWLEDGE_SEARCH_RESULT = "KNOWLEDGE_SEARCH_RESULT"
    # Agent -> Orchestration
    LOG = "LOG"
    ACTION = "ACTION"
    STATUS = "STATUS"
    # Asks the project's knowledge base a question. This name and the result type
    # above are spelled exactly as Orchestration's QaAgentInboundRouter expects
    # them: it rejects an unknown type outright, and that rejection reaches the
    # waiting tool as silence rather than as an error it could report.
    KNOWLEDGE_SEARCH = "KNOWLEDGE_SEARCH"
    # Writes to the project's knowledge base: one new entry, and one soft delete.
    #
    # Unlike the search, these are ONE-WAY. Orchestration's `routeKnowledgeMutation`
    # answers a mutation with no frame at all — a success is silent, and a rejection
    # becomes an ORCHE_INTERNAL row on the run's own timeline, published to the
    # operator's stream rather than back down this socket. Nothing on this side may
    # wait for a reply to one of these.
    #
    # Orchestration also accepts KNOWLEDGE_UPDATE. It is deliberately absent here:
    # this agent corrects an entry by deleting it and recording the corrected
    # version, so there is no tool that could put that frame on the wire
    # (ARTEL-189). The Orchestration path stays for other callers.
    KNOWLEDGE_CREATE = "KNOWLEDGE_CREATE"
    KNOWLEDGE_DELETE = "KNOWLEDGE_DELETE"
    # One defect the run found in the game, filed against this run.
    #
    # ONE-WAY, like the knowledge writes above: Orchestration's `routeIssue`
    # answers nothing. Worse, it drops the frame silently when `payload.severity`
    # is not on its ladder or `payload.title` is blank — the rejection becomes a
    # row on the operator's timeline, not a reply down this socket. Nothing here
    # may wait for an answer, and `report_issue` validates severity itself
    # precisely because a typo would otherwise look like a successful report.
    ISSUE = "ISSUE"
    # Bidirectional
    ERROR = "ERROR"
    # Bidirectional. The operator talking to the Agent mid-run, and its reply.
    # One type both ways; the envelope's direction is what names the speaker.
    CHAT = "CHAT"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class LogCategory(StrEnum):
    THOUGHT = "THOUGHT"
    OBSERVATION = "OBSERVATION"
    SYSTEM = "SYSTEM"
    VALIDATION = "VALIDATION"


class StepStatus(StrEnum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunResult(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class ActionItemStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


# --- Inbound payloads (Orchestration -> Agent) --------------------------------
# `extra="allow"`: the scene carries richer, game-specific data (each element's
# invokable actions/methods and states). The Agent grounds the step's natural
# language against exactly that data at runtime, so it must be PRESERVED and
# forwarded, never stripped. Named fields below are just the always-present ones.


class Rect(BaseModel):
    """Where an element sits on the game's screen, in pixels.

    Origin is the TOP-LEFT of the screen and `x`/`y` are the element's own
    top-left corner — the same coordinates the SDK's `move_mouse` takes, which
    flips into Unity's bottom-left screen space itself. Nothing here converts.
    """

    model_config = ConfigDict(extra="allow")

    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        """The point to aim the pointer at. Derived here so no reader re-does it."""
        return self.x + self.w // 2, self.y + self.h // 2


class Screen(BaseModel):
    model_config = ConfigDict(extra="allow")

    w: int
    h: int


class Interactable(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    name: str
    type: str
    label: str | None = None
    placeholder: str | None = None
    # Both absent from an Orchestration server older than the coordinate relay,
    # so neither may be required. `onScreen` defaults to true: an element the
    # scene bothered to list is on screen unless it says otherwise.
    rect: Rect | None = None
    onScreen: bool = True


class Visual(BaseModel):
    """Something on screen the scene does not offer as an interactable.

    Backgrounds, portraits, sprites — no id worth clicking, but a position worth
    aiming at: the pointer tools reach anything with a rect. `rect` and `onScreen`
    carry the same meaning as on `Interactable`, so one formatter serves both.
    """

    model_config = ConfigDict(extra="allow")

    id: int
    name: str
    # `image` for a uGUI Image, `sprite` for a SpriteRenderer.
    type: str
    # The sprite asset's name; absent when the element has none, as a flat-colour
    # Image does.
    sprite: str | None = None
    rect: Rect | None = None
    onScreen: bool = True


class ActionRecord(BaseModel):
    """One action the game actually ran, as the scene reported it.

    Distinct from ACTION_RESULT: that answers "did the action I dispatched
    succeed", this also covers actions the game ran on its own, which the Agent
    would otherwise have no way to observe.
    """

    model_config = ConfigDict(extra="allow")

    target: str
    name: str
    success: bool
    returnValue: Any | None = None
    error: str | None = None
    at: str


class GameState(BaseModel):
    model_config = ConfigDict(extra="allow")

    scene: str
    screen: Screen | None = None
    interactables: list[Interactable] = Field(default_factory=list)
    # Everything else on screen. Disjoint from `interactables` by construction —
    # an element listed there never repeats here. Empty on an Orchestration
    # server older than the visuals relay.
    visuals: list[Visual] = Field(default_factory=list)
    # Observable state/content values, keyed by name; opaque to the Agent server.
    observables: dict[str, Any] = Field(default_factory=dict)
    # Oldest first, capped by Orchestration.
    recentActions: list[ActionRecord] = Field(default_factory=list)


class ActionResultItem(BaseModel):
    """One action's outcome, as the SDK reports it.

    The SDK sends `{id, success, error}` — a boolean, not the status enum this
    once expected. Every ACTION_RESULT failed validation because of it.
    """

    model_config = ConfigDict(extra="allow")

    id: int
    success: bool = False
    error: str | None = None

    # What the action produced, for the actions that produce something. Absent on
    # every action that does not, which is why it cannot be required. `capture_screen`
    # puts the uploaded image's URL here; the bytes never travel over this socket.
    returnValue: dict | None = None

    @property
    def status(self) -> ActionItemStatus:
        return ActionItemStatus.SUCCEEDED if self.success else ActionItemStatus.FAILED


class ActionResultPayload(BaseModel):
    message: str | None = None
    batchId: int | None = None
    results: list[ActionResultItem] = Field(default_factory=list)


class KnowledgeSearchHit(BaseModel):
    """One piece of project knowledge the search matched.

    Every field defaults, and unknown ones are kept. A hit that failed validation
    would take the whole answer down with it, and the tool that asked would then
    sit until its own timeout — the run paying twenty seconds for a renamed
    field. A missing summary reads as an empty one instead.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    # One of CONTROL|RULE|OBJECTIVE|UI|MISC. Left as a plain string rather than an
    # enum: Orchestration owns that vocabulary, and a value added there must not
    # make this side drop the hit.
    tag: str = ""
    source: str = ""
    summary: str = ""
    description: str = ""
    # Cosine similarity, so higher is closer. Orchestration flips pgvector's
    # distance before sending precisely so nobody downstream has to remember which
    # direction is good.
    score: float = 0.0


class KnowledgeSearchResultPayload(BaseModel):
    """The answer to one KNOWLEDGE_SEARCH.

    An empty `results` is a normal answer, not a failure: the embedding backfill
    runs asynchronously, so knowledge that exists may have no vector yet.
    """

    model_config = ConfigDict(extra="allow")

    query: str = ""
    # Which embedding model the search ran under. Kept because a search that keeps
    # coming back empty is diagnosed by comparing this against the Agent's own
    # embedding configuration.
    model: str = ""
    results: list[KnowledgeSearchHit] = Field(default_factory=list)


class CancelPayload(BaseModel):
    message: str | None = None
    reason: str | None = None


class ChatPayload(BaseModel):
    """One operator turn on the way in, one Agent turn on the way out."""

    message: str
    step: int | None = None


class QaChatTurn(BaseModel):
    """One recorded turn of the operator conversation.

    Lives here, beside the wire types, rather than in the agent package: both the
    session record and the agent request need it, and `app.qa` importing
    `app.agents.qa` would close an import cycle (see `app/qa/__init__.py`).
    """

    role: Literal["USER", "AGENT"]
    message: str
    step: int | None = None


# --- Outbound payloads (Agent -> Orchestration) -------------------------------


class LogPayload(BaseModel):
    level: LogLevel = LogLevel.INFO
    category: LogCategory
    message: str
    step: int | None = None


class JsonRpcAction(BaseModel):
    """One SDK JSON-RPC 2.0 action item carried inside an ACTION frame."""

    id: int
    jsonrpc: str = "2.0"
    method: str
    params: list = Field(default_factory=list)


class ActionPayload(BaseModel):
    message: str
    step: int | None = None
    actions: list[JsonRpcAction] = Field(default_factory=list)


class KnowledgeSearchPayload(BaseModel):
    """A question for the project's knowledge base.

    The project is deliberately absent. Orchestration resolves the search's scope
    from the run itself (`qaTryId -> gameInstanceId -> projectId`), so no frame
    from here can read another project's knowledge.
    """

    query: str
    # Singular, though Orchestration accepts a `tags` list too. The tool offers one
    # topic because one question has one topic, and a result's own `tag` is
    # singular — so a value read off a hit can be fed straight back as a filter.
    tag: str | None = None
    limit: int


class KnowledgeCreatePayload(BaseModel):
    """One fact the run learned, to be filed against this project.

    No project and no source travel with it. Orchestration fixes both from the run
    itself — `qaTryId -> gameInstanceId -> projectId`, `source=QA`,
    `source_id=qa_try.id` — which is what stops one run's frame from writing into
    another project's knowledge base.
    """

    tag: str
    summary: str
    description: str


class KnowledgeDeletePayload(BaseModel):
    """The soft delete of one existing entry.

    Split from the create rather than sharing Orchestration's single
    `KnowledgeMutationRequest`, because on this side the two have no field in
    common: a create never names an id, and a delete never carries a body. One
    model would make every field optional and leave which ones are required to a
    comment.

    `knowledge_id` is a string, and matches the wire name Orchestration maps with
    `@JsonProperty("knowledge_id")`. Ids leave Orchestration as text so a 64-bit
    value cannot lose precision on the way through JSON, and it comes back the
    same way.
    """

    knowledge_id: str


class StatusPayload(BaseModel):
    status: StepStatus
    # Always present: null for per-step frames, PASSED|FAILED only on the
    # terminal run frame — so Orchestration can read it uniformly.
    result: RunResult | None = None
    step: int | None = None
    message: str
    summary: dict | None = None


class IssueSeverity(StrEnum):
    """How badly the defect hurts, worst first.

    Spelled exactly as Orchestration's own `IssueSeverity`: it checks the value
    against that ladder and drops the whole frame when it does not match. A value
    added on that side has to be added here before this agent can send it.
    """

    BLOCKER = "BLOCKER"
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    TRIVIAL = "TRIVIAL"


class IssuePayload(BaseModel):
    """One defect, as the run observed it.

    `title` carries the display line — the one field name that differs from every
    other frame, which uses `message`. Orchestration requires it non-blank.

    The rest is not validated on the far side; it is stored whole in `issue.detail`.
    That is the reason to name the fields here rather than accept free text: an
    issue whose expected/actual and steps are dissolved into one paragraph cannot
    be read back as a bug report months later.
    """

    title: str
    severity: IssueSeverity
    # Required, unlike the `step` on every other payload here: the agent is always
    # working on some step when it notices a defect, even when that step passes,
    # and an issue nobody can place on the timeline is a bug report without a
    # location.
    step: int
    expected: str
    actual: str
    # Oldest first. What someone else has to do to see this again.
    reproduction: list[str] = Field(default_factory=list)


class ErrorPayload(BaseModel):
    message: str
    code: str
    retryable: bool = False
    detail: dict | None = None


def outbound_envelope(
    message_type: MessageType,
    qa_try_id: int,
    sequence: int,
    payload: BaseModel,
    correlation_id: str | None = None,
) -> dict:
    """Stamp a payload into the common envelope for sending over the socket."""
    return {
        # Canonical hyphenated UUID: Orchestration validates messageId with
        # UUID.fromString, which rejects the 32-char dashless `.hex` form.
        "messageId": str(uuid4()),
        "type": message_type.value,
        # Decimal string, per the envelope contract (Orchestration parses it as a
        # digits-only string, not a JSON number).
        "qaTryId": str(qa_try_id),
        "correlationId": correlation_id,
        "sequence": sequence,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload.model_dump(),
    }
