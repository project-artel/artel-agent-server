from pydantic import BaseModel, ConfigDict, Field

from app.agents.scenario import DEFAULT_LANGUAGE, OutputLanguage, ScenarioStep
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.qa.envelope import ActionResultPayload, GameState, QaChatTurn


# --- ACT: plan the actions for one step ---------------------------------------


class QaPlannedAction(BaseModel):
    """One grounded action the agent chose from the CURRENT scene.

    `method` is an SDK invokable method. The known set (button_click, enter_text,
    key_click — see ActionExecutor in artel-sdk) keeps growing, so it is NOT
    constrained here: the agent actively decides *which* method, *which* target
    id, and *which* args by reading the scene. The service assembles the JSON-RPC
    `params` as ``[target_id, *arguments]`` (target id omitted when null). The
    scene's recorded action telemetry is observation only, never an invocation
    target.
    """

    method: str
    target_id: int | None = None
    arguments: list[str] = Field(default_factory=list)


class QaActResult(BaseModel):
    thought: str
    action_message: str
    actions: list[QaPlannedAction] = Field(default_factory=list)




class QaActRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    scenario_title: str
    scenario_description: str
    step: ScenarioStep
    game_state: GameState
    # The operator conversation so far. It steers the decision: an instruction
    # given mid-run has to reach the very next act, or answering it was theatre.
    chat: list[QaChatTurn] = Field(default_factory=list)
    model: LLMModel = DEFAULT_MODEL
    language: OutputLanguage = DEFAULT_LANGUAGE


# --- VERIFY: judge whether the step's `expected` held -------------------------


class QaVerifyResult(BaseModel):
    reasoning: str
    passed: bool
    verdict_message: str


class QaVerifyRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    step: ScenarioStep
    game_state: GameState
    action_result: ActionResultPayload
    chat: list[QaChatTurn] = Field(default_factory=list)
    model: LLMModel = DEFAULT_MODEL
    language: OutputLanguage = DEFAULT_LANGUAGE


# --- CHAT: answer the operator, mid-run -----------------------------------------


class QaChatResult(BaseModel):
    reply: str


class QaChatRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    scenario_title: str
    scenario_description: str
    # None once the run is past its last step.
    step: ScenarioStep | None = None
    game_state: GameState | None = None
    chat: list[QaChatTurn] = Field(default_factory=list)
    model: LLMModel = DEFAULT_MODEL
    language: OutputLanguage = DEFAULT_LANGUAGE
