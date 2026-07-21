from pydantic import BaseModel, Field, field_validator


class ScenarioContext(BaseModel):
    states: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    functions: list[dict] = Field(default_factory=list)
    facts: list[dict] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


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
    user_input: str
    context: ScenarioContext
    draft: ScenarioDraft | None = None


class ScenarioAgentResult(BaseModel):
    message: str
    scenario: ScenarioDraft
