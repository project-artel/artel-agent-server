"""The QA run as a tool loop.

The agent decides what to do next and reaches for a tool to do it. Nothing here
polls or waits on the game's initiative — that was the previous design's flaw:
a game that never volunteered its state left the run idle forever.
"""

import asyncio
import json
import logging

from langchain.agents import create_agent

from app.agents.qa.prompt import LANGUAGE_DIRECTIVES
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.scenario import DEFAULT_LANGUAGE, OutputLanguage, ScenarioDraft
from app.llm.chat_model import build_chat_model
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import LogCategory

logger = logging.getLogger(__name__)

# A scene view runs long. Cut it for the console, and say it was cut — a silently
# truncated log reads as a smaller context than the model actually saw.
MAX_LOGGED_CHARS = 4000

# Two bounds, because either alone leaves a hole. A call cap alone lets one
# unanswered call hold the run open; a clock alone lets a fast loop burn budget.
BASE_TOOL_CALLS = 10
TOOL_CALLS_PER_STEP = 15
RUN_DEADLINE_SECONDS = 600.0

# Content-block types that carry the model's own reasoning, and the keys those
# blocks put it under. Providers disagree on both.
_REASONING_BLOCK_TYPES = ("text", "thinking", "reasoning")
_REASONING_KEYS = ("text", "thinking", "reasoning", "reasoning_content")


SYSTEM_PROMPT = (
    "You are a QA agent executing an approved test scenario against a live Unity "
    "game, step by step, using tools.\n"
    "\n"
    "How to work:\n"
    "1. Call `observe_scene` before acting. You cannot act on a screen you have "
    "not seen, and ids only mean anything in the scene you just observed.\n"
    "2. Carry out the step's `action` with `click_button`, `enter_text`, "
    "`press_key`, or the pointer and hold tools described below. Take ids from "
    "the scene you just observed — never invent one.\n"
    "3. Each of those returns the outcome AND the scene it produced, written as "
    "what CHANGED. That is the evidence the step's `expected` is about; you "
    "usually do not need a separate observation afterwards.\n"
    "4. Call `report_step` with your verdict and the evidence you saw.\n"
    "5. Repeat for every step, then call `finish_run` exactly once.\n"
    "\n"
    "Every tool takes a `thought` — why you are doing this, in one line. It is "
    "written to the run's timeline, and it is the only record of your reasoning "
    "a reviewer will ever see. Most tools also take `step`, the scenario step "
    "the call belongs to; pass the number from the step list, not a guess.\n"
    "\n"
    "A screen with nothing clickable is not a dead end. Dialogue, narration and "
    "cutscenes usually advance on a key — `press_key` needs no target and works "
    "when the scene lists no interactables at all. Reach for it before concluding "
    "that a step cannot be done.\n"
    "\n"
    "Neither is a target the scene gives no id for. The scene prints each element "
    "as `@ x,y wxh` — `x,y` is its CENTRE, the point to aim at, and `wxh` its "
    "size. Those numbers go into `move_pointer` and `drag_pointer` VERBATIM: the "
    "tools take exactly the pixels the scene reports, so never convert, flip or "
    "recompute them. `move_pointer` hovers, `drag_pointer` drags one point onto "
    "another. An element marked `(off screen)` has no position you can aim at — "
    "bring it into view first.\n"
    "\n"
    "The scene also lists what it cannot offer as an action, under `on screen:` — "
    "backgrounds, portraits, sprites. Anything printed there with coordinates can "
    "still be pressed or dragged with the pointer tools, exactly like an element "
    "from the actionable list; only its id is useless, since nothing takes it. "
    "Dragging a sprite that is not a button is reachable no other way, so look "
    "there before deciding the step's target does not exist.\n"
    "\n"
    "`hold_mouse_button`, `hold_key` and their `release_` partners are for state "
    "the game reads as held — walking with a key down, a press that must outlast "
    "several moves. Whatever you hold, release it in the same step, before you "
    "report a verdict: a button or key left down poisons every step after it. "
    "When a plain drag is all you need, use `drag_pointer` rather than holding "
    "the button yourself — it sends the whole press-move-release as one batch the "
    "game runs in order, so it cannot be left half-done.\n"
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


def _clip(text: str) -> str:
    if len(text) <= MAX_LOGGED_CHARS:
        return text
    return f"{text[:MAX_LOGGED_CHARS]}\n… [{len(text) - MAX_LOGGED_CHARS} more characters]"


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
        system_prompt = SYSTEM_PROMPT.format(
            language_directive=LANGUAGE_DIRECTIVES[self._language]
        )
        first_message = _first_message(scenario)
        tools = build_tools(channel, state)

        # The whole starting context in one place. Reading a run afterwards means
        # knowing what the model was actually given, and the prompt is assembled
        # from several pieces that are otherwise only visible in source.
        logger.info(
            "[QA] run starting\n"
            "  model=%s language=%s steps=%d deadline=%.0fs tools=%s\n"
            "--- system prompt ---\n%s\n"
            "--- first message ---\n%s",
            self._model,
            self._language,
            len(scenario.steps),
            self._deadline,
            [tool.name for tool in tools],
            system_prompt,
            _clip(first_message),
        )

        agent = create_agent(
            model=build_chat_model(self._model),
            tools=tools,
            system_prompt=system_prompt,
        )
        # Streamed rather than invoked so the model's reasoning can be put on the
        # timeline as it happens. Left to a tool the agent chooses to call, the
        # reasoning simply never appears — it has no reason to narrate itself, and
        # a run that only shows actions gives no way to tell a considered decision
        # from a lucky one.
        async for update in agent.astream(
            {"messages": [("user", _first_message(scenario))]},
            # recursion_limit counts graph steps, so it bounds tool calls as well
            # as the model turns between them.
            {"recursion_limit": self._tool_call_limit(len(scenario.steps)) * 2},
            stream_mode="updates",
        ):
            await self._log_reasoning(channel, update)

    @staticmethod
    def _text_of(message) -> str:
        """The assistant's own words, without the tool-call scaffolding.

        Reasoning is not always a `text` block. Anthropic emits `thinking`,
        several models fronted by OpenRouter emit `reasoning`, and some carry it
        beside the content in `additional_kwargs` rather than inside it. Reading
        only `text` is why a whole run reached the timeline with no reasoning on
        it at all.
        """
        parts: list[str] = []

        content = getattr(message, "content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") not in _REASONING_BLOCK_TYPES:
                    continue
                # The payload key is usually the block's own type, but `text` is
                # used for reasoning blocks too. Take whichever is present.
                for key in _REASONING_KEYS:
                    value = block.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value)
                        break

        extra = getattr(message, "additional_kwargs", None) or {}
        for key in _REASONING_KEYS:
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
                break

        seen: list[str] = []
        for part in parts:
            stripped = part.strip()
            # A provider that sends reasoning both ways would otherwise log twice.
            if stripped and stripped not in seen:
                seen.append(stripped)
        return "\n".join(seen)

    async def _log_reasoning(self, channel: QaRunChannel, update: dict) -> None:
        """Put each model turn on the timeline, and the whole exchange on the console.

        The timeline carries what a reviewer needs; the console carries what a
        developer needs — including the tool results, which are most of what the
        model is actually reading and are invisible everywhere else.
        """
        for node in update.values():
            if not isinstance(node, dict):
                continue
            for message in node.get("messages", []) or []:
                kind = getattr(message, "type", None)

                if kind == "tool":
                    logger.info(
                        "[QA] tool result ← %s\n%s",
                        getattr(message, "name", "?"),
                        _clip(str(getattr(message, "content", ""))),
                    )
                    continue

                # Tool results are already visible as their own frames; echoing
                # them to the timeline would bury the reasoning in what it
                # reasoned about.
                if kind != "ai":
                    continue

                text = self._text_of(message)
                calls = getattr(message, "tool_calls", None) or []
                logger.info(
                    "[QA] model turn\n  text: %s\n  calls: %s",
                    _clip(text) if text else "(none)",
                    [{"name": call.get("name"), "args": call.get("args")} for call in calls]
                    or "(none)",
                )
                if text:
                    await channel.note(text, LogCategory.THOUGHT)

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
