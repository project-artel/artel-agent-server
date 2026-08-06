from enum import StrEnum

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


class ScenarioAgentRequest(BaseModel):
    # LangChain messages are passed through as-is (not re-validated).
    model_config = ConfigDict(arbitrary_types_allowed=True)

    user_input: str
    # Opaque, game-specific context merged upstream (Unity SDK + user-provided).
    unity_context: dict = Field(default_factory=dict)
    game_context: dict = Field(default_factory=dict)
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


class AuthoredStepKind(StrEnum):
    """저작 Step의 종류(ARTEL-281). `setup`은 사전조건 도달(판정 없이 fast-forward),
    `guide`는 실행. 검증(verify)은 만들지 않는다 — 케이스의 `expected`로 흡수한다(기획)."""

    setup = "setup"
    guide = "guide"


class AuthoredStep(BaseModel):
    """한 케이스 자리에 대한 실행 가이드 한 칸(ARTEL-254/261/281).

    실행 시 Agent에겐 **advisory**다: 씬이 다르면 무시하고 자기 판단으로 간다. `hint`는 사람/에이전트가
    아는 지름길(키·백도어)이고 강제가 아니다. 판정 여부(`assert`)는 Orche가 `kind`에서 유도한다
    (setup=판정 안 함). 그래서 여기선 kind/intent/hint/input만 만든다.
    """

    kind: AuthoredStepKind = AuthoredStepKind.guide
    intent: str
    hint: str | None = None
    # "keyboard" | "click" 같은 조작 방식(있으면). 없으면 null.
    input: str | None = None


class ScenarioCasePlan(BaseModel):
    """시나리오의 한 자리 = 참조할 기존 TestCase(id) + 그 자리의 저작 Step(ARTEL-281).

    Step 저작의 1책임은 Agent다. 각 자리마다 사전조건 도달(setup)과 실행(guide) 가이드를
    케이스의 precondition/expected와 게임 맥락에서 만든다. Step은 자리 전용이라, 같은 케이스가
    여러 자리에 와도 자리마다 다르다.
    """

    # search_test_cases가 돌려준 기존 TestCase id(문자열로 오면 int로 coerce).
    case_id: int
    steps: list[AuthoredStep] = Field(default_factory=list)


class ScenarioPlan(BaseModel):
    """One scenario the run goal was decomposed into.

    Deliberately NOT a ``ScenarioDraft``: the run-scoped authoring agent references
    EXISTING TestCases (which live only in the orchestration server) rather than
    writing step bodies for them. A scenario is the ordered cases that make it up
    (``cases``), each with its authored Steps; Orchestration links them
    (`test_scenario_case`, with the steps) and adds the scenario to the run.
    ``ScenarioDraft``/``ScenarioStep`` stay for the QA execution agent.
    """

    # None = a brand-new scenario to add; an id = edit that existing scenario
    # (echoed from the run's `current_scenarios`). Orchestration branches
    # insert-vs-update on this (ARTEL-206 Step 6). Ids arrive as strings on the
    # wire; pydantic coerces to int.
    scenario_id: int | None = None
    title: str
    description: str
    # The ordered cases this scenario is built from (from `search_test_cases`),
    # each with its authored Steps. Order is the scenario's flow. May be empty only
    # alongside an empty `scenarios` on the result — a scenario with no cases is not
    # authored.
    cases: list[ScenarioCasePlan] = Field(default_factory=list)


class ScenarioAgentResult(BaseModel):
    message: str
    # The run goal, decomposed. Empty when no matching cases were found: the agent
    # must not fabricate scenarios, and says so in `message` instead.
    scenarios: list[ScenarioPlan] = Field(default_factory=list)


# ScenarioAgentRequest references ScenarioPlan (defined after it) via a forward
# ref for `current_scenarios`; resolve it now that ScenarioPlan exists.
ScenarioAgentRequest.model_rebuild()
