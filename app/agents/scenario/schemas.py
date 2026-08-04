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


class ScenarioPlan(BaseModel):
    """One scenario the run goal was decomposed into.

    Deliberately NOT a ``ScenarioDraft``: the run-scoped authoring agent no longer
    writes step bodies. TestCases live only in the orchestration server, so a
    scenario is expressed as references to the cases that make it up
    (``case_ids``), and Orchestration links them (`test_scenario_case`) and adds
    the scenario to the run. ``ScenarioDraft``/``ScenarioStep`` stay for the QA
    execution agent, which still runs an approved step-based scenario.
    """

    # None = a brand-new scenario to add; an id = edit that existing scenario
    # (echoed from the run's `current_scenarios`). Orchestration branches
    # insert-vs-update on this (ARTEL-206 Step 6). Ids arrive as strings on the
    # wire; pydantic coerces to int.
    scenario_id: int | None = None
    title: str
    description: str
    # Ids of existing TestCases (from `search_test_cases`) this scenario is built
    # from. The search returns ids as strings on the wire; they are numeric on the
    # far side, so the plan carries them as ints. May be empty only alongside an
    # empty `scenarios` on the result — a scenario with no cases is not authored.
    case_ids: list[int] = Field(default_factory=list)


class ScenarioAgentResult(BaseModel):
    message: str
    # The run goal, decomposed. Empty when no matching cases were found: the agent
    # must not fabricate scenarios, and says so in `message` instead.
    scenarios: list[ScenarioPlan] = Field(default_factory=list)


# ScenarioAgentRequest references ScenarioPlan (defined after it) via a forward
# ref for `current_scenarios`; resolve it now that ScenarioPlan exists.
ScenarioAgentRequest.model_rebuild()
