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
    # 0.1초마다 뜬 판독을 1초에 모아 보낸 것(ARTEL-401). orchestration 이 변환 없이
    # 중계하므로(ARTEL-414) payload 는 SDK 가 쓴 문서 그대로다.
    PULSE = "PULSE"
    CANCEL = "CANCEL"
    # The answer to a KNOWLEDGE_SEARCH, correlated by that frame's messageId.
    KNOWLEDGE_SEARCH_RESULT = "KNOWLEDGE_SEARCH_RESULT"
    # The answer to a KNOWLEDGE_EXPAND, correlated the same way.
    KNOWLEDGE_EXPAND_RESULT = "KNOWLEDGE_EXPAND_RESULT"
    # The answer to any one of the knowledge writes below (ARTEL-331). ONE type for
    # all five, correlated the same way; which write it answers is in the payload's
    # `type`. Orchestration chose a single type so that the next write inherits the
    # contract instead of deciding again whether it has an answer at all.
    KNOWLEDGE_WRITE_RESULT = "KNOWLEDGE_WRITE_RESULT"
    # Agent -> Orchestration
    LOG = "LOG"
    ACTION = "ACTION"
    STATUS = "STATUS"
    # Asks the project's knowledge base a question. This name and the result type
    # above are spelled exactly as Orchestration's QaAgentInboundRouter expects
    # them: it rejects an unknown type outright, and that rejection reaches the
    # waiting tool as silence rather than as an error it could report.
    KNOWLEDGE_SEARCH = "KNOWLEDGE_SEARCH"
    # Walks the knowledge graph out from one entry (ARTEL-275). Answered, like the
    # search and for the same reason: the tool that asks parks on the reply.
    KNOWLEDGE_EXPAND = "KNOWLEDGE_EXPAND"
    # Writes to the project's knowledge base: one new entry, one correction, one
    # soft delete. Spelled exactly as Orchestration's KNOWLEDGE_MUTATION_TYPES has
    # them, for the reason given on the search above.
    #
    # Answered, since ARTEL-331: success comes back as KNOWLEDGE_WRITE_RESULT above,
    # a rejection as a correlated ERROR — the same pair the search and the expansion
    # already use. Before that these were one-way, which meant a rejection reached
    # the model as a success; `record_knowledge` and friends validate locally partly
    # for that reason, and they keep doing so because a round trip saved is still
    # saved.
    #
    # An answer is not guaranteed. Orchestration performs the write and skips the
    # reply when the run has no Agent session, and frames it drops before routing
    # (unknown or finished run) are never answered at all. Silence therefore means
    # "cannot confirm", NOT "did not happen" — see `QaRunChannel.write_knowledge`.
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
    # Asserts or withdraws a relation between two entries (ARTEL-274). Answered
    # like the writes above, and by the same pair of frames. `link_knowledge` and
    # `unlink_knowledge` still check the relation, the note and the endpoints
    # themselves — that used to be the only thing standing between a bad frame and
    # a false success, and it is now a round trip saved on a run that has a clock.
    KNOWLEDGE_LINK = "KNOWLEDGE_LINK"
    KNOWLEDGE_UNLINK = "KNOWLEDGE_UNLINK"
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


def center_of(x: float, y: float, w: float, h: float) -> tuple[int, int]:
    """조준점을 모서리와 크기에서 뽑는다.

    두 채널이 이것을 쓴다 — `GAME_STATE` 는 [Rect.center] 를 거쳐, 판독은
    `app/qa/pulse.py` 의 `_aim` 에서 직접. 계산을 두 자리에 두면 언젠가 한쪽만
    반올림이 바뀌고, 그 차이는 1px 라 아무도 못 알아본 채로 조준이 갈린다.

    판독의 좌표는 정수가 아닐 수 있다. SDK 가 `"0.####"` 로 쓰므로 `47.0` 은 `47` 로,
    `47.5` 는 그대로 온다. 잘라 버린다 — 픽셀 아래로는 화면의 무엇도 다르게 그려지지
    않고, 그것을 겨눈 무엇도 다른 데 떨어지지 않는다.
    """
    return int(x) + int(w) // 2, int(y) + int(h) // 2


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
        return center_of(self.x, self.y, self.w, self.h)


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


class KnowledgeNeighbour(BaseModel):
    """One entry hanging off another by a relation, or by similarity.

    Every field defaults and unknown ones are kept, for the reason
    `KnowledgeSearchHit` gives: a neighbour that failed validation would take the
    whole answer down with it and leave the asking tool sitting until its timeout.

    `origin` is the discriminator, not `relation`. A neighbour reached by an EDGE
    is something a run asserted deliberately, with a written reason in `note`; a
    VECTOR one is a machine guess with nobody standing behind it, and `relation`
    reads "SIMILAR" only as a label. Weighing them the same is the mistake this
    field exists to prevent.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    # LEADS_TO|CONTRADICTS|REFINES|DEPENDS_ON|REPLACES, or SIMILAR for a vector
    # neighbour. A plain string for the same reason `tag` is one below.
    relation: str = ""
    origin: str = ""
    # OUT|IN from the entry this hangs off; NONE for symmetric relations and for
    # vector neighbours, where a direction word would invent a claim.
    direction: str = ""
    # Why the relation was asserted. None for a vector neighbour — nobody asserted it.
    note: str | None = None
    tag: str = ""
    source: str = ""
    summary: str = ""
    depth: int = 1
    # Cosine similarity, VECTOR only.
    score: float | None = None
    # Which entry this hangs off. Useful at depth 2, where it says where a
    # neighbour branched from.
    via: str = ""


class KnowledgeAnchor(BaseModel):
    """One place an entry is tied to: the fact holds there and not everywhere.

    A list rather than a single value on the hit below, because one entry can be
    tied to several screens. An EMPTY list is not "the anchors could not be read"
    — it is the ordinary case of a fact true wherever the player is, and it is
    also what an Orchestration that predates anchors sends.

    `screen_id` absent is normal too: a screen is decided from observation, and
    when it was not decided the anchor says the scene and stops there. It is text
    for the reason every other id here is — a 64-bit value must not lose precision
    on the way through JSON — and it goes back out that way on
    `KnowledgeCreatePayload`.

    Both fields default, as on `KnowledgeSearchHit`: an anchor that failed
    validation would take the whole answer down with it, and the tool that asked
    would sit until its own timeout.
    """

    model_config = ConfigDict(extra="allow")

    scene_name: str = ""
    screen_id: str | None = None


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
    # Entries one hop from this one (ARTEL-275). Defaults to empty, so an
    # Orchestration that predates the graph — or one with `expand-search-hits`
    # turned off — simply sends hits without it and nothing here notices.
    neighbors: list[KnowledgeNeighbour] = Field(default_factory=list)
    # Where this entry holds, when it holds in one place only (ARTEL-592). Defaults
    # to empty for the same reason `neighbors` does: an Orchestration that does not
    # send anchors and an entry that has none read identically here, and both mean
    # "no screen is claimed".
    anchors: list[KnowledgeAnchor] = Field(default_factory=list)


class KnowledgeExpandResultPayload(BaseModel):
    """The answer to one KNOWLEDGE_EXPAND.

    An empty `neighbors` is a normal answer: the entry may simply have no
    relations yet, and on a run with `knowledge_mode=off` it is always empty.

    `truncated` says a cap cut something off. It rides along rather than being
    inferred from the count because the caps are Orchestration's and this side
    does not know them — and a silently truncated list reads as "that is all
    there is", which is the one thing it is not.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    summary: str = ""
    neighbors: list[KnowledgeNeighbour] = Field(default_factory=list)
    truncated: bool = False


class KnowledgeWriteResultPayload(BaseModel):
    """The answer to one knowledge write (ARTEL-331).

    `type` echoes the write it answers, so a reader of the log can tell what
    happened without matching correlation ids by hand. Only one of the two id
    fields is filled: entry writes carry `knowledge_id`, relation writes carry
    `edge_id`.

    The id is the row that now holds the fact **in this run's knowledge scope**.
    On a scoped run that corrected or deleted a baseline entry it is the shadow or
    tombstone, not the baseline — the baseline id is one this run could not name
    again.

    Both default to empty rather than being required: an Orchestration that adds a
    field must not make this side drop the frame that releases a waiting tool.
    """

    model_config = ConfigDict(extra="allow")

    type: str = ""
    knowledge_id: str = ""
    edge_id: str = ""


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
    # Which scenario step asked. `search_knowledge` has always taken this and never
    # sent it, which is why `knowledge_usage.step` was null on every row ever
    # written. It is a COORDINATE, not a filter: the far side records it and the
    # results do not change. Optional because an Orchestration that predates it
    # ignores unknown payload fields, so neither side has to deploy first.
    step: int | None = None


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
    # The ANCHOR: where this fact holds, when it holds in one place only (ARTEL-592).
    # A control that behaves on this screen unlike anywhere else, a screen whose
    # usual way back does nothing. A fact true wherever the player is fills neither
    # field, and that asymmetry is the whole design — a fact tied to one screen is
    # a fact the run standing on the next one never finds.
    #
    # NOTHING here reads the run's current scene. Filling the anchor from wherever
    # the run happened to be standing would file every game-wide rule under one
    # screen; the agent names it or leaves it out.
    #
    # Optional for the reason `KnowledgeSearchPayload.step` is optional: an
    # Orchestration that does not know these fields ignores unknown payload fields,
    # so neither side has to deploy first. A frame carrying neither is treated
    # exactly as it was before anchors existed.
    #
    # `screen_id` travels as text for the reason every other id crossing this
    # boundary does — `knowledge_id` on the update and the delete, both endpoints of
    # a link: a 64-bit value must not lose precision on the way through JSON. It
    # comes back the same way on `KnowledgeAnchor`.
    #
    # `screen_id` cannot travel alone. A screen lives inside a scene, so an anchor
    # naming a screen and no scene cannot later be read back as which scene's
    # screen it was — Orchestration refuses that pair, and `record_knowledge`
    # refuses it here before a frame is spent finding out.
    scene_name: str | None = None
    screen_id: str | None = None


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


class KnowledgeExpandPayload(BaseModel):
    """A request to walk the graph out from one entry.

    No project and no scope travel with it, for the reason `KnowledgeSearchPayload`
    gives — both come from the run on the far side.

    `depth` is a request, not a guarantee: Orchestration clamps it to its own
    ceiling rather than refusing, so a value past the limit costs a shallower
    answer instead of a failed tool call.
    """

    knowledge_id: str
    depth: int
    include_similar: bool = True
    # Carried for the same reason the search carries it: an expansion writes
    # `knowledge_usage` rows too, and a coordinate on one of the two paths but not
    # the other leaves half the table unable to say when it was read.
    step: int | None = None


class KnowledgeLinkPayload(BaseModel):
    """One relation asserted between two entries.

    Both endpoints are ids the run has been shown. Orchestration folds them to the
    baseline entry they represent before storing, so an id that names a
    scope-local shadow still records the relation where every run can read it.

    `note` is required here and NOT NULL on the far side. An edge nobody can audit
    is an edge nobody can remove with confidence, and for `LEADS_TO` the note is
    not the reason but the ACTION — it is what makes the route usable by a run
    that has never walked it.
    """

    from_knowledge_id: str
    to_knowledge_id: str
    relation: str
    note: str


class KnowledgeUnlinkPayload(BaseModel):
    """The withdrawal of one relation.

    Named by its endpoints and relation rather than by an edge id, because this
    side has never seen an edge id — the id printed with a neighbour is the
    knowledge id. Exposing edge ids would add a second id space to everything the
    agent reads, for the sake of one tool.

    No note. Why a relation was withdrawn is carried by the tool's `thought`,
    which is already the run's record of its reasoning, and there is no surviving
    row to attach it to.
    """

    from_knowledge_id: str
    to_knowledge_id: str
    relation: str


class StatusPayload(BaseModel):
    status: StepStatus
    # Always present: null for per-step frames, PASSED|FAILED only on the
    # terminal run frame — so Orchestration can read it uniformly.
    result: RunResult | None = None
    step: int | None = None
    # Per-step 판정 프레임에만 채워진다(2단 판정): 이 스텝이 속한 TC(`case_id`)와 그 스텝이
    # 구간의 검증(마지막) 스텝인지(`is_verification`). Orche/FE가 스텝을 TC로 묶어 표시한다.
    case_id: int | None = None
    is_verification: bool = False
    message: str
    summary: dict | None = None
    # Knowledge this verdict actually rested on (ARTEL-293). Only a per-step frame
    # ever carries it, and only `report_step` fills it: knowledge bears on the
    # judgement of a step, not on an individual click. Attached to the acting tools
    # instead, an entry used across a ten-click step would score ten times what the
    # same entry scores on a one-click step, and the metric would measure action
    # count rather than usefulness.
    #
    # SELF-REPORTED, unlike everything Orchestration observes for itself. A model
    # that used an entry and did not say so is invisible here, so the count leans
    # LOW. That is the safe direction, and it is not corrected by pressing the
    # model to cite more — pressure buys citations of whatever is at hand, and
    # then the bias has no known direction at all.
    used_knowledge_ids: list[str] = Field(default_factory=list)
    # How many ids `report_step` threw away because this run had never been shown
    # them. Sent rather than dropped because the hallucinated-citation RATE is
    # itself a comparison between models — discarded in silence, that signal is
    # gone, and a model that invents ids scores exactly like one that does not.
    rejected_knowledge_id_count: int = 0


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
