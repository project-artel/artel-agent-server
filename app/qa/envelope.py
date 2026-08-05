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
    # Writes to the project's knowledge base: one new entry, one correction, one
    # soft delete. Spelled exactly as Orchestration's KNOWLEDGE_MUTATION_TYPES has
    # them, for the reason given on the search above.
    #
    # Unlike the search, these are ONE-WAY. Orchestration's `routeKnowledgeMutation`
    # answers a mutation with no frame at all — a success is silent, and a rejection
    # becomes an ORCHE_INTERNAL row on the run's own timeline, published to the
    # operator's stream rather than back down this socket. Nothing on this side may
    # wait for a reply to one of these.
    #
    # KNOWLEDGE_UPDATE was deliberately absent until ARTEL-257. ARTEL-189 had the
    # agent correct an entry by deleting it and recording the corrected version, to
    # keep the tool surface small. What that cost is a history in which a repair
    # and a discard are the same event — both a DELETE and nothing more — so
    # comparing runs by whether the knowledge they wrote survived (ARTEL-239)
    # scored the model that maintained knowledge most carefully as the worst.
    KNOWLEDGE_CREATE = "KNOWLEDGE_CREATE"
    KNOWLEDGE_UPDATE = "KNOWLEDGE_UPDATE"
    KNOWLEDGE_DELETE = "KNOWLEDGE_DELETE"
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


class KnowledgeUpdatePayload(BaseModel):
    """A correction to one existing entry, which keeps its id and its lineage.

    A third model rather than a widened create or delete, by the same rule that
    split those two below: this is the only one of the three that names an id AND
    carries a body, and both are load-bearing. Collapsing all three into
    Orchestration's single `KnowledgeMutationRequest` would make every field
    optional here too, and leave which ones each type actually requires to a
    comment.

    A field left as None is left alone on the far side — `updateFromQaTry` writes
    only what it was given. That is what lets fixing one sentence be a frame
    carrying one sentence, rather than a re-send of the whole entry with the parts
    that were already right copied back over themselves. All three None is refused
    there, so `update_knowledge` refuses it here instead of spending a frame to
    find out.
    """

    knowledge_id: str
    tag: str | None = None
    summary: str | None = None
    description: str | None = None


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
    same way — the update above names its target the same way, for the same reason.
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
