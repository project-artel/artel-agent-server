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
from dataclasses import dataclass, replace

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.decisions import (
    MAX_QUOTE_CHARS,
    MAX_REFUSAL_CHARS,
    MAX_SOURCE_CHARS,
    clip,
    note_decision,
)
from app.agents.planner_executer.developer_macros import (
    NO_DEVELOPER_MACROS,
    LoadedDeveloperMacros,
    bind_developer_calls,
    split_developer_block,
)
from app.agents.planner_executer.macros import (
    RegisteredMacros,
    SceneMacros,
    StepMacros,
    StepObserve,
    parse_scene,
    parse_step,
)
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
_OBSERVE_BLOCK = re.compile(r"```observe\s*\n(.*?)```", re.DOTALL)
# observe 블록 하나에 받는 이름 수. 범위를 좁히는 것이 블록의 쓸모라 넉넉하게만 막는다.
MAX_OBSERVE_NAMES = 60

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


def split_observe_block(answer: str) -> tuple[str, str | None]:
    """The answer without its ```observe block, and the block's text (ARTEL-1295 C). Taken out first for
    the reason `split_plan_block` gives."""
    match = _OBSERVE_BLOCK.search(answer)
    if match is None:
        return answer, None
    return answer[: match.start()] + answer[match.end() :], match.group(1)


def read_observe(text: str | None) -> StepObserve | None:
    """The ```observe block as a `StepObserve`, `None` when there is none, or `MacroRejection`.

    `{"paths": ["<object path as the screen spells it>", ...], "members": ["Type.member", ...]}`. Either key
    may be left out; a left-out key does not narrow.
    """
    if text is None or not text.strip():
        return None
    try:
        block = json.loads(text)
    except json.JSONDecodeError as error:
        raise MacroRejection(f"the ```observe block is not JSON — {error.msg}.") from None
    if not isinstance(block, dict) or set(block) - {"paths", "members"}:
        raise MacroRejection('the ```observe block must be an object with only "paths" and "members" lists.')
    picked: dict[str, tuple[str, ...] | None] = {}
    for key in ("paths", "members"):
        names = block.get(key)
        if names is None:
            picked[key] = None
            continue
        if not isinstance(names, list) or not all(isinstance(name, str) and name.strip() for name in names):
            raise MacroRejection(f'"{key}" in the ```observe block must be a list of non-empty strings.')
        if len(names) > MAX_OBSERVE_NAMES:
            raise MacroRejection(f'"{key}" in the ```observe block lists {len(names)} names; keep it to {MAX_OBSERVE_NAMES}.')
        picked[key] = tuple(name.strip() for name in names)
    return StepObserve(paths=picked["paths"], members=picked["members"])


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
        return await self._write(scenario, step, this_step(scenario, step), f"Step {step}", "step")

    async def replan(self, scenario: QaScenario, step: int, reason: str, turns: list[str]) -> Written:
        """Step `step` written again because the decision model chose `REPLAN` (ARTEL-1295 C).

        `reason` is why play stopped, `turns` the last action turns as `recent_action_lines` writes them. The
        planner sees both above the current screen; the rest of the request is `write`'s.
        """
        lines = "\n".join(f"  {line}" for line in turns) or "  (no action ran)"
        feedback = (
            "Your previous macros for this step were played and the decision model asked for a new plan:\n"
            f"  {reason}\n"
            f"The last turns:\n{lines}\n"
            "Write the step again for the screen as it is now."
        )
        return await self._write(
            scenario, step, this_step(scenario, step), f"Step {step} (replan)", "replan", feedback
        )

    async def write_travel(self, scenario: QaScenario, step: int, scene: str) -> Written:
        """A FREE scenario's move to `scene`, written as an ordinary step (`done()` is that scene).
        `step` is the case the move is for; it labels the notes."""
        return await self._write(
            scenario, step, travel_step(step, scene), f"Travel to `{scene}` (for step {step})", "travel"
        )

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
            scene = self._channel.scene.pulse.scene or "?"
            try:
                verify, later = read_plan(plan_text, candidates)
                developer_actions = await bind_developer_calls(calls, self._developer)
                macros = await parse_scene(source, self._registered, verify, developer_actions)
            except MacroRejection as rejection:
                plan = ScenePlan([], {}, None, rejection.render())
                await note_decision(
                    self._channel,
                    "scene_plan_refused",
                    f"Scene {scene}: the plan was refused (answer {attempt + 1} of {self._spec.max_rewrites + 1}): "
                    f"{clip(plan.refusal, MAX_QUOTE_CHARS)}",
                    steps[0],
                    scene=scene,
                    answer=attempt + 1,
                    refusal=clip(plan.refusal, MAX_REFUSAL_CHARS),
                    plan=clip(plan_text, MAX_REFUSAL_CHARS),
                    source=clip(source, MAX_SOURCE_CHARS),
                )
                feedback = (
                    "Your previous answer cannot be used as written. It was refused:\n"
                    f"  {plan.refusal}\n"
                    "The refusal names what is accepted instead. Rewrite the whole answer."
                )
                continue
            plan = ScenePlan([by_case[case_id] for case_id in verify], {by_case[c]: r for c, r in later.items()}, macros)
            await note_decision(
                self._channel,
                "scene_plan",
                f"Scene {scene}: verify {len(plan.verify)} of {len(steps)} cases now (steps {plan.verify}), "
                f"{len(plan.later)} later, {len(macros.actions)} actions",
                steps[0],
                scene=scene,
                offered=steps,
                verify=plan.verify,
                later={str(step): reason for step, reason in plan.later.items()},
                actions=list(macros.actions),
                source=clip(source, MAX_SOURCE_CHARS),
            )
            return plan
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

    async def _write(
        self, scenario: QaScenario, step: int, shown: str, label: str, kind: str, feedback: str = ""
    ) -> Written:
        """`label` heads the notes ("Step 3", "Travel to `X` (for step 3)"); `kind` names the decision
        in the log (`step`, `travel`, `replan`). `feedback` opens the first request (a replan's reason)."""
        written = Written("", None, "nothing was written")
        for attempt in range(self._spec.max_rewrites + 1):
            await self._channel.note(
                f"{label}: the planner is writing macros"
                + (" again (the last answer was refused)" if attempt else "")
                + f". Prompt {self._config.prompt_version}.",
                LogCategory.THOUGHT,
                step,
            )
            answer = await self._ask(self._system, self._request(scenario, step, shown, feedback))
            rest, observe_text = split_observe_block(answer)
            rest, calls = split_developer_block(rest)
            source = extract_code(rest)
            try:
                observe = read_observe(observe_text)
                developer_actions = await bind_developer_calls(calls, self._developer)
                macros = await parse_step(source, self._registered, developer_actions)
                written = Written(source, replace(macros, observe=observe))
            except MacroRejection as rejection:
                written = Written(source, None, rejection.render())
                await note_decision(
                    self._channel,
                    f"{kind}_macros_refused",
                    f"{label}: the macros were refused (answer {attempt + 1} of {self._spec.max_rewrites + 1}): "
                    f"{clip(written.refusal, MAX_QUOTE_CHARS)}",
                    step,
                    answer=attempt + 1,
                    refusal=clip(written.refusal, MAX_REFUSAL_CHARS),
                    source=clip(source, MAX_SOURCE_CHARS),
                )
                feedback = (
                    "Your previous answer cannot be used as written. The macro parser refused it:\n"
                    f"  {written.refusal}\n"
                    "The refusal names what is accepted instead. Rewrite the whole source."
                )
                continue
            actions = list(written.macros.actions)
            observe = written.macros.observe
            await note_decision(
                self._channel,
                f"{kind}_macros",
                f"{label}: {len(actions)} actions — {', '.join(actions)}",
                step,
                actions=actions,
                source=clip(source, MAX_SOURCE_CHARS),
                observe=None if observe is None else {"paths": observe.paths, "members": observe.members},
            )
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
