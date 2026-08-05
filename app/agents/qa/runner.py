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
from langchain_core.messages import HumanMessage

from app.agents.qa.compaction import QaCompactionMiddleware
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
from app.config import Settings, get_settings
from app.llm.chat_model import build_chat_model
from app.llm.models import DEFAULT_MODEL, LLMModel, ReasoningConfig, get_model_spec
from app.prompts import load_prompt
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import LogCategory

logger = logging.getLogger(__name__)

# Directories under app/prompts/ holding these agents' prompt versions. The
# summarizing prompt is versioned apart from the run's own: it is a different call
# to a different model, so pinning one back must not pin the other.
PROMPT_AGENT = "qa_run"
COMPACTION_PROMPT_AGENT = "qa_compaction"

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

# The graph nodes whose updates carry turns that actually happened. Everything
# else in an `astream` update is a middleware node reporting its own rewrite of
# the conversation, which is not new and must not be logged as if it were.
_TURN_PRODUCING_NODES = frozenset({"model", "tools"})


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


@wrap_model_call
async def _log_token_usage(request, handler):
    """Log what each model call cost, and how much of it came from cache.

    Prompt caching fails silently: a prefix under the model's minimum size, or one
    that changed since the last call, is billed in full with no error and no
    warning. `cache_read` in the details is the only signal that the
    `cache_control` set in `app/llm/chat_model.py` is doing anything, so a run's
    log has to carry it or the setting is unverifiable in production.

    The details dict is logged whole rather than picked apart: which keys
    OpenRouter fills in varies by provider, and an unread key is worth more here
    than a `0` for one this provider never sends.
    """
    response = await handler(request)
    for message in response.result:
        usage = getattr(message, "usage_metadata", None)
        if not usage:
            continue
        logger.info(
            "[QA] tokens input=%d output=%d details=%s",
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("input_token_details") or {},
        )
    return response


class QaRunner:
    """Runs one scenario to completion over one channel."""

    def __init__(
        self,
        model: LLMModel = DEFAULT_MODEL,
        language: OutputLanguage = DEFAULT_LANGUAGE,
        deadline_seconds: float = RUN_DEADLINE_SECONDS,
        prompt_version: str | None = None,
        reasoning: ReasoningConfig | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._model = model
        self._language = language
        self._deadline = deadline_seconds
        # None leaves the choice to settings, and then to the newest version.
        self._prompt_version = prompt_version
        self._reasoning = reasoning
        # Read here rather than taken as arguments so that `QaExecutionService`'s
        # runner factory, which passes only the run's own options, needs no change
        # when a compaction setting is added. Injectable for tests.
        self._settings = settings or get_settings()

    def _tool_call_limit(self, steps: int) -> int:
        return BASE_TOOL_CALLS + TOOL_CALLS_PER_STEP * max(steps, 1)

    def _build_compaction(
        self, channel: QaRunChannel, state: QaRunState
    ) -> QaCompactionMiddleware | None:
        """The compaction middleware, or None when it is switched off."""
        settings = self._settings
        if not settings.qa_compaction_enabled:
            return None
        prompt = load_prompt(COMPACTION_PROMPT_AGENT, "summary")
        logger.info(
            "[QA] compaction on: model=%s prompt_version=%s trigger=%.2f keep=%d",
            settings.qa_compaction_model,
            prompt.version,
            settings.qa_compaction_trigger_fraction,
            settings.qa_compaction_keep_messages,
        )
        return QaCompactionMiddleware(
            # Not the run's model, and not `cache_prompt`: summarizing is one
            # call over a prefix nothing will send again, so caching it would pay
            # the write premium for a read that never happens.
            model=build_chat_model(LLMModel(settings.qa_compaction_model)),
            summary_prompt=prompt.body,
            run_model_max_input_tokens=get_model_spec(self._model).max_input_tokens,
            state=state,
            channel=channel,
            trigger_fraction=settings.qa_compaction_trigger_fraction,
            keep_messages=settings.qa_compaction_keep_messages,
            min_new_messages=settings.qa_compaction_min_new_messages,
            trim_tokens=settings.qa_compaction_trim_tokens,
            on_compacted=self._log_compaction,
        )

    async def _log_compaction(self, summary: str, count: int) -> None:
        """The summary on the console, whole.

        The timeline says that a compaction happened; only the log can say what
        the model was left with, and that is the first thing anyone reads when a
        run goes strange right after one.
        """
        logger.info("[QA] context compacted #%d\n%s", count, _clip(summary))

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

        @wrap_model_call
        async def _append_current_scene(request, handler):
            """Put the scene as it stands right now at the end of every model call.

            The tool loop only ever showed the agent a scene it had asked for, so
            a frame the game volunteered sat in `SceneMemory` unread until the
            next tool call happened to render it. This closes that: the current
            scene is in front of the model on every turn, whether or not it
            looked, and it is written fresh each time rather than accumulated —
            `request.override` touches this one call, not the graph's state, so
            nothing here survives into the next turn to be resent.
            """
            view = channel.scene.render_now()
            if view is None:  # No frame has arrived yet; there is nothing to say.
                return await handler(request)
            return await handler(
                request.override(messages=[*request.messages, HumanMessage(content=view)])
            )

        # The whole starting context in one place. Reading a run afterwards means
        # knowing what the model was actually given, and the prompt is assembled
        # from several pieces that are otherwise only visible in source. The
        # resolved prompt version is here because the text alone does not say
        # which candidate produced this run.
        logger.info(
            "[QA] run starting\n"
            "  model=%s reasoning=%s language=%s prompt_version=%s steps=%d deadline=%.0fs tools=%s\n"
            "--- system prompt ---\n%s\n"
            "--- first message ---\n%s",
            self._model,
            (
                self._reasoning.model_dump(mode="json", exclude_none=True)
                if self._reasoning
                else None
            ),
            self._language,
            prompt.version,
            len(scenario.steps),
            self._deadline,
            [tool.name for tool in tools],
            system_prompt,
            _clip(first_message),
        )

        # Folding runs on every request; the scene views pile up whether or not
        # the model can see. The vision middleware is added only when it can — on
        # a text-only model it would have nothing to inject and nothing to trim.
        # The two touch disjoint messages, so their order here does not matter.
        #
        # The live scene is appended after them: the fold has run over the tool
        # messages and the image trim has happened, which leaves it as the actual
        # final message the model reads. Usage logging sits innermost, after that,
        # so it reports what was actually sent.
        middleware: list = [_fold_scene_views]
        if supports_vision:
            middleware.append(QaCaptureVisionMiddleware(state))
        middleware += [_append_current_scene, _log_token_usage]

        # Compaction is listed first because it is the first thing that happens,
        # but the position is documentation rather than wiring: it hooks
        # `before_model`, which is its own graph node ahead of the model call,
        # while everything above wraps the call itself. List order only sequences
        # middleware sharing a hook.
        compaction = self._build_compaction(channel, state)
        if compaction is not None:
            middleware.insert(0, compaction)

        agent = create_agent(
            # Prompt caching is asked for here and nowhere else: the agent loop
            # resends the whole conversation every turn, which is the one shape
            # that reads back more than it writes. See `build_chat_model`.
            model=build_chat_model(self._model, self._reasoning, cache_prompt=True),
            tools=tools,
            system_prompt=system_prompt,
            middleware=middleware,
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
                # Two graph steps per iteration — the model call and the tool
                # call — plus one for each middleware that runs as its own node
                # ahead of the model. Left at a flat 2, turning compaction on
                # would quietly cost the run a third of its tool budget.
                "recursion_limit": (
                    self._tool_call_limit(len(scenario.steps))
                    * (2 + (1 if compaction is not None else 0))
                ),
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
        for name, node in update.items():
            # Only the two nodes that produce new turns. A middleware node —
            # compaction is one — reports its whole rewritten message list as its
            # update, so every preserved AIMessage in it would be logged and put
            # on the timeline a second time. The operator would see the agent's
            # reasoning repeat itself after each compaction, with no clue why.
            if name not in _TURN_PRODUCING_NODES:
                continue
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
