from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.llm.models import DEFAULT_MODEL, LLMModel


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
    # Authoritative current draft (may contain the user's manual edits).
    draft: ScenarioDraft | None = None
    model: LLMModel = DEFAULT_MODEL


class ScenarioAgentResult(BaseModel):
    message: str
    scenario: ScenarioDraft
