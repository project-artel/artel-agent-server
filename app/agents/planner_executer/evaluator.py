"""The evaluator: judges a step after play stops, with the run's model.

It reads the step (what to do and the result to verify), why play stopped, the turns played —
the screen's scene, `done()` and what Jev said about it, the action picked and how its macro
ended — and the screen when the step began and when play stopped. It answers
`{"passed": bool, "reason": str}` through `structured`, the repo's one place that picks the
structured-output method per route. An answer that cannot be read is asked for once more; a
second one fails the step with `EVALUATOR-ERROR`.

The verdict goes out the way the agent's `report_step` sends it — a per-step `STATUS` with no
`result`, the case id and whether the step is its case's verification step — so Orchestration
records both engines' steps the same way.
"""

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from app.agents.planner_executer.executer import StepPlay
from app.agents.planner_executer.prompts import EVALUATOR_ROLE, prompt_text, this_step
from app.agents.qa.tools import QaRunState
from app.llm.chat_model import structured
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import MessageType, StatusPayload, StepStatus
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario, QaStepResult

# Tries per step: the first answer, and one more when it cannot be read.
ATTEMPTS = 2
# Turns shown; a long step keeps its last ones, where it stopped.
MAX_TURNS_SHOWN = 60

_JSON = re.compile(r"\{.*\}", re.DOTALL)

# Sends the system and user text and returns the answer: a `Verdict`, a dict or a JSON string.
Judge = Callable[[str, str], Awaitable[Any]]


class Verdict(BaseModel):
    passed: bool
    reason: str


class Evaluator:
    def __init__(self, config: RunConfig, judge: Judge | None = None) -> None:
        self._config = config
        self._system = prompt_text(EVALUATOR_ROLE, config.prompt_version)
        self._judge = judge or self._ask_model

    async def judge(self, scenario: QaScenario, step: int, play: StepPlay) -> tuple[bool, str]:
        """`(passed, message)` for the step. The message names the stop when it was not `done`."""
        user = request(scenario, step, play)
        problem = ""
        for _ in range(ATTEMPTS):
            try:
                verdict = _read(await self._judge(self._system, user))
            except QaCancelled:
                raise
            except Exception as error:  # noqa: BLE001 - an unreadable answer is asked again
                problem = f"{type(error).__name__}: {str(error)[:200]}"
                continue
            note = "" if play.stopped == "done" else f" (executer stopped: {play.stopped})"
            return verdict.passed, verdict.reason + note
        return False, f"EVALUATOR-ERROR: no readable verdict in {ATTEMPTS} answers ({problem})"

    async def _ask_model(self, system: str, user: str) -> Verdict:
        chain = structured(self._config.model, Verdict, self._config.reasoning)
        return await chain.ainvoke([SystemMessage(system), HumanMessage(user)])


def request(scenario: QaScenario, step: int, play: StepPlay) -> str:
    """What the evaluator reads about one step."""
    shown = play.turns[-MAX_TURNS_SHOWN:]
    skipped = len(play.turns) - len(shown)
    turns = "\n".join(_turn_line(turn) for turn in shown) or "  (no turn was played)"
    return (
        f"The step:\n{this_step(scenario, step)}\n\n"
        f"Why play stopped: {play.stopped}\n\n"
        f"Turns played ({len(play.turns)}"
        + (f", the first {skipped} left out" if skipped else "")
        + f"):\n{turns}\n\n"
        f"Screen when the step began:\n{play.start_screen}\n\n"
        f"Screen when play stopped:\n{play.end_screen}\n"
    )


def _turn_line(turn: dict) -> str:
    parts = [f"  turn {turn['turn']} on {turn.get('scene')}: done()={turn.get('done')}"]
    if turn.get("done_error"):
        parts.append(f"(done() could not be read: {turn['done_error']})")
    if "goal_ask" in turn:
        parts.append(f"Jev on the goal being reached: {turn['goal_ask']}")
    if "picked" in turn:
        parts.append(f"Jev picked {turn['picked']}")
    if "outcome" in turn:
        parts.append(f"→ {turn['outcome']}" + (f": {turn['reason']}" if turn.get("reason") else ""))
    if turn.get("jev_error"):
        parts.append(f"Jev did not answer: {turn['jev_error']}")
    return "; ".join(parts)


def _read(answer: Any) -> Verdict:
    """A `Verdict` from what the model returned. Raises when it is not one."""
    if isinstance(answer, Verdict):
        return answer
    if isinstance(answer, dict):
        return Verdict.model_validate(answer)
    match = _JSON.search(str(answer))
    if match is None:
        raise ValueError(f"no JSON object in {str(answer)[:120]!r}")
    return Verdict.model_validate(json.loads(match.group(0)))


async def report(
    channel: QaRunChannel, state: QaRunState, step: int, passed: bool, message: str
) -> None:
    """Record and send one step's verdict, in the shape `report_step` sends it."""
    case_id, is_verification = (
        state.step_meta[step - 1] if 0 <= step - 1 < len(state.step_meta) else (None, False)
    )
    state.step_results.append(
        QaStepResult(
            step=step, passed=passed, message=message, case_id=case_id, is_verification=is_verification
        )
    )
    await channel.emit(
        MessageType.STATUS,
        StatusPayload(
            status=StepStatus.COMPLETED if passed else StepStatus.FAILED,
            step=step,
            case_id=case_id,
            is_verification=is_verification,
            message=message,
        ),
    )
