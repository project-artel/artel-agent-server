"""The QA run as a tool loop.

The agent decides what to do next and reaches for a tool to do it. Nothing here
polls or waits on the game's initiative — that was the previous design's flaw:
a game that never volunteered its state left the run idle forever.
"""

import asyncio
import json
import logging

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call

from app.agents.qa.context import fold_stale_scenes
from app.agents.qa.knowledge import (
    MAX_FORGETS_PER_RUN,
    MAX_RECORDS_PER_RUN,
    MAX_SEARCHES_PER_RUN,
)
from app.agents.qa.prompt import LANGUAGE_DIRECTIVES
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.vision import QaCaptureVisionMiddleware
from app.agents.scenario import DEFAULT_LANGUAGE, OutputLanguage, ScenarioDraft
from app.llm.chat_model import build_chat_model
from app.llm.models import DEFAULT_MODEL, LLMModel, get_model_spec
from app.prompts import load_prompt
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import LogCategory

logger = logging.getLogger(__name__)

# Directory under app/prompts/ holding this agent's prompt versions.
PROMPT_AGENT = "qa_run"

# A scene view runs long. Cut it for the console, and say it was cut — a silently
# truncated log reads as a smaller context than the model actually saw.
MAX_LOGGED_CHARS = 4000

# Two bounds, because either alone leaves a hole. A call cap alone lets one
# unanswered call hold the run open; a clock alone lets a fast loop burn budget.
#
# The split is by what the calls are for: BASE covers what a run spends
# regardless of length — the opening observation, `finish_run`, and the
# per-run allowances the tools cap themselves at — while PER_STEP covers the
# work of one scenario step. So BASE grows when a new run-level allowance is
# added; left alone, `search_knowledge` would have taken its budget out of the
# steps and shortened every scenario by the amount it looked things up. The
# knowledge writes are counted the same way, and the record allowance is the one
# that must be there in full: a `forget_knowledge` whose replacement write cannot
# be afforded is how this design loses knowledge.
BASE_TOOL_CALLS = (
    10 + MAX_SEARCHES_PER_RUN + MAX_RECORDS_PER_RUN + MAX_FORGETS_PER_RUN
)
TOOL_CALLS_PER_STEP = 15
RUN_DEADLINE_SECONDS = 600.0

# Content-block types that carry the model's own reasoning, and the keys those
# blocks put it under. Providers disagree on both.
_REASONING_BLOCK_TYPES = ("text", "thinking", "reasoning")
_REASONING_KEYS = ("text", "thinking", "reasoning", "reasoning_content")


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


@wrap_model_call
async def _fold_scene_views(request, handler):
    """Fold stale scene views out of what one model call actually receives.

    `request.override` replaces only this call's messages, not the graph's own
    state, so the timeline and the console logging below keep the full text —
    see `app/agents/qa/context.py` for the fold itself.
    """
    return await handler(request.override(messages=fold_stale_scenes(request.messages)))


class QaRunner:
    """Runs one scenario to completion over one channel."""

    def __init__(
        self,
        model: LLMModel = DEFAULT_MODEL,
        language: OutputLanguage = DEFAULT_LANGUAGE,
        deadline_seconds: float = RUN_DEADLINE_SECONDS,
        prompt_version: str | None = None,
    ) -> None:
        self._model = model
        self._language = language
        self._deadline = deadline_seconds
        # None leaves the choice to settings, and then to the newest version.
        self._prompt_version = prompt_version

    def _tool_call_limit(self, steps: int) -> int:
        return BASE_TOOL_CALLS + TOOL_CALLS_PER_STEP * max(steps, 1)

    async def run(
        self, channel: QaRunChannel, scenario: ScenarioDraft, state: QaRunState
    ) -> None:
        supports_vision = get_model_spec(self._model).supports_vision
        prompt = load_prompt(PROMPT_AGENT, "system", self._prompt_version)
        # A separate file in the same version: it is prompt text, tuned by whoever
        # tunes the rest of it, and only reaches models that can read images.
        # Telling a text-only model about a tool it does not have would send it
        # looking for one.
        vision_directive = (
            load_prompt(PROMPT_AGENT, "vision_directive", self._prompt_version).body
            if supports_vision
            else ""
        )
        system_prompt = prompt.body.format(
            language_directive=LANGUAGE_DIRECTIVES[self._language],
            vision_directive=vision_directive,
        )
        first_message = _first_message(scenario)
        tools = build_tools(channel, state, supports_vision)

        # The whole starting context in one place. Reading a run afterwards means
        # knowing what the model was actually given, and the prompt is assembled
        # from several pieces that are otherwise only visible in source. The
        # resolved prompt version is here because the text alone does not say
        # which candidate produced this run.
        logger.info(
            "[QA] run starting\n"
            "  model=%s language=%s prompt_version=%s steps=%d deadline=%.0fs tools=%s\n"
            "--- system prompt ---\n%s\n"
            "--- first message ---\n%s",
            self._model,
            self._language,
            prompt.version,
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
            # Folding runs on every request; the scene views pile up whether or
            # not the model can see. The vision middleware is added only when it
            # can — on a text-only model it would have nothing to inject and
            # nothing to trim. The two touch disjoint messages, so their order
            # here does not matter.
            middleware=(
                [_fold_scene_views, QaCaptureVisionMiddleware(state)]
                if supports_vision
                else [_fold_scene_views]
            ),
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
            {
                "recursion_limit": self._tool_call_limit(len(scenario.steps)) * 2,
                "run_name": "qa-scenario-run",
                "tags": ["agent", "qa"],
                "metadata": {
                    "qa_try_id": channel.qa_try_id,
                    "model": self._model.value,
                    "language": self._language.value,
                },
            },
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
