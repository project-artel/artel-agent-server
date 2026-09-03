from enum import StrEnum
from typing import Literal

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.llm.models import DEFAULT_MODEL, LLMModel


class OutputLanguage(StrEnum):
    """Language for the scenario agent's natural-language output.

    Scoped to the scenario agent on purpose (see the plan): ``/extract`` keeps
    its output in English. Add a member here and a matching directive in
    ``prompt.LANGUAGE_DIRECTIVES`` to support another language.
    """

    ko = "ko"
    en = "en"


DEFAULT_LANGUAGE: OutputLanguage = OutputLanguage.ko


class ScenarioStep(BaseModel):
    step: int = Field(gt=0)
    title: str
    state: str
    action: str
    expected: str


class ScenarioDraft(BaseModel):
    title: str
    description: str
    steps: list[ScenarioStep] = Field(default_factory=list)

    @field_validator("steps")
    @classmethod
    def validate_step_numbers(cls, steps: list[ScenarioStep]) -> list[ScenarioStep]:
        step_numbers = [step.step for step in steps]
        if len(step_numbers) != len(set(step_numbers)):
            raise ValueError("Scenario step numbers must be unique.")
        if step_numbers != list(range(1, len(steps) + 1)):
            raise ValueError("Scenario steps must be numbered sequentially from 1.")
        return steps


class CaseGuard(BaseModel):
    """One comparison a case's precondition requires.

    Read from the case's condition structure, so the whole name survives:
    ``CombineButton.combineZone.activeSelf``, not ``activeSelf``. Rendering it to
    a sentence and reading it back is what used to lose the owner.
    """

    variable: str
    operator: str
    value: str
    # Where this value moves. Requirements all look alike on one line — `position == 0`
    # and `StagePosition >= 1` read the same — but the first is one arrow key and the
    # second means winning a fight. Empty when the map does not say.
    raised_in: list[str] = Field(default_factory=list)
    # How that value moves, not just where (ARTEL-646). The screen name alone reads as
    # "drop by and come back" — measured (run 203), authoring entered the battle screen
    # and never wrote the step that wins it. The map knew winning was required.
    moves: list["ValueMove"] = Field(default_factory=list)


class ValueMove(BaseModel):
    """One place a value changes.

    ``how`` is empty when there is no button for it: the player has to make ``when``
    come true by playing, and that takes its own step. That single distinction is what
    separates `position == 0` (one arrow key) from `StagePosition >= 1` (win a fight).
    """

    scene: str
    by: str | None = None
    how: str | None = None
    when: str | None = None


class SceneExit(BaseModel):
    """One step from this screen to another.

    ``by`` is what to press — a key, a control path. **Empty means the game goes
    on its own**: nothing to press, so do not send whoever runs this looking for a
    button. "Nothing to press" and "not known" are different answers and this field
    keeps them apart.

    Only one step out, never the full set of screens you could eventually reach:
    measured on a real game, every screen reached every other, so the full set said
    nothing. One step at a time is what you chain into a route.
    """

    scene: str
    by: str | None = None


class TestCaseListItem(BaseModel):
    """One TestCase as the session receives it, in the project's whole list.

    Named after orchestration's DTO on the other end of the wire so the contract
    is greppable from either side. ``verification_status`` is DRAFT/VERIFIED/
    BROKEN — it rides along so the agent can prefer a verified case and steer
    around a broken one, which a similarity score never told it.

    The bodies (``precondition``, ``expected_value``) travel with the entry rather
    than being fetched afterwards. The agent has to read them to write the steps that
    exercise a case, and a fetch-later path would only move the old ceiling — how
    many searches a turn may make — onto a new one.
    """

    id: int
    scene: str
    step: str
    precondition: str | None = None
    expected_value: str
    verification_status: str
    # What the case needs, and what it leaves behind. Orchestration parses these
    # from the case's own condition structure — not from the sentence — so both
    # sides read one state instead of two readings of one sentence.
    #
    # **These were on the wire and dropped here.** The model had no such fields,
    # so pydantic discarded them while the prompt went on telling the agent to
    # order by them. Ordering silently fell back to re-reading the sentence.
    state_before: list[CaseGuard] = Field(default_factory=list)
    state_after: dict[str, str] = Field(default_factory=dict)
    # Where this screen leads in one step, and what to press to get there.
    # The map has known this all along; it was never sent.
    exits: list["SceneExit"] = Field(default_factory=list)
    # What to put in this case's step `input` — `key:Return`, `click:Canvas/continue`.
    # Empty means there is nothing to press: an observation, where the game acts on
    # its own. "Nothing to press" and "not known" are different answers, and an
    # empty string is the first of the two.
    #
    # **This used to cost a tool call.** `explain_case` answered scene, requires,
    # leaves and this; measured over a real turn, this was the only one of the four
    # the list did not already carry. Moving one field removes the round trip.
    input: str = ""


class ScenarioAgentRequest(BaseModel):
    # LangChain messages are passed through as-is (not re-validated).
    model_config = ConfigDict(arbitrary_types_allowed=True)

    user_input: str
    # Opaque, game-specific context merged upstream (Unity SDK + user-provided).
    unity_context: dict = Field(default_factory=dict)
    game_context: dict = Field(default_factory=dict)
    # Every TestCase in the project, sent once when the session opens (ARTEL-319).
    #
    # Empty means orchestration sent none — an older deployment, or a session
    # whose user is not a project member. The turn then falls back to
    # `search_test_cases`, which is why that tool and the whole embedding path
    # behind it stay in place. That fallback is also the rollback: orchestration
    # can stop sending the field and this side needs no redeploy.
    test_case_list: list[TestCaseListItem] = Field(default_factory=list)
    # Recent conversation, text-only, already windowed by the session layer.
    history: list[BaseMessage] = Field(default_factory=list)
    # Authoritative current draft (may contain the user's manual edits). Legacy;
    # run-scoped authoring uses `current_scenarios` below instead.
    draft: ScenarioDraft | None = None
    # The run's current scenarios (ARTEL-206 Step 6). Lets the agent target an
    # existing scenario for edits by echoing its `scenario_id`. Empty for a fresh run.
    current_scenarios: list["ScenarioPlan"] = Field(default_factory=list)
    model: LLMModel = DEFAULT_MODEL
    # Locale for the natural-language output (message + scenario text).
    locale: OutputLanguage = DEFAULT_LANGUAGE
    # Which run this turn belongs to (ARTEL-650). Carried for one reason: the
    # per-run record of the authoring session lives on the orchestration side, and
    # the prompt the model saw plus its raw answer are the only two things that
    # never reach it. Without the id they cannot be filed with the rest.
    run_id: int | None = None
    # Walkable flows, worked out by orchestration before the turn starts (ARTEL-658).
    #
    # Which cases belong in one scenario and in what order is what decides whether the
    # result can actually be run, and holding forty-two cases at once is the part the
    # model is weakest at — measured, a plain list gave 26 scenarios with 9 unreachable
    # climbs, one journey at a time gave 9 with 1. So that judgement moved to the
    # calculation and arrives here already made.
    #
    # Empty means orchestration sent none — an older deployment, or the calculation
    # failed. The turn then groups and orders on its own, exactly as before; that
    # fallback is also the rollback.
    flows: list["AuthoredFlow"] = Field(default_factory=list)
    # Which screen the game boots into (ARTEL-670). The screen graph is cyclic —
    # every screen reaches every other — so nothing in the structure says where a
    # player starts. The build says it, orchestration reads it, and until now it
    # was used only inside orchestration's own calculation and never told to the
    # model. Absent means "not sent", which the shape block prints as such.
    entry_scene: str | None = None


class AuthoredFlow(BaseModel):
    """One walkable flow: which cases, in what order, and what it costs to run.

    **A constraint, not a script.** The order is what makes it walkable; reordering it
    or inserting other cases breaks the guarantee. Cutting is safe — the front part of
    a walkable flow is still walkable.
    """

    case_ids: list[int]
    # What has to be true before step one. The flow cannot produce these itself.
    opening: list[str] = Field(default_factory=list)
    # How many places along the way cannot be instructed — someone has to play through
    # them (win a fight, sit through a cutscene). Each one is a stop for whoever runs it.
    gaps: int = 0


class AuthoredStep(BaseModel):
    """작성된 시나리오 스텝 하나 = 행위 하나 (재설계 2026-08-08, ARTEL-284).

    Orche 저장 계약(`ScenarioResult`/`ScenarioStep`)·QA 실행 계약(`app/qa` QaStep)과 동일한
    필드명이다. `case_id`가 있으면 그 스텝은 해당 TC 검증 구간에 속한다(연속 동일 case_id = 한
    구간, 마지막 스텝이 기대결과 검증). 없으면 판정 대상 아닌 단순 행위(이동·연결). `hint`/`input`은
    강제가 아닌 어드바이저리 근거.
    """

    action: str
    case_id: int | None = None
    hint: str | None = None
    input: str | None = None
    # 이 스텝을 어디서 가져왔는가(ARTEL-467).
    #
    # 검증 스텝은 원래 근거가 있었다 — `case_id`가 "이 스텝은 이 케이스를 본다"고 말해 준다.
    # **브리지만 없었고**, 그래서 지어낸 스텝과 알고 쓴 스텝을 기계가 구분할 수 없었다.
    # 그 구분이 없는 것이 실행 중에 터지는 지점이다.
    #
    # 한 문자열이 아니라 셋으로 나눈 것은 프로토타입에서 `"unknown:StagePosition을…"` 한 필드로
    # 두었다가 파싱이 필요해졌고, `case_id`가 있는데 근거는 간선이라고 적은 계약 위반이 8건
    # 나왔기 때문이다.
    step_source: Literal["CASE", "CAPABILITY", "UNKNOWN", "HUMAN"] | None = Field(
        default=None,
        description=(
            "Where this step came from. Set it on every step.\n"
            "  CASE        this step verifies the case in case_id. **Only when case_id is\n"
            "              set** — a step with no case_id is not a CASE step, it is a bridge,\n"
            "              and marking it CASE is the most common way this field goes wrong\n"
            "              (measured: 22 of 70 steps in one turn, and the whole answer was\n"
            "              rejected for it)\n"
            "  CAPABILITY  this step takes the route find_path gave you — put its id in\n"
            "              step_source_capability_id\n"
            "  UNKNOWN     no known route. Put what is blocking in step_unknown_reason,\n"
            "              and say so in `message` too.\n"
            "  HUMAN       the user told you how this is done, in this conversation. Use it\n"
            "              ONLY for that: it is an attribution, and it is shown to them as\n"
            "              their own answer. Guessing here puts words in their mouth.\n"
            "A step you cannot ground in one of these is a step that fails when someone runs it."
        ),
    )
    step_source_capability_id: int | None = None
    step_unknown_reason: str | None = None


class ScenarioPlan(BaseModel):
    """One scenario the run goal was decomposed into (재설계 2026-08-08, ARTEL-284).

    시나리오 = 순서 있는 `steps` 리스트. 각 스텝은 행위 하나이며, 검증 대상 TC를 `case_id`로
    옵션 참조한다(연속 동일 case_id = 한 TC 검증 구간). Orche(`ScenarioReconcileService`)가
    이 steps를 시나리오 payload로 통째 upsert한다 — 구 조합 테이블(test_scenario_case)은 폐기.
    ``ScenarioDraft``/``ScenarioStep``(v1 실행 초안)은 별개로 남는다.
    """

    # None = a brand-new scenario to add; an id = edit that existing scenario
    # (echoed from the run's `current_scenarios`). Orchestration branches
    # insert-vs-update on this (ARTEL-206 Step 6). Ids arrive as strings on the
    # wire; pydantic coerces to int.
    scenario_id: int | None = None
    title: str
    description: str
    # 시나리오 본문 = 순서 있는 스텝 리스트. 검증 스텝은 `search_test_cases`로 찾은 TC의 id를
    # `case_id`로 단다. 빈 리스트는 `scenarios`가 비어있을 때만 정상(작성할 게 없는 턴).
    steps: list[AuthoredStep] = Field(default_factory=list)


class ReviewedCases(BaseModel):
    """Every case in the project, judged in or out for this request (ARTEL-404).

    Not a list of the ones picked. A picked-only list cannot distinguish a case
    that was considered and dropped from one that was never looked at, so there is
    nothing for orchestration to check. With a verdict on every id, "not reviewed"
    becomes "has no verdict" and a set subtraction finds it.

    Asking the agent whether it read everything would be circular — it answers yes.
    This is not that question. The agent is made to emit the verdicts and the
    counting happens elsewhere, on the other side of the wire.

    Two arrays rather than a map: measured at 3,005 tokens for a thousand cases
    against 5,001 for `{"82": 1, ...}`, and the check is simpler — `in | out` has
    to equal the project.

    No reasons per exclusion. They would double the output at a thousand cases and
    would be the agent describing itself again, which is exactly what cannot be
    checked. The case for an inclusion is the steps that exercise it.
    """

    included: list[int] = Field(default_factory=list, alias="in")
    excluded: list[int] = Field(default_factory=list, alias="out")

    model_config = ConfigDict(populate_by_name=True)


class QuestionOption(BaseModel):
    """One thing the user can pick.

    `label` is written as the user's own instruction ("타이틀 버튼 확인도 담아 줘"), because
    that sentence is what comes back on the next turn. Anything that needs translating
    into an instruction later is a label that was written for the wrong reader.
    """

    id: str
    label: str
    detail: str | None = None


class ScenarioQuestion(BaseModel):
    """Something to ask the user, with somewhere for them to click (ARTEL-487).

    Asking already happened — one authoring message in seven ended with a question. It
    was buried in prose, so it read as explanation and nobody answered it. This is that
    same question with an id, a reason, and options.

    **Asking does not replace authoring.** The question does not block saving, so write
    the scenarios you can ground and ask about the part you cannot. A turn that only
    asks spends the user's time twice.
    """

    id: str
    text: str
    # Why this is being asked. A question with a stated reason can be answered; a bare
    # one asks the user to guess what the tool wants.
    why: str | None = None
    options: list[QuestionOption] = Field(default_factory=list)
    allow_free_text: bool = True


class ScenarioAgentResult(BaseModel):
    message: str
    # The run goal, decomposed. Empty when no matching cases were found: the agent
    # must not fabricate scenarios, and says so in `message` instead.
    scenarios: list[ScenarioPlan] = Field(default_factory=list)
    # Every project case judged in or out for this request (ARTEL-404).
    #
    # None means the turn had nothing to judge against — no `test_case_list`, so no
    # population to be exhaustive over. Orchestration reads None as "skip the check",
    # which is also the rollback path: stop emitting this and the checking stops.
    reviewed: ReviewedCases | None = None
    # One thing to ask the user (ARTEL-487). None on an ordinary turn.
    #
    # **One at a time.** Two questions leave the user no way to say which one they
    # answered, and the screen no way to route the reply.
    question: ScenarioQuestion | None = None


# ScenarioAgentRequest references ScenarioPlan (defined after it) via a forward
# ref for `current_scenarios`; resolve it now that ScenarioPlan exists.
ScenarioAgentRequest.model_rebuild()
