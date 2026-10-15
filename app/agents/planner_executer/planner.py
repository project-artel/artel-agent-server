"""The planner: writes each step's module with the run's model.

One call per step. Every answer is checked before anything runs: the module must load in the
sandbox — which also refuses a `check` that uses another function of the module — and every func's
`run` must pass the macro parser (`runs.parse_run`, the same checks the tool loop's macros pass
when they are written). A refused answer goes back to the model with the reasons, up to
`max_rewrites` times; an answer still refused after that is returned with its problems, and the
runner fails the step on them.

Calls go through `build_chat_model`, so their usage reaches Orchestration under the run's scope
like the agent's, and a run on a user's key uses that key.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.planner_executer import sandbox
from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.executer import load_step_module, parse_runs
from app.agents.planner_executer.g import GameHandle
from app.agents.planner_executer.prompt_maker import PromptMaker
from app.agents.planner_executer.runs import RegisteredMacros
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
    """The planner's last answer for a step, and why it is still refused (empty when it is not)."""

    source: str
    problems: list[str]


class Planner:
    def __init__(
        self,
        config: RunConfig,
        channel: QaRunChannel,
        spec: PlannerExecuterArchSpec,
        ask: Ask | None = None,
        registered: RegisteredMacros | None = None,
    ) -> None:
        self._config = config
        self._channel = channel
        self._spec = spec
        self._maker = PromptMaker(config.prompt_version)
        self._ask = ask or self._ask_model
        self._model = None
        # `from macros import` 를 풀 이 build 의 등록된 macro. executer 와 같은 것을 쓴다.
        self._registered = registered or RegisteredMacros(channel)

    async def write(self, scenario: QaScenario, step: int, g: GameHandle) -> Written:
        """Step `step`'s module, checked; rewritten while it is refused, up to `max_rewrites`."""
        system = self._maker.system()
        state = g.state_view()
        feedback = ""
        written = Written("", [])
        for attempt in range(self._spec.max_rewrites + 1):
            user = self._maker.write_request(
                scenario=scenario,
                step=step,
                state=state,
                context=self._channel.scene.scene_context,
                feedback=feedback,
            )
            await self._channel.note(
                f"Step {step}: the planner is writing funcs"
                + (" again (the last answer was refused)" if attempt else "")
                + f". Prompt {self._maker.version}.",
                LogCategory.THOUGHT,
                step,
            )
            source = extract_code(await self._ask(system, user))
            written = Written(source, await self.problems(source))
            if not written.problems:
                break
            feedback = (
                "Your previous answer cannot be used as written. It is refused before it runs:\n"
                + "\n".join(f"  - {problem}" for problem in written.problems)
                + "\nEach refusal names what is accepted instead. Rewrite the whole module."
            )
        return written

    async def problems(self, source: str) -> list[str]:
        """module 을 load 하지 못하는 이유와, func 마다 `run` macro 를 parser 가 거절한 이유."""
        try:
            module = load_step_module(source, 0)
        except sandbox.ModuleRejected as error:
            return [f"the module is refused: {error}"]
        return [
            f"{problem} (refused by the macro parser)"
            for problem in await parse_runs(module.funcs, self._registered)
        ]

    async def _ask_model(self, system: str, user: str) -> str:
        if self._model is None:
            self._model = build_chat_model(self._config.model, self._config.reasoning)
        answer = await self._model.ainvoke([SystemMessage(system), HumanMessage(user)])
        content = answer.content
        if isinstance(content, list):
            content = "".join(
                block if isinstance(block, str) else block.get("text", "")
                for block in content
                if isinstance(block, str) or block.get("type") in (None, "text")
            )
        return str(content)
