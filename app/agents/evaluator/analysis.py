"""Failure analysis: why a step the Evaluator failed went wrong, read off the play that failed it.

Four things for a failed step — a category, the cause, how to see it again, and what to look at
— so that a person reading the run does not have to open the turn records in `qa_log`. The
analysis reads the play the executer kept in the process (`StepPlay`), not the log: `qa_log`
holds a clipped copy (`MAX_TURN_LOG_BYTES`), and a diagnosis made from a clipped record has
already been wrong once (ARTEL-824).

The reproduction is written by the model, in the run's language, from a source the code made
complete. Every input the play sent is listed with what a player saw — the scene, the kind of
input, the text on the object it landed on, the key pressed — and the model answers one sentence
per input, bound to that input's number. The code then holds the sentence to its source: the
number must exist, the text the input landed on must appear in the sentence as the screen showed
it, and nothing a person cannot see — a selector path, a prefab name, a macro call — may appear.
A sentence that fails is asked for once more; a second failure files the step as `AGENT_ERROR`.
That is what lets one list be both readable and, with its sources beside it, something a person
can follow.

Two kinds of failure never reach the model. A step the engine itself stopped — the planner's
macros refused, Jev not answering, the evaluator unable to read a verdict — is `AGENT_ERROR`
by construction, and a FREE case that never reached its goal or its scene is `UNREACHED`;
both are filled in here by code, with the same shape, so the record reads the same whoever
wrote it.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agents.evaluator.verdict import ATTEMPTS, MAX_TURNS_SHOWN, Judge, read_json, turn_line
from app.agents.planner_executer.executer import StepPlay
from app.agents.planner_executer.prompts import FAILURE_ANALYSIS_ROLE, LANGUAGE_PLACEHOLDER, prompt_text, this_step
from app.agents.qa.prompt import LANGUAGE_DIRECTIVES
from app.llm.chat_model import structured
from app.qa.channel import QaCancelled
from app.qa.envelope import IssueSeverity
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

# Inputs the reproduction may name. A play that sent more is a play nobody replays by hand.
MAX_REPRODUCTION_STEPS = 30
# Longest `cause`, `actual`, `insight` and one reproduction sentence kept. The frame they ride is
# capped at 1 MiB on the far side, and a cause that needs more than this is a turn record.
MAX_TEXT_CHARS = 2_000
MAX_SENTENCE_CHARS = 300

# What a person cannot see on screen and therefore cannot follow: a prefab suffix, a hierarchy
# index, a macro or reader call. A sentence carrying one of these was written off the code, not
# off the screen, and is refused.
_CODE_IN_SENTENCE = re.compile(
    r"\(Clone\)|\[\d+\]|"
    r"\b(?:click|double_click|drag|move_pointer|press_key|enter_text|hold_key|release_key|"
    r"hold_mouse_button|release_mouse_button|set_input_axis|set_input_button|find|selector|"
    r"member|static|scene|wait_until|require)\s*\("
)


class FailureCategory(StrEnum):
    """What kind of failure the step was.

    `UNREACHED` and `AGENT_ERROR` are the engine's failures: play never got to the state the
    step checks, or the engine stopped on its own. `INCONCLUSIVE` is the honest answer when the
    screens cannot show the result either way — the same rule the verdict prompt follows: a
    result the evidence cannot show is not a pass, and here it is not a defect either.
    `GAME_DEFECT` is the model's recommendation; a person turns it into an issue, or does not.
    """

    GAME_DEFECT = "GAME_DEFECT"
    UNREACHED = "UNREACHED"
    INCONCLUSIVE = "INCONCLUSIVE"
    AGENT_ERROR = "AGENT_ERROR"


class ReproductionLine(BaseModel):
    """One sentence of the reproduction, bound to the numbered input it describes."""

    n: int
    text: str


class FailureAnalysis(BaseModel):
    """The model's answer."""

    category: FailureCategory
    cause: str
    actual: str = ""
    reproduction: list[ReproductionLine] = Field(default_factory=list)
    insight: str = ""
    # Suggested for the issue a person may file from a `GAME_DEFECT`; `None` for the rest.
    title: str | None = None
    severity: IssueSeverity | None = None


@dataclass(frozen=True)
class FailureReport:
    """One failed step's analysis, with the reproduction held to the inputs the play really sent.

    `reproduction` is what a person reads; `inputs` is what the code knows each sentence was
    written about, kept beside it so a reader can check one against the other.

    `engine_claimed` is whether the engine thought it had reached the goal (`done()` held on
    some turn). Against the Evaluator's verdict, and against the person's approval later, it is
    one axis of the confusion matrix that says whether the engine or the judge needs work.
    """

    category: FailureCategory
    cause: str
    reproduction: tuple[str, ...]
    insight: str
    engine_claimed: bool
    inputs: tuple[dict[str, Any], ...] = field(default_factory=tuple)
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
            "reproduction_inputs": [_input_payload(item) for item in self.inputs],
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

    One entry per batch the macro host sent (`ChannelMacroHost.sent`): what a player saw —
    scene, kind, the text on the object, the key, the typed value — plus the source line the
    macro runner wrote for it and what the game answered.
    """
    inputs: list[dict[str, Any]] = []
    for turn in play.turns:
        for batch in turn.get("sent") or []:
            inputs.append(
                {
                    "n": len(inputs) + 1,
                    "turn": turn.get("turn"),
                    "action": turn.get("picked"),
                    "scene": batch.get("scene") or turn.get("scene"),
                    "kind": batch.get("kind") or "other",
                    "target": batch.get("target"),
                    "target_name": batch.get("target_name"),
                    "target_text": batch.get("target_text"),
                    "to_text": batch.get("to_text"),
                    "key": batch.get("key"),
                    "value": batch.get("value"),
                    "source": str(batch.get("summary") or ""),
                    "ok": bool(batch.get("ok")),
                    "error": batch.get("error"),
                    "reached": batch.get("reached"),
                    "blocked": batch.get("blocked"),
                }
            )
    return inputs


def input_result(item: dict[str, Any]) -> str:
    """What the game did with the input, in one phrase."""
    if not item["ok"]:
        return f"refused: {item['error'] or 'no reason given'}"
    if item.get("blocked"):
        return f"blocked by {item['blocked']}"
    if item.get("reached") == "nothing":
        return "landed on nothing"
    return "ok"


def _input_payload(item: dict[str, Any]) -> dict[str, Any]:
    """The source of one sentence, as `reproduction_inputs` carries it."""
    return {
        "n": item["n"],
        "scene": item.get("scene"),
        "kind": item["kind"],
        "target_text": item.get("target_text"),
        "target_name": item.get("target_name"),
        "to_text": item.get("to_text"),
        "key": item.get("key"),
        "value": item.get("value"),
        "result": input_result(item),
        "source": item["source"],
    }


def engine_claimed(play: StepPlay | None) -> bool:
    return play is not None and play.reached_goal()


def engine_error(message: str, play: StepPlay | None = None) -> FailureReport:
    """A step the engine stopped on its own: refused macros, Jev silent, no readable verdict."""
    return FailureReport(
        category=FailureCategory.AGENT_ERROR,
        cause=_clip(message),
        reproduction=(),
        inputs=tuple(sent_inputs(play)) if play is not None else (),
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
        reproduction=(),
        inputs=tuple(sent_inputs(play)) if play is not None else (),
        insight=(
            "Nothing here says the game is wrong: the case was not verified. Check that the case "
            "names the scene the game really shows and that its precondition can be reached from "
            "the start, then run it again."
        ),
        engine_claimed=engine_claimed(play),
    )


def unobservable(missing: list[dict[str, Any]], message: str, play: StepPlay | None = None) -> FailureReport:
    """A case whose `observe` names the SDK could not resolve: the verdict read a screen that was
    never going to show the value, so the failure says nothing about the game either way."""
    named = ", ".join(f"`{item.get('name')}` ({item.get('reason')})" for item in missing)
    return FailureReport(
        category=FailureCategory.INCONCLUSIVE,
        cause=_clip(f"UNOBSERVABLE: the SDK could not find {named}; the verdict was: {message}"),
        reproduction=(),
        inputs=tuple(sent_inputs(play)) if play is not None else (),
        insight=(
            "Fix the case's observe name so it names a field, property or static of a game type "
            "(`Type::member`), or mark the member in the game so the SDK can reach it, then run "
            "the case again. Nothing here says the game is wrong."
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
        an answer that cannot be read or held to its sources is asked for once more, and a
        second one files the step as `AGENT_ERROR` naming the problem, the way the verdict does.
        """
        if play.stopped.startswith("JEV-ERROR") or verdict.startswith("EVALUATOR-ERROR"):
            return engine_error(verdict if verdict.startswith("EVALUATOR-ERROR") else play.stopped, play)
        if self._system is None:
            self._system = prompt_text(
                FAILURE_ANALYSIS_ROLE,
                self._config.prompt_version,
                **{LANGUAGE_PLACEHOLDER: LANGUAGE_DIRECTIVES[self._config.language]},
            )
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
            reproduction=(),
            inputs=tuple(inputs),
            insight="",
            engine_claimed=engine_claimed(play),
        )

    async def _ask_model(self, system: str, user: str) -> FailureAnalysis:
        chain = structured(self._config.model, FailureAnalysis, self._config.reasoning)
        return await chain.ainvoke([SystemMessage(system), HumanMessage(user)])


def ground(answer: FailureAnalysis, inputs: list[dict[str, Any]], play: StepPlay) -> FailureReport:
    """A `FailureReport` from the model's answer, or `ValueError` when a sentence is not held by its source.

    Refused rather than repaired, and asked for again: a number outside the list, a number used
    twice, more sentences than `MAX_REPRODUCTION_STEPS`, a sentence carrying code a player cannot
    see, a sentence that does not name the text its input landed on as the screen showed it, and
    a `GAME_DEFECT` without the title and severity its issue draft needs. Repairing any of them
    here would file a reproduction the model did not write about the input it names.
    """
    if len(answer.reproduction) > MAX_REPRODUCTION_STEPS:
        raise ValueError(f"reproduction has {len(answer.reproduction)} lines; at most {MAX_REPRODUCTION_STEPS}")
    seen: set[int] = set()
    sentences: list[str] = []
    for line in answer.reproduction:
        if not 1 <= line.n <= len(inputs):
            raise ValueError(f"reproduction names input {line.n}, and the play sent {len(inputs)}")
        if line.n in seen:
            raise ValueError(f"reproduction names input {line.n} twice")
        seen.add(line.n)
        text = " ".join(line.text.split())
        if not text:
            raise ValueError(f"reproduction line {line.n} is empty")
        check_sentence(text, inputs[line.n - 1], inputs)
        sentences.append(_clip(text, MAX_SENTENCE_CHARS))
    if answer.category is FailureCategory.GAME_DEFECT:
        if not (answer.title or "").strip():
            raise ValueError("a GAME_DEFECT needs a title")
        if answer.severity is None:
            raise ValueError("a GAME_DEFECT needs a severity")
    return FailureReport(
        category=answer.category,
        cause=_clip(answer.cause),
        reproduction=tuple(sentences),
        inputs=tuple(inputs[line.n - 1] for line in answer.reproduction),
        insight=_clip(answer.insight),
        engine_claimed=engine_claimed(play),
        actual=_clip(answer.actual),
        title=answer.title.strip() if answer.category is FailureCategory.GAME_DEFECT and answer.title else None,
        severity=answer.severity if answer.category is FailureCategory.GAME_DEFECT else None,
    )


def check_sentence(text: str, item: dict[str, Any], inputs: list[dict[str, Any]]) -> None:
    """`ValueError` when `text` was not written off what a player saw of `item`.

    Two checks. Nothing from the code may appear: a `(Clone)` suffix, a `[n]` index, a macro or
    reader call, or the selector path of any input. And what the screen showed must appear: the
    text on the object the input landed on (and, for a drag, the text on the destination), or the
    key pressed, exactly as the list gave it, so a person can find it on the screen. An input whose
    object has no text — a sprite — has nothing to hold the sentence to, and only the first check
    applies.
    """
    if _CODE_IN_SENTENCE.search(text):
        raise ValueError(f"reproduction line {item['n']} carries code a player cannot see: {text[:80]!r}")
    for other in inputs:
        for path in (other.get("target"), other.get("reached")):
            if isinstance(path, str) and len(path) > 3 and "/" in path and path in text:
                raise ValueError(f"reproduction line {item['n']} carries a selector path: {path}")
    lowered = text.casefold()
    required = [item.get("target_text")]
    if item["kind"] == "drag":
        required.append(item.get("to_text"))
    if item["kind"] in ("key", "key_down", "key_up"):
        required.append(item.get("key"))
    for needle in required:
        if needle and needle.strip().casefold() not in lowered:
            raise ValueError(f"reproduction line {item['n']} does not name {needle!r}, which the input landed on")


def analysis_request(
    scenario: QaScenario, step: int, play: StepPlay, verdict: str, inputs: list[dict[str, Any]]
) -> str:
    """What the analyzer reads: the verdict's evidence plus the numbered inputs the play sent."""
    shown = play.turns[-MAX_TURNS_SHOWN:]
    skipped = len(play.turns) - len(shown)
    turns = "\n".join(turn_line(turn) for turn in shown) or "  (no turn was played)"
    listed = "\n".join(input_line(item) for item in inputs) or "  (no input was sent)"
    return (
        f"The step:\n{this_step(scenario, step)}\n\n"
        f"The evaluator's verdict: FAILED — {verdict}\n\n"
        f"Why play stopped: {play.stopped}\n\n"
        f"Turns played ({len(play.turns)}"
        + (f", the first {skipped} left out" if skipped else "")
        + f"):\n{turns}\n\n"
        f"Inputs the play sent to the game, in order ({len(inputs)}). Each line: number, scene, what was "
        f"done and to what — the text on the object as the screen showed it, in quotes — and what the game "
        f"did with it. Write `reproduction` from these lines and cite each by its number:\n{listed}\n\n"
        f"Screen when the step began:\n{play.start_screen}\n\n"
        f"Screen when play stopped:\n{play.end_screen}\n"
    )


def input_line(item: dict[str, Any]) -> str:
    """One input as the model reads it. The text in quotes is what the sentence has to keep."""
    kind = item["kind"]
    what = _target_phrase(item)
    if kind == "drag":
        what = f"drag {what} to {_quoted(item.get('to_text')) or 'an object with no text'}"
    elif kind in ("key", "key_down", "key_up"):
        verb = {"key": "press key", "key_down": "hold key", "key_up": "release key"}[kind]
        what = f"{verb} {_quoted(item.get('key')) or '?'}"
    elif kind == "text":
        what = f"type {_quoted(item.get('value'))} into {what}"
    elif kind in ("click", "double_click", "move"):
        what = f"{kind.replace('_', ' ')} {what}"
    else:
        what = f"{kind}: {item['source']}"
    return f"  {item['n']}. [{item.get('scene') or '?'}] {what} — {input_result(item)}  (source: {item['source']})"


def _target_phrase(item: dict[str, Any]) -> str:
    text = _quoted(item.get("target_text"))
    if text:
        return text
    if item.get("target_name"):
        return f"an object named {item['target_name']} with no text on it"
    return "a screen point"


def _quoted(value: Any) -> str:
    return f'"{value}"' if value not in (None, "") else ""


def _read(answer: Any) -> FailureAnalysis:
    if isinstance(answer, FailureAnalysis):
        return answer
    return FailureAnalysis.model_validate(read_json(answer))


def _clip(text: str | None, limit: int = MAX_TEXT_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… (+{len(text) - limit} chars)"


__all__ = [
    "MAX_REPRODUCTION_STEPS",
    "FailureAnalysis",
    "FailureAnalyzer",
    "FailureCategory",
    "FailureReport",
    "ReproductionLine",
    "analysis_request",
    "check_sentence",
    "engine_error",
    "ground",
    "input_line",
    "input_result",
    "sent_inputs",
    "unreached",
]
