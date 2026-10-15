"""The planner: writes each step as one macro source with the run's model.

The answer is checked before anything runs (`macros.parse_step`): the macro parser reads each
action and `done`, and the source must have what the executer needs. A refused answer goes back to
the model with the parser's sentences, up to `max_rewrites` times; an answer still refused after
that is returned with the refusal, and the runner fails the step without playing it.

The call goes through `build_chat_model` like the tool loop's, so it takes the route the model id
names — `claude/` to the Claude API, `gpt/` to the OpenAI API, `bedrock/` to Bedrock, anything else
to OpenRouter — with the user's key when the session bound one and the platform key otherwise,
and its usage reaches Orchestration under the run's scope.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.macros import RegisteredMacros, StepMacros, parse_step
from app.agents.planner_executer.prompts import RULES_ROLE, game_map, prompt_text, screen, this_step
from app.agents.qa.macro.errors import MacroRejection
from app.llm.chat_model import build_chat_model
from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)

# Sends the system and user text and returns the model's answer. Tests replace it.
Ask = Callable[[str, str], Awaitable[str]]


def extract_code(text: str) -> str:
    """The first fenced block, or the whole answer when there is none."""
    match = _FENCE.search(text)
    return (match.group(1) if match else text).strip() + "\n"


@dataclass(frozen=True)
class Written:
    """The planner's last answer for a step: parsed, or the refusal it still gets."""

    source: str
    macros: StepMacros | None
    refusal: str | None = None


class Planner:
    def __init__(
        self,
        config: RunConfig,
        channel: QaRunChannel,
        spec: PlannerExecuterArchSpec,
        registered: RegisteredMacros,
        ask: Ask | None = None,
    ) -> None:
        self._config = config
        self._channel = channel
        self._spec = spec
        self._registered = registered
        self._system = prompt_text(RULES_ROLE, config.prompt_version)
        self._ask = ask or self._ask_model
        self._model = None

    async def write(self, scenario: QaScenario, step: int) -> Written:
        """Step `step`'s macros; rewritten while the parser refuses them, up to `max_rewrites`."""
        feedback = ""
        written = Written("", None, "nothing was written")
        for attempt in range(self._spec.max_rewrites + 1):
            await self._channel.note(
                f"Step {step}: the planner is writing macros"
                + (" again (the last answer was refused)" if attempt else "")
                + f". Prompt {self._config.prompt_version}.",
                LogCategory.THOUGHT,
                step,
            )
            source = extract_code(await self._ask(self._system, self._request(scenario, step, feedback)))
            try:
                written = Written(source, await parse_step(source, self._registered))
            except MacroRejection as rejection:
                written = Written(source, None, rejection.render())
                feedback = (
                    "Your previous answer cannot be used as written. The macro parser refused it:\n"
                    f"  {written.refusal}\n"
                    "The refusal names what is accepted instead. Rewrite the whole source."
                )
                continue
            break
        return written

    def _request(self, scenario: QaScenario, step: int, feedback: str) -> str:
        drawn = game_map(self._channel.scene.scene_context)
        return (
            (f"Game map:\n{drawn}\n\n" if drawn else "")
            + f"Scenario: {scenario.title}\n\n"
            + f"Write step {step} now:\n{this_step(scenario, step)}\n\n"
            + f"Current screen:\n{screen(self._channel.scene.pulse)}\n"
            + (f"\n{feedback}\n" if feedback else "")
        )

    async def _ask_model(self, system: str, user: str) -> str:
        if self._model is None:
            self._model = build_chat_model(self._config.model, self._config.reasoning)
        answer = await self._model.ainvoke([SystemMessage(system), HumanMessage(user)])
        return _text(answer.content)


def _text(content) -> str:
    """The model's text, whether the provider answered with a string or with content blocks."""
    if isinstance(content, list):
        return "".join(
            block if isinstance(block, str) else block.get("text", "")
            for block in content
            if isinstance(block, str) or block.get("type") in (None, "text")
        )
    return str(content)
