"""Failure analysis: why a step the Evaluator failed went wrong, read off the play that failed it.

Four things for a failed step — a category, the cause, how to see it again, and what to look at
— so that a person reading the run does not have to open the turn records in `qa_log`. The
analysis reads the play the executer kept in the process (`StepPlay`), not the log: `qa_log`
holds a clipped copy (`MAX_TURN_LOG_BYTES`), and a diagnosis made from a clipped record has
already been wrong once (ARTEL-824).

The reproduction is grounded mechanically. The request lists every input the play sent to the
game, numbered, and the model answers with numbers; the code turns them back into the sentences
the macro runner sent. An input the play never sent cannot be written into the steps, which is
the one way a replay of them can mean anything.

Two kinds of failure never reach the model. A step the engine itself stopped — the planner's
macros refused, Jev not answering, the evaluator unable to read a verdict — is `AGENT_ERROR`
by construction, and a FREE case that never reached its goal or its scene is `UNREACHED`;
both are filled in here by code, with the same shape, so the record reads the same whoever
wrote it.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agents.evaluator.verdict import (
    ATTEMPTS,
    MAX_TURNS_SHOWN,
    Judge,
    read_json,
    turn_line,
)
from app.agents.planner_executer.executer import StepPlay
from app.agents.planner_executer.prompts import (
    FAILURE_ANALYSIS_ROLE,
    prompt_text,
    this_step,
)
from app.llm.chat_model import structured
from app.qa.channel import QaCancelled
from app.qa.envelope import IssueSeverity
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

# Inputs the reproduction may name. A play that sent more is a play nobody replays by hand.
MAX_REPRODUCTION_STEPS = 30
# Longest `cause`, `actual` and `insight` kept. The frame they ride is capped at 1 MiB on the far
# side, and a cause that needs more than this is a turn record, not a cause.
MAX_TEXT_CHARS = 2_000


class FailureCategory(StrEnum):
    """What kind of failure the step was. Only `GAME_DEFECT` becomes an `ISSUE`.

    `UNREACHED` and `AGENT_ERROR` are the engine's failures: play never got to the state the
    step checks, or the engine stopped on its own. `INCONCLUSIVE` is the honest answer when the
    screens cannot show the result either way — the same rule the verdict prompt follows: a
    result the evidence cannot show is not a pass, and here it is not a defect either.
    """

    GAME_DEFECT = "GAME_DEFECT"
    UNREACHED = "UNREACHED"
    INCONCLUSIVE = "INCONCLUSIVE"
    AGENT_ERROR = "AGENT_ERROR"


class FailureAnalysis(BaseModel):
    """The model's answer. `reproduction` holds numbers from the request's input list."""

    category: FailureCategory
    cause: str
    actual: str = ""
    reproduction: list[int] = Field(default_factory=list)
    insight: str = ""
    title: str | None = None
    severity: IssueSeverity | None = None


@dataclass(frozen=True)
class FailureReport:
    """One failed step's analysis, grounded: `reproduction` is sentences the play really sent.

    `engine_claimed` is whether the engine thought it had reached the goal (`done()` held on
    some turn). Against the Evaluator's verdict it measures how often the engine's own claim is
    right, which is the one number that says whether the engine or the judge needs work.
    """

    category: FailureCategory
    cause: str
    reproduction: tuple[str, ...]
    insight: str
    engine_claimed: bool
    actual: str = ""
    title: str | None = None
    severity: IssueSeverity | None = None

    @property
    def is_defect(self) -> bool:
        return self.category is FailureCategory.GAME_DEFECT

    def as_payload(self) -> dict[str, Any]:
        """What rides `STATUS.analysis`. `title` and `severity` only when the step is a defect."""
        payload: dict[str, Any] = {
            "category": self.category.value,
            "cause": self.cause,
            "reproduction": list(self.reproduction),
            "actual": self.actual,
            "insight": self.insight,
            "engine_claimed": self.engine_claimed,
        }
        if self.is_defect:
            payload["title"] = self.title
            payload["severity"] = self.severity.value if self.severity else None
        return payload


def sent_inputs(play: StepPlay) -> list[dict[str, Any]]:
    """Every input the play sent to the game, in order, numbered from 1.

    One entry per batch the macro host sent (`ChannelMacroHost.sent`): the sentence the macro
    runner wrote for it, whether the game took it, and the turn and action it came from.
    """
    inputs: list[dict[str, Any]] = []
    for turn in play.turns:
        for batch in turn.get("sent") or []:
            inputs.append(
                {
                    "n": len(inputs) + 1,
                    "turn": turn.get("turn"),
                    "action": turn.get("picked"),
                    "summary": str(batch.get("summary") or "(no summary)"),
                    "ok": bool(batch.get("ok")),
                    "error": batch.get("error"),
                    "reached": batch.get("reached"),
                    "blocked": batch.get("blocked"),
                }
            )
    return inputs


def reproduction_lines(inputs: list[dict[str, Any]], numbers: list[int] | None = None) -> tuple[str, ...]:
    """The sentences for `numbers` (all inputs when `None`), a refused one saying so."""
    chosen = inputs if numbers is None else [inputs[n - 1] for n in numbers]
    lines = []
    for item in chosen[:MAX_REPRODUCTION_STEPS]:
        line = item["summary"]
        if not item["ok"]:
            line += f" — the game refused it: {item['error'] or 'no reason given'}"
        lines.append(line)
    return tuple(lines)


def engine_claimed(play: StepPlay | None) -> bool:
    return play is not None and play.reached_goal()


def engine_error(message: str, play: StepPlay | None = None) -> FailureReport:
    """A step the engine stopped on its own: refused macros, Jev silent, no readable verdict."""
    return FailureReport(
        category=FailureCategory.AGENT_ERROR,
        cause=_clip(message),
        reproduction=reproduction_lines(sent_inputs(play)) if play is not None else (),
        insight=(
            "The engine, not the game, ended this step. Read the message for which part stopped "
            "and run the step again before reading anything about the game into it."
        ),
        engine_claimed=engine_claimed(play),
    )


def unreached(message: str, play: StepPlay | None = None) -> FailureReport:
    """A FREE case play never got to: the scene, the goal, or a plan that kept leaving it out."""
    return FailureReport(
        category=FailureCategory.UNREACHED,
        cause=_clip(message),
        reproduction=reproduction_lines(sent_inputs(play)) if play is not None else (),
        insight=(
            "Nothing here says the game is wrong: the case was not verified. Check that the case "
            "names the scene the game really shows and that its precondition can be reached from "
            "the start, then run it again."
        ),
        engine_claimed=engine_claimed(play),
    )


class FailureAnalyzer:
    """Asks the run's model why a judged step failed, from the same evidence the verdict read."""

    def __init__(self, config: RunConfig, judge: Judge | None = None) -> None:
        self._config = config
        self._system: str | None = None
        self._judge = judge or self._ask_model

    async def analyze(self, scenario: QaScenario, step: int, play: StepPlay, verdict: str) -> FailureReport:
        """The analysis of a step the Evaluator judged failed with `verdict` as its message.

        A step the engine stopped (`JEV-ERROR`) or the Evaluator could not judge
        (`EVALUATOR-ERROR`) is `AGENT_ERROR` without a model call. Everything else is asked;
        an answer that cannot be read or grounded is asked for once more, and a second one
        files the step as `AGENT_ERROR` naming the problem, the way the verdict does.
        """
        if play.stopped.startswith("JEV-ERROR") or verdict.startswith("EVALUATOR-ERROR"):
            return engine_error(verdict if verdict.startswith("EVALUATOR-ERROR") else play.stopped, play)
        if self._system is None:
            self._system = prompt_text(FAILURE_ANALYSIS_ROLE, self._config.prompt_version)
        inputs = sent_inputs(play)
        user = analysis_request(scenario, step, play, verdict, inputs)
        problem = ""
        for _ in range(ATTEMPTS):
            try:
                answer = _read(await self._judge(self._system, user))
                return ground(answer, inputs, play)
            except QaCancelled:
                raise
            except Exception as error:  # noqa: BLE001 - an unreadable or ungrounded answer is asked again
                problem = f"{type(error).__name__}: {str(error)[:200]}"
        return FailureReport(
            category=FailureCategory.AGENT_ERROR,
            cause=f"ANALYSIS-ERROR: no readable analysis in {ATTEMPTS} answers ({problem})",
            reproduction=reproduction_lines(inputs),
            insight="",
            engine_claimed=engine_claimed(play),
        )

    async def _ask_model(self, system: str, user: str) -> FailureAnalysis:
        chain = structured(self._config.model, FailureAnalysis, self._config.reasoning)
        return await chain.ainvoke([SystemMessage(system), HumanMessage(user)])


def ground(answer: FailureAnalysis, inputs: list[dict[str, Any]], play: StepPlay) -> FailureReport:
    """A `FailureReport` from the model's answer, or `ValueError` when it cannot be grounded.

    Two things are refused rather than repaired: a reproduction number outside the list the
    request gave, and a `GAME_DEFECT` without the `title` and `severity` an `ISSUE` needs. Both
    are asked for again; repairing them here would file a defect the model did not describe.
    """
    for number in answer.reproduction:
        if not 1 <= number <= len(inputs):
            raise ValueError(f"reproduction names input {number}, and the play sent {len(inputs)}")
    if answer.category is FailureCategory.GAME_DEFECT:
        if not (answer.title or "").strip():
            raise ValueError("a GAME_DEFECT needs a title")
        if answer.severity is None:
            raise ValueError("a GAME_DEFECT needs a severity")
    return FailureReport(
        category=answer.category,
        cause=_clip(answer.cause),
        reproduction=reproduction_lines(inputs, answer.reproduction),
        insight=_clip(answer.insight),
        engine_claimed=engine_claimed(play),
        actual=_clip(answer.actual),
        title=answer.title.strip() if answer.category is FailureCategory.GAME_DEFECT and answer.title else None,
        severity=answer.severity if answer.category is FailureCategory.GAME_DEFECT else None,
    )


def analysis_request(
    scenario: QaScenario, step: int, play: StepPlay, verdict: str, inputs: list[dict[str, Any]]
) -> str:
    """What the analyzer reads: the verdict's evidence plus the numbered inputs the play sent."""
    shown = play.turns[-MAX_TURNS_SHOWN:]
    skipped = len(play.turns) - len(shown)
    turns = "\n".join(turn_line(turn) for turn in shown) or "  (no turn was played)"
    listed = "\n".join(_input_line(item) for item in inputs) or "  (no input was sent)"
    return (
        f"The step:\n{this_step(scenario, step)}\n\n"
        f"The evaluator's verdict: FAILED — {verdict}\n\n"
        f"Why play stopped: {play.stopped}\n\n"
        f"Turns played ({len(play.turns)}"
        + (f", the first {skipped} left out" if skipped else "")
        + f"):\n{turns}\n\n"
        f"Inputs the play sent to the game, in order ({len(inputs)}). Cite them by number in "
        f"`reproduction`:\n{listed}\n\n"
        f"Screen when the step began:\n{play.start_screen}\n\n"
        f"Screen when play stopped:\n{play.end_screen}\n"
    )


def _input_line(item: dict[str, Any]) -> str:
    line = f"  {item['n']}. [turn {item['turn']}, {item['action']}] {item['summary']} — "
    line += "ok" if item["ok"] else f"REFUSED: {item['error'] or 'no reason given'}"
    if item.get("blocked"):
        line += f"; the press was swallowed by {item['blocked']}"
    elif item.get("reached") == "nothing":
        line += "; the press reached nothing"
    return line


def _read(answer: Any) -> FailureAnalysis:
    if isinstance(answer, FailureAnalysis):
        return answer
    return FailureAnalysis.model_validate(read_json(answer))


def _clip(text: str | None) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return f"{text[:MAX_TEXT_CHARS]}… (+{len(text) - MAX_TEXT_CHARS} chars)"


__all__ = [
    "MAX_REPRODUCTION_STEPS",
    "FailureAnalysis",
    "FailureAnalyzer",
    "FailureCategory",
    "FailureReport",
    "analysis_request",
    "engine_error",
    "ground",
    "reproduction_lines",
    "sent_inputs",
    "unreached",
]
