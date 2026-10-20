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

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.developer_macros import (
    NO_DEVELOPER_MACROS,
    LoadedDeveloperMacros,
    bind_developer_calls,
    split_developer_block,
)
from app.agents.planner_executer.macros import RegisteredMacros, SceneMacros, StepMacros, parse_scene, parse_step
from app.agents.planner_executer.prompts import (
    RULES_ROLE,
    SCENE_RULES_ROLE,
    candidate_list,
    developer_macro_list,
    game_map,
    prompt_text,
    screen,
    this_step,
    travel_step,
)
from app.agents.qa.macro.errors import MacroRejection
from app.llm.chat_model import build_chat_model
from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
_PLAN_BLOCK = re.compile(r"```plan\s*\n(.*?)```", re.DOTALL)

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


@dataclass(frozen=True)
class ScenePlan:
    """The planner's plan for one scene of a FREE scenario (ARTEL-1149).

    `verify` is the case ids to play now, in order; `later` the candidates left for another
    scene or round, with why. `macros` is `None` while the answer is still refused.
    """

    verify: list[int]
    later: dict[int, str]
    macros: SceneMacros | None
    refusal: str | None = None


def split_plan_block(answer: str) -> tuple[str, str | None]:
    """The answer without its ```plan block, and the block's text. Taken out first: left in, its
    closing fence would be read as the python block's opening one."""
    match = _PLAN_BLOCK.search(answer)
    if match is None:
        return answer, None
    return answer[: match.start()] + answer[match.end() :], match.group(1)


def read_plan(text: str | None, candidates: list[int]) -> tuple[list[int], dict[int, str]]:
    """`(verify, later)` from the ```plan block, or `MacroRejection` naming what is wrong.

    A candidate the plan leaves out is put `later` with that said; an id that is not a candidate,
    or one listed twice, refuses the plan.
    """
    if text is None:
        raise MacroRejection(
            'the answer needs a ```plan block first: {"verify": [<case id>, ...], "later": {"<case id>": "<why>"}}.'
        )
    try:
        plan = json.loads(text)
    except json.JSONDecodeError as error:
        raise MacroRejection(f"the ```plan block is not JSON — {error.msg}.") from None
    if not isinstance(plan, dict) or not isinstance(plan.get("verify", []), list):
        raise MacroRejection('the ```plan block must be an object with a "verify" list and a "later" object.')
    known = set(candidates)
    try:
        verify = [int(case_id) for case_id in plan.get("verify", [])]
        later = {int(case_id): str(reason) for case_id, reason in (plan.get("later") or {}).items()}
    except (TypeError, ValueError, AttributeError):
        raise MacroRejection('case ids in the ```plan block must be the numbers the request lists.') from None
    strangers = sorted((set(verify) | set(later)) - known)
    if strangers:
        raise MacroRejection(
            f"the plan names {', '.join(map(str, strangers))}, which are not candidates. Use only the ids "
            f"the request lists: {', '.join(map(str, candidates))}."
        )
    twice = sorted({case_id for case_id in verify if verify.count(case_id) > 1} | (set(verify) & set(later)))
    if twice:
        raise MacroRejection(f"case {', '.join(map(str, twice))} is listed twice; put each case in one place once.")
    for case_id in candidates:
        if case_id not in verify and case_id not in later:
            later[case_id] = "the plan did not include it"
    return verify, later


class Planner:
    def __init__(
        self,
        config: RunConfig,
        channel: QaRunChannel,
        spec: PlannerExecuterArchSpec,
        registered: RegisteredMacros,
        ask: Ask | None = None,
        developer: LoadedDeveloperMacros = NO_DEVELOPER_MACROS,
    ) -> None:
        self._config = config
        self._channel = channel
        self._spec = spec
        self._registered = registered
        self._developer = developer
        # 런 동안 바뀌지 않으므로 한 번 그린다.
        self._developer_macros = developer_macro_list(developer.usable.values())
        self._system = prompt_text(RULES_ROLE, config.prompt_version)
        # FREE 시나리오에서만 읽는다. v3 전 버전에는 없는 파일이라 처음 쓸 때 읽는다.
        self._scene_system: str | None = None
        self._ask = ask or self._ask_model
        self._model = None

    async def write(self, scenario: QaScenario, step: int) -> Written:
        """Step `step`'s macros; rewritten while the parser refuses them, up to `max_rewrites`."""
        return await self._write(scenario, step, this_step(scenario, step))

    async def write_travel(self, scenario: QaScenario, step: int, scene: str) -> Written:
        """A FREE scenario's move to `scene`, written as an ordinary step (`done()` is that scene).
        `step` only labels the notes."""
        return await self._write(scenario, step, travel_step(step, scene))

    async def plan_scene(self, scenario: QaScenario, steps: list[int]) -> ScenePlan:
        """The plan for the scene on screen: which of the candidate cases (`steps`) to verify now and
        in what order, and their macros. Rewritten while it is refused, up to `max_rewrites`."""
        if self._scene_system is None:
            self._scene_system = prompt_text(SCENE_RULES_ROLE, self._config.prompt_version)
        by_case = {scenario.steps[step - 1].case_id: step for step in steps}
        candidates = list(by_case)
        feedback = ""
        plan = ScenePlan([], {}, None, "nothing was written")
        for attempt in range(self._spec.max_rewrites + 1):
            await self._channel.note(
                f"Scene {self._channel.scene.pulse.scene or '?'}: the planner is planning {len(steps)} cases"
                + (" again (the last answer was refused)" if attempt else "")
                + f". Prompt {self._config.prompt_version}.",
                LogCategory.THOUGHT,
                steps[0],
            )
            answer = await self._ask(self._scene_system, self._scene_request(scenario, steps, feedback))
            rest, plan_text = split_plan_block(answer)
            rest, calls = split_developer_block(rest)
            source = extract_code(rest) if _FENCE.search(rest) else ""
            try:
                verify, later = read_plan(plan_text, candidates)
                developer_actions = await bind_developer_calls(calls, self._developer)
                macros = await parse_scene(source, self._registered, verify, developer_actions)
            except MacroRejection as rejection:
                plan = ScenePlan([], {}, None, rejection.render())
                feedback = (
                    "Your previous answer cannot be used as written. It was refused:\n"
                    f"  {plan.refusal}\n"
                    "The refusal names what is accepted instead. Rewrite the whole answer."
                )
                continue
            return ScenePlan([by_case[case_id] for case_id in verify], {by_case[c]: r for c, r in later.items()}, macros)
        return plan

    def _scene_request(self, scenario: QaScenario, steps: list[int], feedback: str) -> str:
        drawn = game_map(self._channel.scene.scene_context)
        return (
            (f"Game map:\n{drawn}\n\n" if drawn else "")
            + (f"{self._developer_macros}\n\n" if self._developer_macros else "")
            + f"Scenario: {scenario.title}\n\n"
            + f"Scene on screen: {self._channel.scene.pulse.scene or '(unknown)'}\n\n"
            + f"Candidate cases:\n{candidate_list(scenario, steps)}\n\n"
            + f"Current screen:\n{screen(self._channel.scene.pulse)}\n"
            + (f"\n{feedback}\n" if feedback else "")
        )

    async def _write(self, scenario: QaScenario, step: int, shown: str) -> Written:
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
            answer = await self._ask(self._system, self._request(scenario, step, shown, feedback))
            rest, calls = split_developer_block(answer)
            source = extract_code(rest)
            try:
                developer_actions = await bind_developer_calls(calls, self._developer)
                written = Written(source, await parse_step(source, self._registered, developer_actions))
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

    def _request(self, scenario: QaScenario, step: int, shown: str, feedback: str) -> str:
        drawn = game_map(self._channel.scene.scene_context)
        return (
            (f"Game map:\n{drawn}\n\n" if drawn else "")
            + (f"{self._developer_macros}\n\n" if self._developer_macros else "")
            + f"Scenario: {scenario.title}\n\n"
            + f"Write step {step} now:\n{shown}\n\n"
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
