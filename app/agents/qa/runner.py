"""The QA run as a tool loop.

The agent decides what to do next and reaches for a tool to do it. Nothing here
polls or waits on the game's initiative — that was the previous design's flaw:
a game that never volunteered its state left the run idle forever.
"""

import asyncio
import json

from langchain.agents import create_agent

from app.agents.qa.prompt import LANGUAGE_DIRECTIVES
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.scenario import DEFAULT_LANGUAGE, OutputLanguage, ScenarioDraft
from app.llm.chat_model import build_chat_model
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.qa.channel import QaCancelled, QaRunChannel

# Two bounds, because either alone leaves a hole. A call cap alone lets one
# unanswered call hold the run open; a clock alone lets a fast loop burn budget.
BASE_TOOL_CALLS = 10
TOOL_CALLS_PER_STEP = 15
RUN_DEADLINE_SECONDS = 600.0


SYSTEM_PROMPT = (
    "You are a QA agent executing an approved test scenario against a live Unity "
    "game, step by step, using tools.\n"
    "\n"
    "How to work:\n"
    "1. Call `observe_scene` before acting. You cannot act on a screen you have "
    "not seen, and ids only mean anything in the scene you just observed.\n"
    "2. Carry out the step's `action` with `perform_actions`. Choose the method "
    "and the target id from what is actually on screen — never invent an id.\n"
    "3. Call `observe_scene` again to see what your action did. The result is "
    "written as what CHANGED, which is the evidence the step's `expected` is "
    "about.\n"
    "4. Call `report_step` with your verdict and the evidence you saw.\n"
    "5. Repeat for every step, then call `finish_run` exactly once.\n"
    "\n"
    "Available SDK methods (the set grows; pick whichever fits):\n"
    "- button_click, target_id = the button's id, no arguments\n"
    "- enter_text, target_id = the field's id, arguments [value]\n"
    "- key_click, no target_id, arguments [keyCode, durationSeconds]\n"
    "\n"
    "If the screen is not ready — loading, animating, counting down — call "
    "`observe_scene` again with `wait_seconds` rather than acting into it. If the "
    "game stops answering, decide for yourself whether to wait once more or judge "
    "the step failed; do not loop on it forever.\n"
    "\n"
    "The operator may speak to you mid-run. Their words are appended to tool "
    "results. Treat an instruction as binding from that point on, and answer a "
    "question with `reply_to_operator` — never with an action.\n"
    "\n"
    "{language_directive}"
)


def _first_message(scenario: ScenarioDraft) -> str:
    steps = [
        {
            "step": step.step,
            "title": step.title,
            "state": step.state,
            "action": step.action,
            "expected": step.expected,
        }
        for step in scenario.steps
    ]
    return (
        f"Scenario: {scenario.title} — {scenario.description}\n\n"
        f"Steps to execute in order:\n{json.dumps(steps, ensure_ascii=False, indent=2)}\n\n"
        "Begin. Observe the screen first."
    )


class QaRunner:
    """Runs one scenario to completion over one channel."""

    def __init__(
        self,
        model: LLMModel = DEFAULT_MODEL,
        language: OutputLanguage = DEFAULT_LANGUAGE,
        deadline_seconds: float = RUN_DEADLINE_SECONDS,
    ) -> None:
        self._model = model
        self._language = language
        self._deadline = deadline_seconds

    def _tool_call_limit(self, steps: int) -> int:
        return BASE_TOOL_CALLS + TOOL_CALLS_PER_STEP * max(steps, 1)

    async def run(
        self, channel: QaRunChannel, scenario: ScenarioDraft, state: QaRunState
    ) -> None:
        agent = create_agent(
            model=build_chat_model(self._model),
            tools=build_tools(channel, state),
            system_prompt=SYSTEM_PROMPT.format(
                language_directive=LANGUAGE_DIRECTIVES[self._language]
            ),
        )
        await agent.ainvoke(
            {"messages": [("user", _first_message(scenario))]},
            # recursion_limit counts graph steps, so it bounds tool calls as well
            # as the model turns between them.
            {"recursion_limit": self._tool_call_limit(len(scenario.steps)) * 2},
        )

    async def run_with_deadline(
        self, channel: QaRunChannel, scenario: ScenarioDraft
    ) -> tuple[QaRunState, str | None]:
        """Returns the state and, when the run did not close cleanly, why.

        The state is built here and handed down so that a run cut short by the
        deadline still carries the verdicts it managed to record.
        """
        state = QaRunState(total_steps=len(scenario.steps))
        try:
            await asyncio.wait_for(
                self.run(channel, scenario, state), timeout=self._deadline
            )
        except asyncio.TimeoutError:
            return state, f"The run exceeded its {int(self._deadline)}s limit."
        except QaCancelled:
            return state, None
        except Exception as error:  # noqa: BLE001 - the reason has to reach the timeline
            return state, f"The run stopped on an error: {error}"
        if not state.finished:
            return state, "The agent stopped without closing the run."
        return state, None
