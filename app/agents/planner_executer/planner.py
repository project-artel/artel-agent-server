"""The planner: writes each step as one macro source with the run's model.

The answer is checked before anything runs (`macros.parse_step`): the macro parser reads each
action and `done`, and the source must have what the executer needs. A refused answer goes back to
the model with the parser's sentences, up to `max_rewrites` times; an answer still refused after
that is returned with the refusal, and the runner fails the step without playing it.

The call goes through `build_chat_model` like the tool loop's, so it takes the route the model id
names — `claude/` to the Claude API, `gpt/` to the OpenAI API, anything else to OpenRouter — with the user's key when the session bound one and the platform key otherwise,
and its usage reaches Orchestration under the run's scope.
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.decisions import (
    MAX_QUOTE_CHARS,
    MAX_REFUSAL_CHARS,
    MAX_SOURCE_CHARS,
    clip,
    note_decision,
)
from app.agents.planner_executer.inputs import InputsReader
from app.agents.planner_executer.developer_macros import (
    NO_DEVELOPER_MACROS,
    LoadedDeveloperMacros,
    bind_developer_calls,
    bind_guidance_calls,
    split_developer_block,
    DEVELOPER_ACTION_PREFIX,
)
from app.agents.planner_executer.macros import (
    RegisteredMacros,
    SceneMacros,
    StepCheck,
    StepMacros,
    StepObserve,
    parse_guidance_step,
    parse_scene,
    parse_step,
)
from app.agents.planner_executer.facts import NO_FACTS, GameFacts, check_guidance_facts, planner_text, read_facts_block
from app.agents.planner_executer.target import StepContracts, planner_note
from app.agents.planner_executer.prompts import (
    CONTRACT_RULES_ROLE,
    GUIDANCE_RULES_ROLE,
    RULES_ROLE,
    SCENE_RULES_ROLE,
    candidate_list,
    developer_macro_list,
    game_map,
    registered_action_list,
    prompt_text,
    screen,
    step_text,
    this_step,
    travel_step,
)
from app.agents.planner_executer.ledger import ActionLedger
from app.agents.planner_executer.contract import split_actions_block
from app.agents.planner_executer.plan_edit import PlanDraft, merge, read_changes, split_changes_block
from app.agents.planner_executer.plan_edit import shown as shown_plan
from app.agents.planner_executer.screen_capture import ScreenImage, capture_screen
from app.agents.qa.macro.errors import MacroRejection
from app.llm.chat_model import build_chat_model, prompt_messages
from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
_PLAN_BLOCK = re.compile(r"```plan\s*\n(.*?)```", re.DOTALL)
_OBSERVE_BLOCK = re.compile(r"```observe\s*\n(.*?)```", re.DOTALL)
_CHECK_BLOCK = re.compile(r"```check\s*\n(.*?)```", re.DOTALL)
_SUMMARY_BLOCK = re.compile(r"```summary\s*\n(.*?)```", re.DOTALL)
_GUIDANCE_SUMMARY_BLOCK = re.compile(r"```guidance_summary\s*\n(.*?)```", re.DOTALL)
_AFTER_BLOCK = re.compile(r"```after\s*\n(.*?)```", re.DOTALL)
_GUIDANCE_BLOCK = re.compile(r"```guidance\s*\n(.*?)```", re.DOTALL)
_FACTS_BLOCK = re.compile(r"```facts\s*\n(.*?)```", re.DOTALL)
# 사람이 읽는 계획 요약의 언어. prompt 의 `summary_language` 자리에 들어간다.
SUMMARY_LANGUAGES = {"ko": "Korean", "en": "English"}
# observe 블록 하나에 받는 이름 수. 범위를 좁히는 것이 블록의 쓸모라 넉넉하게만 막는다.
MAX_OBSERVE_NAMES = 60

# Sends the system and user text and returns the model's answer. Tests replace it.
Ask = Callable[[str, str], Awaitable[str]]


# 화면(`PulseMemory.render`)의 "switched off" 목록은 DontDestroyOnLoad 객체를 `씬/selector` 꼴로 적는다. 모델이 그 꼴을 베끼면
# SDK selector 와 `STATE_SET` 이 아는 `@persistent/…` 와 맞지 않아 아무것도 찾지 못한다(런 166 step 4: check 값·`done()`·editor
# 가 쓴 action 이 전부 헛짚었다). 답을 읽을 때 그 꼴만 바로잡는다.
_SCENE_PREFIXED_PERSISTENT = "DontDestroyOnLoad/@persistent/"


def normalize_paths(answer: str) -> str:
    """`DontDestroyOnLoad/@persistent/…` 를 `@persistent/…` 로."""
    return answer.replace(_SCENE_PREFIXED_PERSISTENT, "@persistent/")


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
    return _split_block(answer, _OBSERVE_BLOCK)


def split_summary_block(answer: str) -> tuple[str, str | None]:
    """The answer without its ```summary block, and the block's text: the plan in plain words for a person
    reading the run. Only the log carries it; the executer does not read it."""
    return _split_block(answer, _SUMMARY_BLOCK)


def split_guidance_summary_block(answer: str) -> tuple[str, str | None]:
    """The answer without its ```guidance_summary block, and the block's text: the ```guidance retold for a person in
    the run's language (prompt v4). The decision model reads the English ```guidance; only the log carries this."""
    return _split_block(answer, _GUIDANCE_SUMMARY_BLOCK)


def split_guidance_block(answer: str) -> tuple[str, str | None]:
    """The answer without its ```guidance block, and the block's text: the step's sub-goal, limits and how to use the
    chosen actions, for the decision model."""
    return _split_block(answer, _GUIDANCE_BLOCK)


def split_after_block(answer: str) -> tuple[str, str | None]:
    """The answer without its ```after block, and the block's text: which actions may follow which."""
    return _split_block(answer, _AFTER_BLOCK)


def read_after(text: str | None, actions: list[str]) -> dict[str, tuple[str, ...]]:
    """The ```after block, `{action: [actions that may come next]}`, or `MacroRejection`. Every name must be an action of
    the step — a misspelt name would silently constrain nothing."""
    if text is None or not text.strip():
        return {}
    try:
        block = json.loads(text)
    except json.JSONDecodeError as error:
        raise MacroRejection(f"the ```after block is not JSON — {error.msg}.") from None
    if not isinstance(block, dict):
        raise MacroRejection('the ```after block must be an object: {"<action>": ["<action that comes next>", ...]}.')
    known = set(actions)

    def named(name: str) -> list[str]:
        # 인자를 받는 developer macro 를 호출 그대로 적어도(`developer:select(find(name="X"))`) 그 action 으로 읽는다. 같은 macro
        # 를 여러 번 고른 step 이면 `#2` 들까지 모두다(런 184: 호출을 적어 두 번 거절됐다).
        if name in known:
            return [name]
        base = name.split("(", 1)[0].strip()
        return sorted(action for action in known if action == base or action.startswith(base + "#"))

    after: dict[str, tuple[str, ...]] = {}
    for first, nexts in block.items():
        if isinstance(nexts, str):
            nexts = [nexts]
        if not isinstance(nexts, list) or not nexts or not all(isinstance(name, str) for name in nexts):
            raise MacroRejection(f'"{first}" in the ```after block must name a list of actions.')
        unknown = [name for name in [first, *nexts] if not named(name)]
        if unknown:
            raise MacroRejection(
                f"the ```after block names {', '.join(unknown)}, which is not an action of this step: {', '.join(sorted(known))}."
            )
        followers = tuple(dict.fromkeys(action for name in nexts for action in named(name)))
        for action in named(first):
            after[action] = followers
    return after


def split_facts_block(answer: str) -> tuple[str, str | None]:
    """The answer without its ```facts block, and the block's text."""
    return _split_block(answer, _FACTS_BLOCK)


def split_check_block(answer: str) -> tuple[str, str | None]:
    """The answer without its ```check block, and the block's text. Taken out first for the reason
    `split_plan_block` gives."""
    return _split_block(answer, _CHECK_BLOCK)


def _split_block(answer: str, block: re.Pattern[str]) -> tuple[str, str | None]:
    match = block.search(answer)
    if match is None:
        return answer, None
    return answer[: match.start()] + answer[match.end() :], match.group(1)


def read_observe(text: str | None) -> StepObserve | None:
    """The ```observe block as a `StepObserve`, `None` when there is none, or `MacroRejection`.

    `{"paths": ["<object path as the screen spells it>", ...], "members": ["Type.member", ...]}`. Either key
    may be left out; a left-out key does not narrow.
    """
    names = _read_names(text, "observe")
    return None if names is None else StepObserve(paths=names["paths"], members=names["members"])


def read_check(text: str | None) -> StepCheck | None:
    """The ```check block as a `StepCheck`, `None` when there is none or it names nothing, or `MacroRejection`.
    Same shape as ```observe.

    A block that names nothing is read as no block (ARTEL-1453). Taken literally it would record every live
    value, and refusing it made the planner write the whole step again for a block it meant as "nothing in
    particular" — 5 of 26 refusals in three weeks. The active scene is recorded either way (`_read_evidence`).
    """
    names = _read_names(text, "check")
    if names is None or (not names["paths"] and not names["members"]):
        return None
    return StepCheck(paths=names["paths"], members=names["members"])


def _read_names(text: str | None, kind: str) -> dict[str, tuple[str, ...] | None] | None:
    if text is None or not text.strip():
        return None
    try:
        block = json.loads(text)
    except json.JSONDecodeError as error:
        raise MacroRejection(f"the ```{kind} block is not JSON — {error.msg}.") from None
    if not isinstance(block, dict) or set(block) - {"paths", "members"}:
        raise MacroRejection(f'the ```{kind} block must be an object with only "paths" and "members" lists.')
    picked: dict[str, tuple[str, ...] | None] = {}
    for key in ("paths", "members"):
        names = block.get(key)
        if names is None or names == []:
            # 빈 목록은 그 축을 거르지 않는다는 뜻으로 읽는다. 모델은 "멤버는 거르지 않는다" 를 `"members": []` 로 쓰는데, 문자
            # 그대로 읽으면 값이 하나도 안 보인다 — 런 167 step 5 에서 Jev 가 HP·주문 준비 상태를 못 보고 턴 종료만 되풀이했다.
            picked[key] = None
            continue
        if not isinstance(names, list) or not all(isinstance(name, str) and name.strip() for name in names):
            raise MacroRejection(f'"{key}" in the ```{kind} block must be a list of non-empty strings.')
        if len(names) > MAX_OBSERVE_NAMES:
            raise MacroRejection(f'"{key}" in the ```{kind} block lists {len(names)} names; keep it to {MAX_OBSERVE_NAMES}.')
        picked[key] = tuple(name.strip() for name in names)
    return picked


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
        ledger: ActionLedger | None = None,
        facts: GameFacts = NO_FACTS,
        inputs: InputsReader | None = None,
    ) -> None:
        self._config = config
        # 개발자가 선언한 fact·rule. `facts="on"` 일 때만 요청에 싣고 ```facts 블록을 읽는다.
        self._game_facts = facts if spec.facts == "on" else NO_FACTS
        self._facts_text = planner_text(self._game_facts)
        # 런 단위 행동 장부. 계획 검증과 요청의 "이 런에서 확인된 것" 이 읽는다.
        self._ledger = ledger if ledger is not None else ActionLedger()
        # 지금 step 의 계획. 복구는 이것에 대한 수정으로 받는다. 새 step 을 쓸 때 비운다.
        self._draft: PlanDraft | None = None
        self._draft_version = 0
        self._channel = channel
        self._spec = spec
        self._registered = registered
        self._developer = developer
        # 런 동안 바뀌지 않으므로 한 번 그린다.
        self._developer_macros = (
            registered_action_list(developer.usable.values())
            if spec.actions == "guidance"
            else developer_macro_list(developer.usable.values())
        )
        role = {"contract": CONTRACT_RULES_ROLE, "guidance": GUIDANCE_RULES_ROLE}.get(spec.actions, RULES_ROLE)
        self._system = prompt_text(
            role, config.prompt_version, summary_language=SUMMARY_LANGUAGES.get(getattr(config.language, "value", str(config.language)), "English")
        )
        # FREE 시나리오에서만 읽는다. v3 전 버전에는 없는 파일이라 처음 쓸 때 읽는다.
        self._scene_system: str | None = None
        self._ask = ask or self._ask_model
        # step 의 대상 계약. runner 가 런 시작 때 채운다. 계획 대상이 아니라 요청에 읽기 전용으로 붙는다.
        self.targets: StepContracts | None = None
        # 지금 쓰는 답에 붙일 화면 한 장. `_write` 가 매번 새로 찍는다.
        self._image: ScreenImage | None = None
        # 이 빌드가 읽는 입력(SDK `list_inputs`). `_write`·`plan_scene` 이 매번 새로 묻는다 — Input System action 은
        # 씬마다 바뀐다.
        self._inputs_reader = inputs if inputs is not None else InputsReader(channel)
        self._inputs: str | None = None
        self._model = None
        # 요청의 앞부분 중 런 동안 바뀌지 않는 것(게임 지도, developer macro 목록, 시나리오 제목). Claude 호출이 그 끝에
        # cache 경계를 둔다(`prompt_messages`). 요청을 만들 때마다 다시 정한다.
        self._stable_prefix = ""

    async def write(self, scenario: QaScenario, step: int) -> Written:
        """Step `step`'s macros; rewritten while the parser refuses them, up to `max_rewrites`."""
        self._draft = None
        return await self._write(scenario, step, this_step(scenario, step), f"Step {step}", "step")

    async def replan(
        self, scenario: QaScenario, step: int, reason: str, turns: list[str], transition: dict | None = None
    ) -> Written:
        """Step `step` written again: the decision model chose `REPLAN` (ARTEL-1295 C), or an action got stuck
        under `repair="planner"`.

        `reason` is why play stopped, `turns` the last action turns as `recent_action_lines` writes them. The
        planner sees both above the current screen; the rest of the request is `write`'s. `transition` rides the
        decision log: why the step is written again, which attempt, the budget and the state version (묶음 2).
        """
        lines = "\n".join(f"  {line}" for line in turns) or "  (no action ran)"
        base = self._draft
        feedback = (
            "Your previous macros for this step were played and need a new plan:\n"
            f"  {reason}\n"
            f"The last turns:\n{lines}\n"
        )
        if base is None:
            feedback += "Write the step again for the screen as it is now."
        else:
            feedback += (
                f"\n{shown_plan(base)}\n\n"
                "Answer with the changes to this plan, for the screen as it is now: a ```changes block, then only "
                "what changes — a ```python block with the new or replaced defs, a ```developer block with developer "
                "macros to add, and ```observe, ```check or ```after only if they change. Everything you do not "
                "name stays as it is."
            )
        return await self._write(
            scenario, step, this_step(scenario, step), f"Step {step} (replan)", "replan", feedback, transition, base
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
        self._inputs = await self._inputs_reader.read()
        for attempt in range(self._spec.max_rewrites + 1):
            await self._channel.note(
                f"Scene {self._channel.scene.pulse.scene or '?'}: the planner is planning {len(steps)} cases"
                + (" again (the last answer was refused)" if attempt else "")
                + f". Prompt {self._config.prompt_version}.",
                LogCategory.THOUGHT,
                steps[0],
            )
            answer = normalize_paths(await self._ask(self._scene_system, self._scene_request(scenario, steps, feedback)))
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
        self._stable_prefix = self._run_context(scenario)
        return (
            self._stable_prefix
            + (f"{self._inputs}\n\n" if self._inputs else "")
            + f"Scene on screen: {self._channel.scene.pulse.scene or '(unknown)'}\n\n"
            + f"Candidate cases:\n{candidate_list(scenario, steps)}\n\n"
            + f"Current screen:\n{screen(self._channel.scene.pulse)}\n"
            + (f"\n{feedback}\n" if feedback else "")
        )

    async def _write(
        self,
        scenario: QaScenario,
        step: int,
        shown: str,
        label: str,
        kind: str,
        feedback: str = "",
        transition: dict | None = None,
        base: PlanDraft | None = None,
    ) -> Written:
        """`label` heads the notes ("Step 3", "Travel to `X` (for step 3)"); `kind` names the decision
        in the log (`step`, `travel`, `replan`). `feedback` opens the first request (a replan's reason)."""
        written = Written("", None, "nothing was written")
        # 거절은 쌓아서 다시 보낸다. 마지막 거절만 보내면 고친 것을 다음 답에서 되돌린다(런 179 step 4: 1·3번째 답이 같은
        # `press_key` 인자를 빠뜨렸다). replan 의 이유(`feedback`)도 다시 쓸 때마다 앞에 남긴다.
        opening, refusals = feedback, []
        self._image = (
            await capture_screen(self._channel, f"{label}: capturing the screen for the planner")
            if self._spec.screen_images == "on"
            else None
        )
        self._inputs = await self._inputs_reader.read()
        for attempt in range(self._spec.max_rewrites + 1):
            await self._channel.note(
                f"{label}: the planner is writing macros"
                + (" again (the last answer was refused)" if attempt else "")
                + f". Prompt {self._config.prompt_version}.",
                LogCategory.THOUGHT,
                step,
            )
            answer = normalize_paths(await self._ask(self._system, self._request(scenario, step, shown, feedback)))
            rest, summary = split_summary_block(answer)
            rest, guidance_summary = split_guidance_summary_block(rest)
            rest, observe_text = split_observe_block(rest)
            rest, check_text = split_check_block(rest)
            rest, after_text = split_after_block(rest)
            rest, calls = split_developer_block(rest)
            rest, changes_text = split_changes_block(rest)
            rest, actions_text = split_actions_block(rest)
            rest, guidance_text = split_guidance_block(rest)
            rest, facts_text = split_facts_block(rest)
            contract_mode = self._spec.actions == "contract"
            guidance_mode = self._spec.actions == "guidance"
            source = extract_code(rest)
            changes = None
            try:
                if base is not None:
                    # 복구는 지금 계획에 대한 수정이다. 바꾸지 않은 것은 그대로 남는다.
                    changes = read_changes(changes_text, base)
                    source, calls = merge(base, changes, source, calls)
                    observe_text = observe_text if observe_text is not None else base.observe_text
                    check_text = check_text if check_text is not None else base.check_text
                    after_text = after_text if after_text is not None else base.after_text
                    actions_text = actions_text if actions_text is not None else base.actions_text
                    guidance_text = guidance_text if guidance_text is not None else base.guidance_text
                    facts_text = facts_text if facts_text is not None else base.facts_text
                    summary = summary or base.summary
                    # 지침을 바꾸지 않은 수정이면 사람에게 보이는 지침도 그대로다.
                    guidance_summary = guidance_summary or (base.guidance_summary if guidance_text == base.guidance_text else None)
                observe = read_observe(observe_text)
                check = read_check(check_text)
                fact_names = read_facts_block(facts_text, self._game_facts) if self._game_facts.facts else ()
                if guidance_mode:
                    developer_actions, guided, binder = await bind_guidance_calls(calls, self._developer)
                    macros = await parse_guidance_step(
                        source, self._registered, developer_actions, guided, binder, guidance_text
                    )
                    # 지침 모드의 ```developer 줄은 이름 하나라 줄과 행동 키가 하나씩 맞는다.
                    pairs = tuple((DEVELOPER_ACTION_PREFIX + call.removesuffix("()"), call) for call in calls)
                else:
                    developer_actions = await bind_developer_calls(calls, self._developer)
                    macros = await parse_step(
                        source, self._registered, developer_actions, (actions_text or "") if contract_mode else None
                    )
                    pairs = tuple(zip(developer_actions, calls))
                after = read_after(after_text, [*macros.actions, *macros.contracts])
                if self._game_facts.facts:
                    check_guidance_facts(macros.guidance, fact_names, self._game_facts)
                written = Written(source, replace(macros, observe=observe, check=check, after=after, facts=fact_names))
                # 계획 검증(파서와 따로): 이 런에서 통과할 수 없다고 확인된 버전을 다시 고르지 않았는가.
                refused = self._ledger.check(written.macros, self._developer, self._channel.scene.pulse)
                if refused is not None:
                    raise MacroRejection(refused)
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
                refusals.append(written.refusal)
                listed = "\n".join(f"  answer {number}: {refusal}" for number, refusal in enumerate(refusals, 1))
                again = (
                    f"Answer the changes to plan {base.version} again" if base is not None else "Rewrite the whole source"
                )
                feedback = (
                    (f"{opening}\n\n" if opening else "")
                    + "Your previous answers cannot be used as written. They were refused:\n"
                    f"{listed}\n"
                    f"Each refusal names what is accepted instead. {again}, and do not bring back anything an earlier "
                    "answer was refused for."
                )
                continue
            self._draft_version += 1
            self._draft = PlanDraft(
                self._draft_version, source, pairs,
                tuple([*written.macros.actions, *written.macros.contracts]),
                observe_text, check_text, after_text, summary, actions_text, guidance_text, guidance_summary,
                facts_text,
            )
            actions = [*written.macros.actions, *written.macros.contracts]
            observe = written.macros.observe
            check = written.macros.check
            await note_decision(
                self._channel,
                f"{kind}_macros",
                f"{label}: {len(actions)} actions — {', '.join(actions)}",
                step,
                actions=actions,
                source=clip(source, MAX_SOURCE_CHARS),
                observe=None if observe is None else {"paths": observe.paths, "members": observe.members},
                check=None if check is None else {"paths": check.paths, "members": check.members},
                summary=summary.strip() if summary else None,
                after={first: list(nexts) for first, nexts in written.macros.after.items()} or None,
                screen_capture_id=self._image.capture_id if self._image else None,
                transition=transition,
                plan_version=self._draft_version,
                contracts={
                    name: {
                        "params": {p: list(d) for p, d in c.domains.items()} | {p: {"holds": h} for p, h in c.holds.items()},
                        "effect": c.effect.mode, "revision": c.revision,
                    }
                    for name, c in written.macros.contracts.items()
                } or None,
                guidance=written.macros.guidance,
                facts=list(written.macros.facts) or None,
                guidance_summary=guidance_summary.strip() if guidance_summary else None,
                changes=changes.logged() if changes is not None else None,
                descriptions={name: written.macros.description(name) for name in actions},
            )
            break
        return written

    def _run_context(self, scenario: QaScenario) -> str:
        """요청의 맨 앞, 런 동안 바뀌지 않는 부분. 순서를 지켜야 Claude 의 cache prefix 가 맞는다."""
        drawn = game_map(self._channel.scene.scene_context)
        return (
            (f"Game map:\n{drawn}\n\n" if drawn else "")
            + (f"{self._developer_macros}\n\n" if self._developer_macros else "")
            + self._facts_text
            + f"Scenario: {scenario.title}\n\n"
        )

    def _request(self, scenario: QaScenario, step: int, shown: str, feedback: str) -> str:
        self._stable_prefix = self._run_context(scenario)
        return (
            self._stable_prefix
            + self._ledger.lessons(self._channel.scene.pulse)
            + (f"{self._inputs}\n\n" if self._inputs else "")
            + f"Write step {step} now:\n{shown}\n\n"
            + planner_note(
                self.targets, step, step_text(scenario.steps[step]) if self.targets is not None and step < len(scenario.steps) else None
            )
            + f"Current screen:\n{screen(self._channel.scene.pulse)}\n"
            + (f"\n{feedback}\n" if feedback else "")
        )

    async def _ask_model(self, system: str, user: str) -> str:
        if self._model is None:
            self._model = build_chat_model(self._config.model, self._config.reasoning)
        images = [self._image.block()] if self._image is not None else None
        answer = await self._model.ainvoke(prompt_messages(self._config.model, system, user, self._stable_prefix, images))
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
