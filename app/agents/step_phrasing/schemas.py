"""What the user said about a gap, turned into scenario steps.

Authoring leaves a gap where the scene spec does not know how one screen reaches
the next, and asks. The answer comes back as a person types it — "대화 끝나면
스페이스 한번 더" — and dropping that sentence into the scenario verbatim puts a
line in a different voice from every step around it, sometimes several actions
long, sometimes not an instruction at all.

This is the smallest possible model call: rephrase, split, and nothing else. It
does not decide where the steps go (Orchestration knows the position — it asked
the question) and it must not add an action the user did not describe. A step
invented here would read exactly like one the user asked for, and the whole point
of the gap was that nobody knows what goes there.
"""

from pydantic import BaseModel, Field

from app.llm.models import DEFAULT_MODEL, LLMModel


class PhrasedStep(BaseModel):
    """One step line, in the same voice as the steps around it."""

    action: str
    # The key or control the step presses, when the user named one. Mirrors
    # ScenarioStep.input on the far side; null when the sentence names none.
    input: str | None = None


class PhrasedSteps(BaseModel):
    """The model's structured output: zero or more steps.

    Empty is a real answer, not a failure. "잘 모르겠는데" is a reply to the
    question, not a description of how to get across — the caller keeps the gap
    and hands the sentence to the conversation instead.
    """

    steps: list[PhrasedStep] = Field(default_factory=list)


class StepPhrasingRequest(BaseModel):
    """One gap, what the user said about it, and its neighbours for voice.

    `before`/`after` are the steps on either side of the gap. They are context
    for phrasing only — the model matches their wording and tense, and must not
    repeat what they already do.
    """

    said: str
    blocked_by: str = ""
    before: str = ""
    after: str = ""
    locale: str = "ko"
    model: LLMModel = DEFAULT_MODEL
