from enum import StrEnum
from typing import Literal

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer

from app.llm.models import DEFAULT_MODEL, LLMModel, ReasoningConfig


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
    # Why this requirement is not confirmed, or None. Read from plain-language cases,
    # some of these are wrong and some only go by another name; dropping them lost the
    # right ones too, so they come marked and the case text decides.
    unconfirmed: str | None = None


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


class SceneEdge(BaseModel):
    """One screen transition from the whole map, not tied to any case.

    `SceneExit` above rides on a case, so a screen carrying no case never shows
    its outgoing steps — and the model reads that absence as "no way there". This
    is the same fact (where a screen leads, what to press; ``by`` empty means the
    game moves on its own), sent once for the whole map, so the shape block can
    fold it into routes between screens.
    """

    from_scene: str
    to_scene: str
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
    # Any one of these is enough. `state_before` keeps only what every branch shares,
    # so a "one of two ways" requirement arrives there empty and lives here instead.
    state_before_any: list[list[CaseGuard]] = Field(default_factory=list)
    state_after: dict[str, str] = Field(default_factory=dict)
    # Where this screen leads in one step, and what to press to get there.
    # The map has known this all along; it was never sent.
    exits: list["SceneExit"] = Field(default_factory=list)
    # 이 케이스의 스텝 `input` 에 넣을 값 — `key:Return`, `click:Canvas/continue`.
    # 비어 있으면 누를 것이 없다는 뜻이다: 게임이 스스로 하는 관측이다. "누를 것이 없다"와
    # "모른다"는 다른 답이고, 빈 문자열은 앞엣것이다.
    #
    # **이 한 칸이 도구 호출 하나였다.** `explain_case` 가 scene · requires · leaves 와
    # 이것을 답했는데, 실측해 보니 넷 중 목록에 없던 것은 이것뿐이었다. 한 칸을 옮기니
    # 왕복이 사라졌다.
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
    # 세션 창 밖으로 밀려난 대화가 있는가(ARTEL-942). 잘라 낸 뒤의 `history` 만으로는 알 수 없고,
    # 모델이 "원래 없던 말" 과 "밀려난 말" 을 가르려면 이것이 필요하다.
    history_truncated: bool = False
    # Authoritative current draft (may contain the user's manual edits). Legacy;
    # run-scoped authoring uses `current_scenarios` below instead.
    draft: ScenarioDraft | None = None
    # The run's current scenarios (ARTEL-206 Step 6). Lets the agent target an
    # existing scenario for edits by echoing its `scenario_id`. Empty for a fresh run.
    current_scenarios: list["ScenarioPlan"] = Field(default_factory=list)
    model: LLMModel = DEFAULT_MODEL
    # 이 턴의 추론 예산. 없으면 안 켠다 — 지금까지가 그랬다.
    reasoning: ReasoningConfig | None = None
    # Locale for the natural-language output (message + scenario text).
    locale: OutputLanguage = DEFAULT_LANGUAGE
    # Which run this turn belongs to (ARTEL-650). Carried for one reason: the
    # per-run record of the authoring session lives on the orchestration side, and
    # the prompt the model saw plus its raw answer are the only two things that
    # never reach it. Without the id they cannot be filed with the rest.
    run_id: int | None = None
    # 묶기·순서 검증(워크플로 B 미니 루프)이 오케 채점 창구를 부를 때 쓰는 스코프.
    # 채점의 지도 읽기가 project + user 로 좁혀지므로 둘 다 있어야 한다. 없으면
    # (구 배포가 안 보냄) 검증을 건너뛴다 — 턴은 그대로 진행된다.
    project_id: int | None = None
    app_user_id: int | None = None
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
    # The whole map's screen transitions (flows-off experiment). Folded into
    # screen-to-screen routes by the shape block, because per-case `exits` answer
    # one step only and the model reads a missing second step as "no way there" —
    # the same misreading the pair matrix used to correct, 1,126 cells of it.
    # Empty from an older orchestration; the shape block then says nothing new.
    scene_edges: list[SceneEdge] = Field(default_factory=list)
    # Which router branch this request is already on (workflow Step 1). None means
    # "not yet routed" — the agent routes once and re-enters with the branch set,
    # so a re-entry (e.g. the slim question turn) is never routed twice.
    current_route: str | None = None
    # What every value starts as when the game boots (flows-off experiment).
    # The flow calculation used to be the only holder of this fact; with flows
    # off nothing carried it, and the model could not even tell whether the
    # first case of a scenario is placeable.
    starting_values: dict[str, str] = Field(default_factory=dict)


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
    # 케이스에서 다음 케이스로 가는 데 무엇이 필요한지를 순서대로. 케이스 n 건이면 hop 은
    # n-1 개다. 비어 있으면 그것을 안 보내는 옛 orchestration 이고, 그때는 두 케이스 사이를
    # 물어봐야 한다.
    #
    # **계산해 놓고 버리던 것이다.** 흐름을 고르려면 케이스 두 개씩의 행렬이 통째로 필요하므로,
    # 여기 담긴 답은 turn 이 시작되기 전에 이미 다 있었다. 흐름은 순서만 실어 보냈다. 실측 한
    # turn 에서 agent 가 그것을 다시 알아내려고 `find_path` 를 280번 왕복했고 그중 109번이
    # 이미 답을 받은 것을 다시 물은 것이었다.
    hops: list["FlowHop"] = Field(default_factory=list)


class FlowHop(BaseModel):
    """흐름에서 이웃한 두 케이스 사이에 무엇이 들어가는지.

    `link` is the answer's kind: `beside` (nothing goes in between), `by_operation`
    (press what `actions` says), `by_play` (someone has to play through it) or
    `blocked` (there is no route, and `blocked_by` names what stops it).
    """

    from_case_id: int = Field(alias="from_case_id")
    to_case_id: int = Field(alias="to_case_id")
    link: str
    actions: list[str] = Field(default_factory=list)
    blocked_by: str | None = None

    model_config = ConfigDict(populate_by_name=True)


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
            "  CAPABILITY  this step takes a route the map named — put its capability id in\n"
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
    # 이 스텝이 확인하는 **승인 전 TC 초안**의 임시 번호(-1, -2, …, ARTEL-1273). 초안은 아직 TC 가 아니라
    # `case_id` 에 둘 수 없다. 오케가 승인 때 TC 를 만들고 이 번호를 진짜 id 로 바꾼다. 그 전까지 오케는
    # 이 칸을 모르고 지나가므로 스텝은 판정 없는 스텝으로 저장된다.
    proposed_case: int | None = None

    @model_serializer(mode="wrap")
    def _without_empty_draft(self, handler):
        # 초안이 없는 스텝은 예전과 같은 모양으로 나간다 — 칸을 모르는 쪽이 받는 값이 바뀌지 않게.
        data = handler(self)
        if isinstance(data, dict) and data.get("proposed_case") is None:
            data.pop("proposed_case", None)
        return data


class ProposedCase(BaseModel):
    """저작이 제안한 TC 초안 하나(ARTEL-1273). 사용자가 승인하면 사용자 TC 가 된다.

    TC 정의(게임 무관): 사전조건(선택) · 행동 · 기대 결과. 행동이 기대 결과를 직접 일으키거나, 행동이 없으면
    관측할 대상이 있어야 한다. 둘 다 아니면 TC 가 아니라 스텝이다.
    """

    ref: int = Field(description="Temporary id: -1, -2, … Unique within the answer.")
    scene: str = Field(default="", description="Screen the action happens on, as the cases name screens. Empty if unknown.")
    precondition: str = Field(default="", description="State that must hold before the action. Empty if none.")
    action: str = Field(default="", description="One thing the player does. Empty for a TC that only observes.")
    expected: str = Field(description="The result to check.")
    why: str = Field(default="", description="One short sentence: what this TC checks, for the user.")


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
    # ORDERED 는 스텝 순서대로 실행하고, FREE 는 순서 없는 TC 묶음이라 QA agent 가 순서를 정한다.
    # FREE 의 스텝은 TC 마다 하나(`action` 은 비고 `case_id` 만 있다)다.
    #
    # None 은 "말하지 않음" 이다. 제출에서는 새 시나리오면 ORDERED, `scenario_id` 가 있으면 저장된
    # 모드를 그대로 둔다는 뜻이고, `current_scenarios` 로 받은 값에서는 ORDERED 로 읽는다 —
    # 이 칸을 안 보내는 orchestration 의 시나리오는 전부 ORDERED 였다. 그렇게 읽는 곳이 [mode] 다.
    execution_mode: Literal["ORDERED", "FREE"] | None = None

    def mode(self) -> Literal["ORDERED", "FREE"]:
        """비어 있으면 ORDERED 로 읽은 실행 모드."""
        return self.execution_mode or "ORDERED"

    def is_free(self) -> bool:
        return self.mode() == "FREE"


def free_steps(case_ids: list[int]) -> list[AuthoredStep]:
    """FREE 시나리오의 스텝 — 중복을 뺀 case id 하나에 스텝 하나, 받은 순서대로.

    모델에게 쓰게 하지 않는다. FREE 스텝에는 문장도 순서도 없어서 쓸 것이 없고, 모델이 쓰면
    같은 TC 를 두 번 싣거나 `case_id` 없는 스텝을 섞어 orchestration 이 `invalid_free_scenario`
    로 돌려보낸다.
    """
    return [
        AuthoredStep(action="", case_id=case_id, step_source="CASE")
        for case_id in dict.fromkeys(case_ids)
    ]


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


class ScenarioChange(BaseModel):
    """이번 턴에 바뀐 시나리오 하나 (ARTEL-936). 화면이 결과 박스에 색으로 나눠 그린다."""

    action: Literal["created", "updated", "removed"]
    title: str
    # 지운 것은 이제 없으므로 번호가 없을 수 있다. 있으면 화면이 그 시나리오로 잇는다.
    scenario_id: int | None = None


class AgentReply(BaseModel):
    """한 턴의 답을 사람이 읽는 세 칸으로 (ARTEL-927, 질문 칸은 ARTEL-1116).

    한 덩어리 글이던 답에서는 무엇을 했는지와 왜 그랬는지가 섞여, 사용자가 반박하거나
    결정할 자리가 보이지 않았다. 세 번째 칸인 질문([asks])은 예전에 답과 따로 질문
    메시지로 나갔다 — 화면이 그것을 모달로 받았다. 이제 대화를 기억하므로(ARTEL-942)
    답 맨 아래에 함께 싣고, 사용자는 평범한 대화로 답한다.
    """

    # 코드가 센 한 줄. 몇 건을 생성·수정·삭제했는지(ARTEL-936) — 무엇이 바뀌었는지는 [changes].
    result: str
    # 판단한 모델의 말. 마크다운(문단·목록·표)이고, 모양은 읽기 쉬운 쪽으로 모델이 고른다.
    detail: str = ""
    # 바뀐 시나리오 목록(ARTEL-936). [result] 는 이것을 센 한 줄이다.
    changes: list[ScenarioChange] = Field(default_factory=list)
    # 사용자가 정할 것(ARTEL-1116). 비어 있으면 물을 것이 없는 턴이다. 선택지는 사용자의
    # 말로 쓰여 있어, 화면은 누르면 입력 칸에 그 말을 채운다 — 답은 다음 대화로 온다.
    asks: list[ScenarioQuestion] = Field(default_factory=list)


class Ref(BaseModel):
    """답 속 `[[tc:N]]`·`[[ts:N]]` 표식이 가리키는 것 (ARTEL-931).

    번호는 표식 안에서 기계만 읽는다. 화면은 [label] 로 칩을 그리고, TC 칩을 누르면
    [detail](사전조건·기대값)을 보여 준다 — TC 를 따로 볼 화면이 없어서다.
    """

    # tr · qa · issue · build 는 질문 답이 서비스 페이지로 보내는 표식이다(ARTEL-1097).
    kind: Literal["tc", "ts", "tr", "qa", "issue", "build"]
    id: int
    label: str
    detail: str | None = None


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
    # The answer split for the screen (ARTEL-927). None for a plain reply — a greeting,
    # an off-topic decline, a failure — which stays `message` alone.
    reply: AgentReply | None = None
    # What every `[[tc:N]]`/`[[ts:N]]` marker in `reply` (its asks included) points at
    # (ARTEL-931). Only markers that resolved are listed; the rest were removed.
    refs: list[Ref] = Field(default_factory=list)
    # 이 턴이 제안한 TC 초안(ARTEL-1273). 스텝의 `proposed_case` 가 이 `ref` 를 가리킨다. 오케가 아직
    # 모르는 칸이라 그동안은 답의 설명 칸에 표로도 싣는다.
    proposed_cases: list[ProposedCase] = Field(default_factory=list)


# ScenarioAgentRequest references ScenarioPlan (defined after it) via a forward
# ref for `current_scenarios`; resolve it now that ScenarioPlan exists.
ScenarioAgentRequest.model_rebuild()
