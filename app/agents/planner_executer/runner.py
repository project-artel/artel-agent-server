"""Runs one scenario on the planner–executer engine, in the place `QaRunner` takes for the agent.

`QaExecutionService` calls `run_with_deadline(channel, scenario)` on whichever runner the run's
engine names and reads back the state and, when the scenario did not close itself, why.

Each step: the planner writes the step's macros, the executer plays them with Jev, the evaluator
judges the step and sends the verdict the way the agent's `report_step` does. A step whose macros
are still refused after the planner's rewrites fails without being played. The closing `STATUS`
carries `QaRunState.build_summary`, so Orchestration grades both engines' runs the same way.

An ORDERED scenario plays its steps in order. A FREE scenario is a set of test cases, one per step,
in no order (ARTEL-1149), played scene by scene. In each scene the planner gets the cases still to
verify there (`free.candidates`) and answers which to verify now, in what order, with shared actions
and one goal per case; the executer plays them one after another while the scene stays, and the
scene evaluator judges them a few at a time. A scene with more cases than one plan takes is planned
again with the rest. Then the planner writes a move to the next scene that still has cases
(`free.next_scene`). A case put off — left `later` by the planner, in a scene play could not reach,
or played without ever reaching its goal — waits for the next round, after `reset_game`, up to
`free_max_resets` resets and `free_case_attempts` plays per case; what is still left is reported
failed as UNREACHED. The branch is per scenario: a session that mixes ORDERED and FREE scenarios is
recorded as ORDERED, and its FREE scenarios still play this way.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, replace
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.agents.qa.reset import reset_game_cheat
from app.agents.editor import MacroEditor
from app.agents.evaluator.verdict import StepVerdict
from app.agents.evaluator import (
    Evaluator,
    FailureAnalyzer,
    FailureReport,
    Judge,
    RunReviewer,
    SceneEvaluator,
    engine_error,
    insufficient,
    send_verdict,
    unobservable,
)
from app.agents.evaluator import (
    unreached as unreached_report,
)
from app.agents.planner_executer.decisions import MAX_REFUSAL_CHARS, clip, note_decision
from app.agents.planner_executer.evidence import readings
from app.agents.planner_executer.developer_macros import (
    DEVELOPER_ACTION_PREFIX,
    NO_DEVELOPER_MACROS,
    LoadedDeveloperMacros,
)
from app.agents.planner_executer.executer import (
    GOAL_STOPS,
    PASSED_IN_EARLIER_STEP,
    RESULT_IN_EARLIER_STEP,
    Executer,
    StepPlay,
    merge_plays,
    segment_play,
    recent_action_lines,
)
from app.agents.planner_executer.free import (
    FreeCase,
    candidates,
    free_cases,
    next_scene,
    same_scene,
)
from app.agents.planner_executer.inputs import InputsReader
from app.agents.planner_executer.macros import RegisteredMacros, StepMacros, step_of_segment
from app.agents.planner_executer.observe import watch
from app.agents.planner_executer.planner import Ask, Planner, Written
from app.agents.planner_executer.ledger import ActionLedger
from app.agents.planner_executer.prompts import screen, step_text
from app.agents.planner_executer.screen_capture import capture_screen
from app.agents.planner_executer.facts import NO_FACTS, GameFacts, load_game_facts, watch_facts
from app.agents.planner_executer.identities import GameIdentity
from app.config import get_settings
from app.agents.planner_executer.target import StepContracts, StepTarget, TargetResolver, guard, observe
from app.agents.qa.macro.binding import MacroMemories
from app.agents.qa.runner import _step_plan
from app.agents.qa.tools import QaRunState
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import (
    JsonRpcAction,
    MessageType,
    RunResult,
    StatusPayload,
    StepStatus,
)
from app.qa.jev import JevClient
from app.qa.provider_error import classify_provider_error
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

# How long the run waits for a first reading before it writes from an empty screen.
FIRST_READING_SECONDS = 5.0
# How long a FREE round waits for the game's first reading after `reset_game`.
RESET_READING_SECONDS = 10.0



# 미리 쓰기: 씬이 바뀌었는지 보는 간격, 바뀐 뒤 객체가 들어오기를 기다리는 시간, 한 step 에서 다시 쓰는 횟수의 상한.
SCENE_POLL_SECONDS = 0.5
SCENE_SETTLE_SECONDS = 1.5
MAX_EARLY_RESTARTS = 2
# `pause_time`·`resume_time` 의 답을 기다리는 시간. SDK 가 바로 답하는 액션이라 짧게 둔다.
GAME_TIME_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger(__name__)


@dataclass
class Early:
    """다음 step 의 미리 쓰는 계획(pe9 `plan_ahead`): 어느 step 인가, 쓰기 시작할 때의 씬, 쓰는 task."""

    step: int
    scene: str | None
    task: "asyncio.Task[Written]"


def current_scene(channel: QaRunChannel) -> str | None:
    """지금 화면의 씬 이름. 아직 읽은 것이 없으면 `None` 이다."""
    return MacroMemories(scene=channel.scene).scene_name or None


@dataclass(frozen=True)
class Held:
    """묶음이 끝나기를 기다리는 step 하나의 play 와, 판정에 함께 쓰는 끝 화면·대상(pe9)."""

    step: int
    play: StepPlay
    image: Any
    target: Any
    # 판정 step 이 `done()` 으로 멈춘 자리에서 이미 받은 판정(pe12). 묶음이 아니면 이것을 그대로 쓴다 — 같은 기록, 같은 화면이다.
    verdict: Any = None

class PlannerExecuterRunner:
    def __init__(
        self,
        config: RunConfig,
        ask: Ask | None = None,
        judge: Judge | None = None,
        jev: JevClient | None = None,
        developer: LoadedDeveloperMacros = NO_DEVELOPER_MACROS,
        resolve: Callable[[str, str], Awaitable[object]] | None = None,
        identities: tuple[GameIdentity, ...] = (),
        facts: GameFacts | None = None,
    ) -> None:
        """`ask` (planner), `judge` (evaluator) and `jev` are replaced by tests; a run uses the run's
        model for the first two and `app/qa/jev.py`'s client for Jev. `developer` 는
        `QaExecutionService` 가 런 시작에 한 번 읽고 파싱해 시나리오마다 같은 것을 넘긴다. `resolve` 는 대상 해석의
        모델 호출이고 테스트가 바꿔 끼운다. `identities` 는 개발자가 선언한 게임 값의 뜻이고 해석이 읽는다."""
        self._config = config
        self._resolve = resolve
        self._identities = identities
        self._developer = developer
        assert config.arch.planner is not None, "resolve_arch fills `planner` for this engine"
        self._spec = config.arch.planner
        # 개발자가 선언한 fact·rule. 런은 service 가 orchestration(또는 `qa_facts_dir`)에서 읽어 넘긴다. 넘기지 않았으면
        # `qa_facts_dir` 만 본다.
        if facts is None:
            facts = load_game_facts(get_settings().qa_facts_dir) if self._spec.facts == "on" else NO_FACTS
        self._facts = facts
        self._ask = ask
        self._judge = judge
        self._jev = jev

    async def run_with_deadline(
        self, channel: QaRunChannel, scenario: QaScenario
    ) -> tuple[QaRunState, str | None]:
        """Returns the state and, when the scenario did not close cleanly, why."""
        state = QaRunState(
            total_steps=len(scenario.steps),
            step_meta=_step_plan(scenario),
            step_texts=[step_text(step) for step in scenario.steps],
            execution_mode=scenario.execution_mode,
        )
        jev = self._jev or JevClient()
        deadline = self._config.arch.deadline_seconds
        try:
            await asyncio.wait_for(self._run(channel, scenario, state, jev), timeout=deadline)
        except asyncio.TimeoutError:
            return state, f"The run exceeded its {int(deadline)}s limit."
        except QaCancelled:
            return state, None
        except Exception as error:  # noqa: BLE001 - the reason has to reach the timeline
            state.provider_failure = classify_provider_error(error)
            return state, f"The run stopped on an error: {type(error).__name__}: {error}"
        finally:
            if self._jev is None:
                await jev.aclose()
        return state, None

    async def _run(
        self, channel: QaRunChannel, scenario: QaScenario, state: QaRunState, jev: JevClient
    ) -> None:
        if not channel.scene.pulse.seen:
            await channel.wait_for_reading(FIRST_READING_SECONDS)
        registered = RegisteredMacros(channel)
        # 런 단위 행동 장부: play 마다 실패를 종류로 쌓고, planner 가 계획을 검증할 때 읽는다.
        self._ledger = ActionLedger()
        # 이 빌드가 읽는 입력(SDK `list_inputs`). planner 와 editor 가 나눠 써서, 옛 SDK 의 거절을 한 번만 듣는다.
        inputs = InputsReader(channel)
        planner = Planner(
            self._config, channel, self._spec, registered, ask=self._ask, developer=self._developer, ledger=self._ledger,
            facts=self._facts, inputs=inputs,
        )
        # 앞 step 이 도는 동안 다음 step 을 미리 쓰는 planner(pe9 `plan_ahead`). 본 planner 와 계획을 나누지 않아, 앞 step 의
        # 재계획이 미리 쓰는 계획을 덮지 않는다.
        self._ahead_planner = (
            Planner(
                self._config, channel, self._spec, registered, ask=self._ask, developer=self._developer,
                ledger=self._ledger, facts=self._facts, inputs=inputs, ahead=True,
            )
            if self._spec.plan_ahead == "on"
            else None
        )
        executer = Executer(channel, self._spec, jev, facts=self._facts)
        if self._spec.facts == "on":
            await note_decision(
                channel, "game_facts",
                f"Game facts: {len(self._facts.facts)} facts, {len(self._facts.rules)} rules"
                + (f" from {self._facts.origin} (revision {self._facts.revision})" if self._facts.revision else " (none loaded)"),
                None, revision=self._facts.revision or None, origin=self._facts.origin or None,
                facts=[fact.name for fact in self._facts.facts],
                rules=[rule.name for rule in self._facts.rules],
            )
            await watch_facts(channel, self._facts)
        # 런 중 macro 수정(ARTEL-1288). `editor="off"` 면 executer 는 멈춤 규칙만 더한 채 돈다.
        self._editor = MacroEditor(
            self._config, channel, self._spec, registered, ask=self._ask, developer=self._developer, inputs=inputs
        ) if (
            self._spec.editor == "on"
        ) else None
        judge = Evaluator(self._config, self._judge)
        self._coverage_judge = judge
        # 실패 분석과 런 리뷰(ARTEL-1267). `analysis="off"` 면 둘 다 없고 `STATUS` 는 pe2 그대로다.
        analysing = self._spec.analysis == "on"
        self._analyzer = FailureAnalyzer(self._config, self._judge) if analysing else None
        self._reviewer = RunReviewer(self._config, self._judge) if analysing else None
        self._reports: dict[int, FailureReport] = {}
        # 시나리오의 `observe` 이름을 SDK 에 한 번 청한다. 못 푼 이름은 case 별로 쥐고, 그 case 의
        # 실패는 모델을 부르지 않고 `INCONCLUSIVE` 로 채운다(ARTEL-1279).
        self._unobservable = await watch(channel, scenario)

        if scenario.execution_mode == "FREE":
            await self._run_free(channel, scenario, state, planner, executer, SceneEvaluator(self._config, self._judge))
        else:
            # 대상 계약: 첫 계획 전에 대상의 뜻을 한 번 고정한다. planner 는 읽기만 한다.
            targets = await self._resolve_targets(channel, scenario, executer) if self._spec.target_contract == "on" else None
            planner.targets = targets
            # 계획을 받은 화면에서 끝 조건을 읽는다(pe11). planner 는 게임을 멈춘 채 쓰므로 그 화면이 play 가 시작할 화면이다.
            planner.start_check = lambda macros, step: self._start_refusal(executer, macros, step)
            # 판정(evaluator·실패 분석)은 다음 step 의 계획·플레이와 겹쳐 돈다. 런 418 에서 실패한 step 마다 판정 두 호출이
            # 약 30초씩 다음 계획을 막았고, 다음 planner 는 앞 판정을 읽지 않는다. 판정끼리는 앞 판정이 끝난 뒤에 시작하므로
            # evaluator 가 읽는 앞 step 들의 판정과 STATUS 의 순서는 그대로다. 끝 화면은 다음 step 전에 지금 찍는다.
            settling: asyncio.Task[None] | None = None
            # 앞 step 의 계획이 ```segment 로 함께 맡은 뒤 step 들(pe9). 그 step 은 planner 를 다시 부르지 않고 이 계획으로 돈다.
            # 앞 step 이 제 완료 조건에서 멈추지 않았으면 버린다 — 그 계획은 지금 화면을 전제하지 않는다.
            ahead: dict[int, StepMacros] = {}
            # 묶음이 끝나기를 기다리는 step 들의 play(pe9). 묶음의 마지막 step 이 끝나거나 묶음이 끊기면 함께 판정한다.
            held: list[Held] = []
            # 다음 step 의 미리 쓴 계획(pe9 `plan_ahead`).
            early: Early | None = None
            # 묶음의 앞 step 이 뒤 step `done_k` 로 멈췄으면 그 k(pe11). 그 사이 step 과 k 는 입력 없이 끝난다.
            ended_through: int | None = None

            def release() -> None:
                nonlocal settling, held
                if not held:
                    return
                group, held = held, []
                settling = self._after(settling, self._settle_group(channel, state, scenario, judge, group, targets is not None))

            try:
                for step in range(1, len(scenario.steps) + 1):
                    if settling is not None and settling.done():
                        # 앞 판정이 예외로 끝났으면 여기서 올린다. 다음 step 을 돌린 뒤에야 알게 두지 않는다.
                        settling.result()
                    target = targets.get(step) if targets is not None else None
                    if targets is not None and target is not None and target.status == "fixed":
                        # 확인할 값은 그 step 을 시작할 때 지금 값과 선언으로 고른다. 뜻은 바뀌지 않는다.
                        target = observe(target, await executer.read_values(), self._identities)
                        targets = targets.with_step(target)
                        planner.targets = targets
                        await note_decision(channel, "step_target", f"Step {step}: target {target.status}", step, **target.logged())
                    written: Written | None = None
                    # 이 step 의 계획이 시작 화면에서 끝 조건을 거짓으로 읽었나(pe11). 묶음의 뒤 step 은 앞 step 의 마지막 turn 이 읽었다.
                    checked = False
                    finished: str | None = None
                    if step in ahead:
                        if early is not None:
                            # 묶음이 이 step 을 맡았는데 미리 쓴 계획이 있다면 그것은 쓰지 않는다.
                            early.task.cancel()
                            early = None
                        candidate = ahead.pop(step)
                        if ended_through is not None and step <= ended_through:
                            # 앞 step 이 이 step 의 결과에서(또는 그 뒤에서) 멈췄다. 결과는 앞 step 기록에 있다.
                            finished = RESULT_IN_EARLIER_STEP if step == ended_through else PASSED_IN_EARLIER_STEP
                            written, checked = Written("", candidate), True
                        elif (candidate.actions or candidate.contracts) and await executer.holds(candidate.probe, step):
                            # 앞 step 이 제 완료 조건에서 멈췄는데 이 step 의 완료 조건이 벌써 참이다. "뒤 step 의 `done_` 은 앞 step 이
                            # 멈춘 곳에서 거짓이어야 한다" 를 어긴 계획이다. 묶음을 끊고 이 step 을 새로 쓴다.
                            ahead.clear()
                            await note_decision(
                                channel, "segment_broken",
                                f"Step {step}: the segment plan was dropped: this step's done() already holds where the earlier step stopped",
                                step,
                            )
                        else:
                            written, checked = Written("", candidate), True
                        if written is not None:
                            await note_decision(
                                channel, "segment_step", f"Step {step}: played with the plan written for the earlier step", step,
                                goals=[name for name, _goal in written.macros.goals] or None,
                                finished=finished,
                            )
                    if written is None:
                        ahead.clear()
                        used = None
                        async with self._game_paused(channel, step):
                            if early is not None and early.step == step:
                                used = await self._use_early(channel, planner, early)
                                if used is not None and used.macros is not None:
                                    # 미리 쓴 계획은 다른 화면에서 썼다. 받는 지금 화면에서 끝 조건을 읽는다.
                                    started = await self._start_refusal(executer, used.macros, step)
                                    if started is not None:
                                        await note_decision(
                                            channel, "ahead_plan_dropped",
                                            f"Step {step}: the plan written ahead was not used ({clip(started, MAX_REFUSAL_CHARS)})",
                                            step, reason=clip(started, MAX_REFUSAL_CHARS), ahead=True,
                                        )
                                        used = None
                            elif early is not None:
                                early.task.cancel()
                            early = None
                            written = used if used is not None else await planner.write(scenario, step)
                        checked = written.macros is not None and not written.unchecked
                        if written.macros is not None and written.macros.segment:
                            ahead = {
                                number: step_of_segment(written.macros, number)
                                for number in written.macros.segment if number > step
                            }
                            written = Written(written.source, step_of_segment(written.macros, step), written.refusal, written.unchecked)
                    if written.macros is None:
                        release()
                        message = (
                            f"PLANNER-REFUSED: the step's macros were still refused after "
                            f"{self._spec.max_rewrites} rewrites: {written.refusal}"
                        )
                        settling = self._after(settling, self._send_verdict(
                            channel, state, scenario, step, False, message, self._engine_error(message)
                        ))
                        continue
                    following = None
                    if self._ahead_planner is not None and step < len(scenario.steps) and step + 1 not in ahead:
                        # 다음 step 이 이 계획의 묶음에 없으면 이 step 이 도는 동안 미리 쓴다. 이 step 에서 씬이 바뀌면 새 씬에서
                        # 다시 쓴다(`_follow_scene`).
                        early = self._start_early(channel, scenario, step + 1, step)
                        holder = [early]
                        following = asyncio.create_task(self._follow_scene(channel, scenario, step, holder))
                    try:
                        play = await self._play_with_replans(
                            scenario, step, planner, executer, written.macros, target, checked=checked, finished=finished,
                            precondition=case_precondition(scenario, step) if finished is None else None,
                        )
                    finally:
                        if following is not None:
                            following.cancel()
                            early = holder[0]
                    if early is not None and early.step == step + 1 and current_scene(channel) != early.scene:
                        # 이 step 이 끝날 때 씬이 바뀌어 있으면(씬 추적이 다시 쓰기 전에 끝났다) 지금 씬에서 다시 쓰기 시작한다.
                        early.task.cancel()
                        early = self._start_early(channel, scenario, step + 1, step)
                    if play is None:
                        release()
                        message = (
                            "PLANNER-REFUSED: the replanned macros were still refused after "
                            f"{self._spec.max_rewrites} rewrites"
                        )
                        settling = self._after(settling, self._send_verdict(
                            channel, state, scenario, step, False, message, self._engine_error(message)
                        ))
                        continue
                    if play.later_step is not None:
                        ended_through = play.later_step
                    if (play.stopped not in GOAL_STOPS and play.stopped != PASSED_IN_EARLIER_STEP) or play.rewritten:
                        # 뒤 step 의 계획은 이 step 이 끝 조건에서 멈춘 화면을 전제한다. 다시 쓴 계획은 묶음을 받지 않는다.
                        ahead.clear()
                    if not ahead:
                        ended_through = None
                    # `done()` 자리에서 이미 판정을 받았고 묶음이 이어지지 않으면 그 판정을 쓴다. 화면도 그때 찍었다(pe12).
                    reuse = getattr(play, "judged", None) is not None and not ahead
                    image = (
                        await capture_screen(channel, f"Step {step}: capturing the screen for the evaluator", step)
                        if self._spec.screen_images == "on" and not reuse
                        else None
                    )
                    held.append(Held(step, play, image, target, play.judged if reuse else None))
                    if not ahead:
                        # 묶음이 끝났거나(남은 step 이 없다) 묶음이 아니었다. 묶음의 뒤 step 이 남아 있으면 그 step 까지 기다린다.
                        release()
                release()
                if settling is not None:
                    await settling
                    settling = None
            finally:
                if early is not None:
                    early.task.cancel()
                if settling is not None and not settling.done():
                    # 런이 끊겼다. 뒤에서 돌던 판정을 남기지 않는다.
                    settling.cancel()
        state.finished = True
        passed = all(result.passed for result in state.step_results)
        review = None
        if self._reviewer is not None:
            # 정상 종료에만 한다. 취소·시간 초과로 끊긴 run 은 `service._send_terminal` 이 닫고, 그 run 을
            # 통째로 읽으면 끊김을 게임 탓으로 읽는다.
            review = await self._reviewer.review(scenario, state, self._reports)
            if review is not None:
                await note_decision(
                    channel,
                    "run_review",
                    review.overview,
                    patterns=review.patterns,
                    insights=review.insights,
                )
        await channel.emit(
            MessageType.STATUS,
            StatusPayload(
                status=StepStatus.COMPLETED,
                result=RunResult.PASSED if passed else RunResult.FAILED,
                message=f"{sum(r.passed for r in state.step_results)}/{state.total_steps} steps passed.",
                summary=state.build_summary(),
                analysis=review.model_dump() if review is not None else None,
            ),
        )

    def _after(self, previous: "asyncio.Task[None] | None", work: Awaitable[None]) -> "asyncio.Task[None]":
        """`previous` 가 끝난 뒤 `work` 를 돌리는 task. step 판정을 앞 판정 뒤로 줄 세운다.

        앞 판정이 실패하면 뒤 판정도 같은 예외로 끝나고, 마지막 task 를 기다리는 쪽(step 루프의 끝)이 그것을 받는다.
        """

        async def chained() -> None:
            try:
                if previous is not None:
                    await previous
            except BaseException:
                # 앞이 끝나지 못하면 이 판정은 시작하지 않는다. 만들어 둔 coroutine 을 닫아 경고 없이 버린다.
                close = getattr(work, "close", None)
                if close is not None:
                    close()
                raise
            await work

        return asyncio.create_task(chained())

    def _start_early(self, channel: QaRunChannel, scenario: QaScenario, step: int, playing: int) -> "Early":
        """step `step` 의 계획을 지금 화면으로 뒤에서 쓰기 시작한다. 쓸 때의 씬을 함께 쥔다."""
        assert self._ahead_planner is not None
        task = asyncio.create_task(self._ahead_planner.write_ahead(scenario, step, playing))
        # 쓰지 않고 버린 task 의 예외가 "회수되지 않음" 경고로 남지 않게 한다.
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        return Early(step, current_scene(channel), task)

    async def _follow_scene(self, channel: QaRunChannel, scenario: QaScenario, playing: int, holder: "list[Early]") -> None:
        """앞 step 이 도는 동안 씬이 바뀌면 미리 쓰던 계획을 버리고 새 씬에서 다시 쓴다. 새 씬의 객체가 보여야 다음 step 을 쓸
        수 있다. 씬이 막 바뀐 뒤 객체가 들어올 시간을 조금 둔다."""
        restarts = 0
        while restarts < MAX_EARLY_RESTARTS:
            await asyncio.sleep(SCENE_POLL_SECONDS)
            early = holder[0]
            now = current_scene(channel)
            if not now or now == early.scene:
                continue
            await asyncio.sleep(SCENE_SETTLE_SECONDS)
            early.task.cancel()
            holder[0] = self._start_early(channel, scenario, early.step, playing)
            await note_decision(
                channel, "ahead_plan_restarted", f"Step {early.step}: the scene changed to `{now}`; writing it again ahead",
                early.step, scene=now, ahead=True,
            )
            restarts += 1

    @contextlib.asynccontextmanager
    async def _game_paused(self, channel: QaRunChannel, step: int) -> AsyncIterator[None]:
        """계획을 기다리는 동안 게임 시간을 멈춘다(pe10 `pause_while_planning`). 멈추지 못했으면 그냥 기다린다.

        SDK `pause_time` 은 `Time.timeScale` 을 0 으로 둔다. SDK 의 대기와 상태 읽기는 unscaled 라 멈춘 동안에도 planner 가 읽는
        화면은 그대로 온다. orchestration 은 멈춘 동안의 화면을 녹화하지 않고 그 구간을 `PAUSED` 로 남긴다.
        """
        if self._spec.pause_while_planning != "on":
            yield
            return
        paused = await self._set_game_time(channel, "pause_time", "Pausing game time while the plan is written", step)
        try:
            yield
        finally:
            if paused:
                await self._set_game_time(channel, "resume_time", "Resuming game time", step)

    async def _set_game_time(self, channel: QaRunChannel, method: str, message: str, step: int) -> bool:
        """`pause_time`·`resume_time` 하나를 보내고 SDK 가 받았는지 돌려준다."""
        try:
            result = await channel.dispatch_actions(
                [JsonRpcAction(id=1, method=method)], message, step, timeout=GAME_TIME_TIMEOUT_SECONDS
            )
        except QaCancelled:
            raise
        except Exception:  # 멈추지 못해도 계획은 쓴다
            logger.warning("[QA] %s failed", method, exc_info=True)
            return False
        ok = result is not None and all(getattr(item, "success", True) for item in result.results)
        if not ok:
            logger.warning("[QA] %s was not accepted: %s", method, result)
        return ok

    async def _use_early(self, channel: QaRunChannel, planner: Planner, early: "Early") -> "Written | None":
        """미리 쓴 계획을 확인하고 쓸 수 있으면 본 planner 로 넘긴다. 못 쓰면 이유를 남기고 `None` 이다.

        계획을 쓸 때의 씬이 지금 씬과 같아야 한다. 다른 씬에서 쓴 계획은 지금 화면의 객체를 모른다(Minecraft 사례처럼, 받는 사이
        단계가 바뀌면 버린다). 버리면 지금처럼 새로 쓴다 — 잃는 것은 그 호출 하나다.
        """
        assert self._ahead_planner is not None
        now = current_scene(channel)
        if now != early.scene:
            # 다른 씬에서 쓰는 계획은 다 쓰기를 기다리지 않고 버린다. 로컬 try 23 step 3 은 Title 에서 쓰던 계획을 76초 기다린 뒤
            # 버렸다.
            early.task.cancel()
            await note_decision(
                channel, "ahead_plan_dropped",
                f"Step {early.step}: the plan written ahead was not used (written on `{early.scene}`, and the screen is now on `{now}`)",
                early.step, reason=f"written on `{early.scene}`, and the screen is now on `{now}`", ahead=True,
            )
            return None
        try:
            written = await early.task
        except asyncio.CancelledError:
            if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                raise
            written, problem = None, "the plan written ahead was cancelled"
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - 미리 쓴 계획이 실패하면 새로 쓴다
            written, problem = None, f"{type(error).__name__}: {str(error)[:200]}"
        else:
            now = current_scene(channel)
            if written.macros is None:
                problem = f"refused: {clip(written.refusal or '', MAX_REFUSAL_CHARS)}"
            elif now != early.scene:
                problem = f"written on `{early.scene}`, and the screen is now on `{now}`"
            else:
                problem = None
        if problem is not None or written is None or self._ahead_planner.logged is None:
            await note_decision(
                channel, "ahead_plan_dropped", f"Step {early.step}: the plan written ahead was not used ({problem})",
                early.step, reason=problem, ahead=True,
            )
            return None
        planner.adopt(self._ahead_planner)
        message, _step, fields = self._ahead_planner.logged
        await note_decision(
            channel, "step_macros", f"{message} (written ahead)", early.step,
            **{**fields, "plan_version": planner.draft_version}, written_ahead=True,
        )
        return written

    async def _settle_group(
        self,
        channel: QaRunChannel,
        state: QaRunState,
        scenario: QaScenario,
        judge: Evaluator,
        group: "list[Held]",
        contracted: bool,
    ) -> None:
        """한 계획이 함께 돈 step 들의 판정(pe9). 묶음이면 각 step 을 묶음 전체의 기록으로, 앞 step 부터 차례로 판정한다.

        step 사이 경계는 executer 가 다음 step 의 완료 조건으로 짐작한 것이다. 로컬 try 18 step 4 는 시작하자마자 끝났다고
        보고 대사를 넘긴 14 turn 은 모두 step 5 에 찍혀, step 4 만 따로 본 evaluator 는 "대사가 그대로" 라며 실패로 판정했다.
        묶음 전체를 보면 경계가 틀려도 결과가 나타난 자리를 찾는다. 대상 계약 확인·화면 기록은 그 step 의 play 로 한다.
        """
        steps = [held.step for held in group]
        if len(group) > 1:
            await note_decision(channel, "segment_judgement", f"Steps {steps[0]}-{steps[-1]}: judged together", steps[0], steps=steps)
        whole = segment_play([(held.step, held.play) for held in group])
        for held in group:
            # 화면은 그 step 이 끝날 때의 것이다. 글자 없이 그림으로만 보이는 결과(타이틀 그림)는 이 화면으로만 확인된다 —
            # 로컬 try 20 은 묶음 끝 화면을 붙여 step 1 의 타이틀을 못 보고 실패로 판정했다.
            # 묶음 기록은 같지만 멈춘 이유는 step 마다 다르다. 마지막 step 의 이유를 모든 step 에 붙이면, 뒤 step 의 결과로 끝난
            # step 에 "입력을 보내지 않음" 이 달렸다(로컬 try 26 step 3·4).
            judged = (
                replace(whole, stopped=held.play.stopped, precondition=held.play.precondition) if len(group) > 1 else None
            )
            await self._settle(
                channel, state, scenario, held.step, judge, held.play, held.image,
                held.target, contracted, judged=judged, segment=steps if len(group) > 1 else None,
                ready=held.verdict if len(group) == 1 else None,
            )

    async def _settle(
        self,
        channel: QaRunChannel,
        state: QaRunState,
        scenario: QaScenario,
        step: int,
        judge: Evaluator,
        play: StepPlay,
        image: Any,
        target: Any,
        contracted: bool,
        judged: StepPlay | None = None,
        segment: list[int] | None = None,
        ready: "StepVerdict | None" = None,
    ) -> None:
        """한 step 의 판정: evaluator, 대상 계약 확인, 실패 분석, `STATUS`. 앞 step 의 판정이 끝난 뒤에 불린다.

        `judged` 는 evaluator·실패 분석이 읽을 기록이다. 묶음이면 묶음 전체의 것이고(`segment`), 아니면 이 step 의 play 다.
        """
        record = judged if judged is not None else play
        if ready is not None:
            verdict = ready
        elif segment:
            verdict = await judge.judge(scenario, step, record, earlier_verdicts(scenario, state), image, segment=segment)
        else:
            verdict = await judge.judge(scenario, step, record, earlier_verdicts(scenario, state), image)
        details = verdict.details()
        if contracted:
            # 모델의 답을 대상 계약으로 거른다. 대상 불일치·미확인, 예측과 다른 움직임은 통과로 남지 않는다.
            read = readings(play.evidence)
            verdict, check = guard(verdict, target, read[0]["values"] if read else None, read[-1]["values"] if read else None)
            if check is not None and target is not None:
                logged = check.logged(target)
                details = {**verdict.details(), "target": logged}
                await note_decision(channel, "target_check", f"Step {step}: target {check.state}", step, **logged)
        analysis = (
            None if verdict.passed
            else await self._analyse(scenario, step, record, verdict.message, verdict.evidence_sufficient)
        )
        await self._send_verdict(channel, state, scenario, step, verdict.passed, verdict.message, analysis, play, details)

    async def _send_verdict(
        self,
        channel: QaRunChannel,
        state: QaRunState,
        scenario: QaScenario,
        step: int,
        passed: bool,
        message: str,
        analysis: FailureReport | None = None,
        play: StepPlay | None = None,
        details: dict | None = None,
    ) -> None:
        """One step's verdict and, for a failed step, its analysis — kept for the run review too.

        `play` is the play the verdict is about; its frames go out as `played`. A step that was never
        played (refused macros, an unreached FREE case) passes none. `details` is the Evaluator's
        `state_detail` and `insight` (`StepVerdict.details`); a step it did not judge has none.
        """
        if analysis is not None:
            self._reports[step] = analysis
        await send_verdict(
            channel,
            state,
            step,
            passed,
            message,
            analysis=analysis,
            played=play.played() if play else None,
            verdict=details,
        )

    async def _resolve_targets(self, channel: QaRunChannel, scenario: QaScenario, executer: Executer) -> StepContracts:
        """대상의 뜻을 런 시작 때 한 번 고정하고 로그에 남긴다."""
        baseline = await executer.read_values()
        contracts = await TargetResolver(self._config, self._resolve, self._identities).resolve(scenario, baseline, version=1)
        fixed = sum(target.status == "fixed" for target in contracts.steps.values())
        unknown = sum(target.status == "unknown" for target in contracts.steps.values())
        await note_decision(
            channel,
            "step_contract",
            f"Step contract: {fixed} steps with a fixed target, {unknown} whose target could not be fixed",
            **contracts.logged(),
        )
        return contracts

    async def _play_with_replans(
        self,
        scenario: QaScenario,
        step: int,
        planner: Planner,
        executer: Executer,
        macros,
        target: StepTarget | None = None,
        checked: bool = False,
        finished: str | None = None,
        precondition: str | None = None,
    ) -> StepPlay | None:
        """`precondition` 은 이 step 에서 시작하는 TC 의 사전 조건이다(pe16). play 전에 화면과 대조하고, 성립하지 않으면 planner 가
        그것을 먼저 맞추도록 한 번 다시 쓴다. 대조 결과는 판정에 함께 간다.

        Play the step, and have the planner write it again while it is asked to and has budget left: no action was
        ready or every action was out, the scene changed under the plan (`off_plan`, pe11), or an action got stuck under
        `repair="planner"`. `checked` and `finished` go to the first play (`Executer.play`); a plan written again carries
        its own check.

        Every play of the step reaches the evaluator, joined (`merge_plays`), and the step's time budget runs on
        across them. A rewrite refused to the end ends play there; what was played before it is still judged.
        """
        spec = self._spec
        plays: list[StepPlay] = []
        replans = repairs = scene_replans = coverages = 0
        started = executer.now()
        planner_repairs = spec.repair == "planner" and spec.editor == "on"
        checked_precondition: dict | None = None
        if precondition and spec.precondition_check == "on":
            checked_precondition = await executer.precondition(precondition)
            await note_decision(
                executer.channel, "precondition_check",
                f"Step {step}: the test case's precondition {checked_precondition['status']}", step, **checked_precondition,
            )
            if checked_precondition["status"] == "unmet":
                reason = (
                    "Before this step's own move, this test case's precondition must hold, and it does not on the screen "
                    f"now: \"{precondition}\". Plan the moves that make it hold first — close what covers the screen, "
                    "finish what is still in progress — then the step's move."
                )
                transition = {"trigger": "precondition", "attempt": 1, "budget": 1, "precondition": precondition}
                async with self._game_paused(executer.channel, step):
                    written = await planner.replan(scenario, step, reason, [], transition)
                if written.macros is not None:
                    macros = written.macros
                    checked = not written.unchecked
                    checked_precondition["rewritten"] = True
        while True:
            remaining = spec.step_budget_seconds - (executer.now() - started)
            play = await executer.play(
                step,
                step_text(scenario.steps[step - 1]),
                macros,
                None if planner_repairs else self._repair(scenario, step),
                replans < spec.max_replans_per_step,
                budget_seconds=max(remaining, 0.0),
                can_ask_planner=planner_repairs and repairs < spec.max_repairs_per_step,
                target=target,
                history=[turn for earlier in plays for turn in earlier.turns],
                checked=checked,
                finished_earlier=finished if not plays else None,
                scene_bound=True,
                can_replan_scene=scene_replans < spec.max_scene_replans_per_step,
            )
            plays.append(play)
            self._ledger.record(step, play, macros, self._developer)
            if play.repair_request is not None:
                repairs += 1
                reason, lines = repair_brief(play, self._developer)
                transition = {"trigger": "repair", "attempt": repairs, "budget": spec.max_repairs_per_step}
            elif play.replan_requested and play.left_scene is not None:
                scene_replans += 1
                reason, lines = play.stopped, recent_action_lines(play.turns)
                transition = {"trigger": "scene", "attempt": scene_replans, "budget": spec.max_scene_replans_per_step}
            elif play.replan_requested:
                replans += 1
                reason, lines = play.stopped, recent_action_lines(play.turns)
                transition = {"trigger": "replan", "attempt": replans, "budget": spec.max_replans_per_step}
            elif (
                play.stopped == "done"
                and spec.coverage_check == "on"
                and coverages < spec.max_coverage_rewrites
                and verdict_step(scenario, step)
            ):
                joined = _with_precondition(_joined_rewrites(plays), checked_precondition)
                missing, verdict = await self._missing_results(executer, scenario, step, joined)
                if not missing:
                    joined.judged = verdict
                    return joined
                coverages += 1
                reason = (
                    "done() held, but the record shows nothing for these expected results yet, so no verdict can pass: "
                    + "; ".join(missing)
                    + ". Plan the moves that show each of them; done() reads the state after the last of those moves."
                )
                lines = recent_action_lines(play.turns)
                transition = {"trigger": "coverage", "attempt": coverages, "budget": spec.max_coverage_rewrites, "missing": list(missing)}
            else:
                return _with_precondition(_joined_rewrites(plays), checked_precondition)
            # 자동 복구의 전이마다 이유·소비 예산·쓴 상태 버전을 남긴다(개선 계획 묶음 2).
            transition |= {
                "stopped": clip(play.stopped, MAX_REFUSAL_CHARS),
                "state_version": play.end_state_version,
                "seconds_left": round(max(spec.step_budget_seconds - (executer.now() - started), 0.0), 1),
            }
            async with self._game_paused(executer.channel, step):
                written = await planner.replan(scenario, step, reason, lines, transition)
            if written.macros is None:
                plays[-1].stopped = (
                    f"PLANNER-REFUSED: the rewritten macros were still refused after {spec.max_rewrites} rewrites "
                    f"(asked because: {clip(play.stopped, MAX_REFUSAL_CHARS)})"
                )
                return _with_precondition(_joined_rewrites(plays), checked_precondition)
            macros = written.macros
            checked = not written.unchecked

    async def _missing_results(
        self, executer: Executer, scenario: QaScenario, step: int, play: StepPlay
    ) -> "tuple[tuple[str, ...], StepVerdict]":
        """판정 step 이 `done()` 으로 멈춘 자리에서 근거가 아직 없는 기대 항목(pe12). 반대로 나온 항목이 있으면 비어 있다 —
        그것은 더 돌아서 나올 결과가 아니라 판정이다. 게임을 멈춘 채 묻는다: 묻는 동안 화면이 다음 상태로 흘러가지 않게."""
        async with self._game_paused(executer.channel, step):
            image = (
                await capture_screen(executer.channel, f"Step {step}: capturing the screen to check the expected results", step)
                if self._spec.screen_images == "on"
                else None
            )
            verdict = await self._coverage_judge.judge(scenario, step, play, image=image)
        await note_decision(
            executer.channel, "coverage_check",
            f"Step {step}: done() held; expected results without evidence: {len(verdict.missing)}",
            step, missing=list(verdict.missing) or None, contradicted=list(verdict.contradicted) or None,
            checks=list(verdict.checks) or None,
        )
        return (() if verdict.contradicted else verdict.missing), verdict

    async def _start_refusal(self, executer: Executer, macros: StepMacros, step: int) -> str | None:
        """계획을 받은 지금 화면에서 끝 조건이 이미 참이면 그 까닭(planner 거절 문장), 아니면 `None`(pe11).

        동작이 있는 step 의 `done()`, 그리고 묶음의 뒤 step `done_<k>()` 를 읽는다. 앞의 것이 참이면 play 는 입력 하나 없이 끝나고,
        뒤의 것이 참이면 이 step 이 시작하자마자 그 step 의 결과로 끝난다. 예전에는 Jev 에게 목표 도달을 물었지만 답은 반반이었다
        (try 17·18·24)."""
        held = []
        if (macros.actions or macros.contracts) and await executer.holds(macros.probe, step):
            held.append("`done()`")
        for number, (_goals, probe, _actions) in sorted(macros.segment.items()):
            if number > step and probe is not None and await executer.holds(probe, number):
                held.append(f"`done_{number}()`")
        if not held:
            return None
        return (
            f"{' and '.join(held)} already {'holds' if len(held) == 1 else 'hold'} on the screen this plan was written on, so "
            "play would stop before any move. When this step's result is already on the screen, write the step with no "
            "actions. Otherwise the predicate reads a state that is already there or comes back on its own: read what this "
            "step's own moves change. A later step's `done_` must be false where the step before it stops."
        )

    def _repair(self, scenario: QaScenario, step: int):
        """The editor for one step's play, or `None` when the run does not edit."""
        return self._editor.for_step(scenario, step) if self._editor is not None else None

    async def _analyse(
        self, scenario: QaScenario, step: int, play: StepPlay, message: str, evidence_sufficient: bool = True
    ) -> FailureReport | None:
        """The failure analysis of a judged step, or `None` when the run does not analyse. A verdict the evaluator
        could not ground in any game value is `INCONCLUSIVE` without asking the model (묶음 3): a missing observation
        says nothing about the game."""
        if self._analyzer is None:
            return None
        if not evidence_sufficient:
            return insufficient(message, play)
        case = scenario.steps[step - 1].case
        missing = self._unobservable.get(case.id) if case is not None else None
        if missing:
            return unobservable(missing, message, play)
        return await self._analyzer.analyze(scenario, step, play, message)

    def _engine_error(self, message: str, play: StepPlay | None = None) -> FailureReport | None:
        return engine_error(message, play) if self._analyzer is not None else None

    def _unreached(self, message: str, play: StepPlay | None = None) -> FailureReport | None:
        return unreached_report(message, play) if self._analyzer is not None else None

    async def _run_free(
        self,
        channel: QaRunChannel,
        scenario: QaScenario,
        state: QaRunState,
        planner: Planner,
        executer: Executer,
        scene_judge: SceneEvaluator,
    ) -> None:
        """Plays a FREE scenario scene by scene, round after round, until every case is reported.

        `tried` holds the cases offered to a plan this round, or skipped because play could not
        reach their scene. A round goes on while the scene on screen has untried cases — plan after
        plan, `free_batch_size` cases at a time — or another scene with untried cases can be reached.
        """
        spec = self._spec
        pending = {case.step: case for case in free_cases(scenario)}
        context = channel.scene.scene_context
        known = {scene.scene_name.strip().casefold() for scene in (context.scenes if context else []) if scene.scene_name}
        resets = 0

        async def settle(
            step: int, passed: bool, message: str, analysis: FailureReport | None = None, play: StepPlay | None = None
        ) -> None:
            await self._send_verdict(channel, state, scenario, step, passed, message, analysis, play)
            del pending[step]

        rounds = 0
        while pending:
            rounds += 1
            await note_decision(
                channel,
                "free_round",
                f"FREE round {rounds}: {len(pending)} cases left, starting in {channel.scene.pulse.scene or '?'}",
                scene=channel.scene.pulse.scene,
                round=rounds,
                resets=resets,
                pending=list(pending),
            )
            tried: set[int] = set()
            while pending:
                untried = [case for case in pending.values() if case.step not in tried]
                offered = candidates(untried, channel.scene.pulse.scene, known, spec.free_batch_size)
                if offered:
                    tried.update(case.step for case in offered)
                    unplayed = await self._play_scene(
                        channel, scenario, planner, executer, scene_judge, offered, pending, resets, settle
                    )
                    # Cases the plan meant to verify but play left the scene before: offered again,
                    # in that scene, without spending a play.
                    tried.difference_update(unplayed)
                    continue
                target = next_scene(untried, channel.scene.pulse.scene, known)
                if target is None:
                    break
                if not await self._travel(channel, scenario, planner, executer, target, pending):
                    tried.update(case.step for case in untried if same_scene(case.scene, target.scene))
            if not pending:
                return
            if resets >= spec.free_max_resets:
                left = list(pending.values())
                causes: dict[str, int] = {}
                for case in left:
                    causes[case.cause] = causes.get(case.cause, 0) + 1
                await note_decision(
                    channel,
                    "free_end",
                    f"FREE ends after {resets} resets: {len(left)} cases unreached ("
                    + ", ".join(f"{cause} {count}" for cause, count in sorted(causes.items()))
                    + ")",
                    resets=resets,
                    causes=causes,
                    unreached={str(case.step): case.cause for case in left},
                )
                for case in left:
                    message = unreached(case.attempts, resets, case.cause, case.last_reason)
                    await settle(case.step, False, message, self._unreached(message))
                return
            await note_decision(
                channel,
                "reset",
                f"Resetting the game after round {rounds}: {len(pending)} cases left",
                round=rounds,
                pending=list(pending),
            )
            await reset_game(channel)
            resets += 1

    async def _play_scene(
        self,
        channel: QaRunChannel,
        scenario: QaScenario,
        planner: Planner,
        executer: Executer,
        scene_judge: SceneEvaluator,
        offered: list[FreeCase],
        pending: dict[int, FreeCase],
        resets: int,
        settle: Callable[..., Awaitable[None]],
    ) -> list[int]:
        """One plan: the planned cases played in order while the scene stays, then judged.

        Returns the planned cases left unplayed because the scene changed before their turn.
        """
        scene = channel.scene.pulse.scene
        plan = await planner.plan_scene(scenario, [case.step for case in offered])
        if plan.macros is None:
            # The plan never parsed. The scene's own cases fail as a refused step does; the cases
            # offered only because their scene is unknown wait for another scene.
            for case in offered:
                if same_scene(case.scene, scene):
                    message = (
                        f"PLANNER-REFUSED: the scene plan was still refused after {self._spec.max_rewrites} "
                        f"rewrites: {plan.refusal}"
                    )
                    await settle(case.step, False, message, self._engine_error(message))
            return []
        for step, reason in plan.later.items():
            pending[step].last_reason = f"the planner left it for later in {scene or 'an unknown scene'}: {reason}"
            pending[step].cause = "DEFERRED"
        first_screen = screen(channel.scene.pulse)
        played: list[tuple[int, StepPlay]] = []
        unplayed: list[int] = []
        for step in plan.verify:
            if unplayed or not (same_scene(channel.scene.pulse.scene, scene) or channel.scene.pulse.scene == scene):
                # A case before it left the scene; this one was planned for the scene it left.
                unplayed.append(step)
                pending[step].last_reason = f"play left {scene or 'the scene'} before the case's turn"
                pending[step].cause = "LEFT_SCENE"
                continue
            pending[step].attempts += 1
            case_id = scenario.steps[step - 1].case_id
            play = await executer.play(step, step_text(scenario.steps[step - 1]), plan.macros.for_case(case_id))
            played.append((step, play))
        if not played:
            return unplayed
        verdicts = await scene_judge.judge(scenario, played, first_screen)
        for step, play in played:
            passed, message = verdicts[step]
            case = pending[step]
            if passed or reached(play):
                analysis = None if passed else await self._analyse(scenario, step, play, message)
                await settle(step, passed, message, analysis, play)
            elif case.attempts >= self._spec.free_case_attempts:
                last = unreached(case.attempts, resets, "GOAL_UNREACHED", message)
                await settle(step, False, last, self._unreached(last, play), play)
            else:
                case.last_reason = message
                case.cause = "GOAL_UNREACHED"
        return unplayed

    async def _travel(
        self,
        channel: QaRunChannel,
        scenario: QaScenario,
        planner: Planner,
        executer: Executer,
        target: FreeCase,
        pending: dict[int, FreeCase],
    ) -> bool:
        """Move to `target`'s scene. `False`, with why on that scene's cases, when play does not get there."""
        scene = target.scene or ""
        origin = channel.scene.pulse.scene
        written = await planner.write_travel(scenario, target.step, scene)
        stopped = f"the move was still refused after {self._spec.max_rewrites} rewrites: {written.refusal}"
        if written.macros is not None:
            play = await executer.play(
                target.step, f"Go to the scene `{scene}`.", written.macros, self._repair(scenario, target.step)
            )
            stopped = play.stopped
        arrived = channel.scene.pulse.scene
        waiting = [case.step for case in pending.values() if same_scene(case.scene, scene)]
        if same_scene(scene, arrived):
            await note_decision(
                channel,
                "travel",
                f"Travel {origin or '?'} → {scene}: reached",
                target.step,
                origin=origin,
                target=scene,
                reached=True,
            )
            return True
        for case in pending.values():
            if same_scene(case.scene, scene):
                case.last_reason = f"play could not reach the scene {scene} ({stopped})"
                case.cause = "SCENE_UNREACHED"
        await note_decision(
            channel,
            "travel",
            f"Travel {origin or '?'} → {scene}: not reached, ended in {arrived or '?'} ({clip(stopped, 200)}); "
            f"{len(waiting)} cases of {scene} wait for the next round",
            target.step,
            origin=origin,
            target=scene,
            reached=False,
            ended_in=arrived,
            stopped=clip(stopped, MAX_REFUSAL_CHARS),
            deferred=waiting,
        )
        return False


def reached(play: StepPlay) -> bool:
    """Whether play got to the step's goal: `done()` held on some turn (`StepPlay.reached_goal`)."""
    return play.reached_goal()


# UNREACHED 보고의 첫 문장. 씬에 못 간 case 와 play 했는데 목표에 못 닿은 case 가 같은 문구였다(ARTEL-1186).
_UNREACHED_SAYS = {
    "GOAL_UNREACHED": "play never reached the case's goal",
    "SCENE_UNREACHED": "play could not reach the case's scene, so it was not played",
    "DEFERRED": "the planner kept leaving it for later, so it was not played",
    "LEFT_SCENE": "play left the scene before the case's turn, so it was not played",
    "NOT_PLAYED": "it was never offered to a plan, so it was not played",
}


def unreached(attempts: int, resets: int, cause: str, last: str) -> str:
    return (
        f"UNREACHED (도달 못함) {cause}: {_UNREACHED_SAYS.get(cause, cause)} — {attempts} "
        f"{'play' if attempts == 1 else 'plays'} and {resets} {'reset' if resets == 1 else 'resets'}. "
        f"Last: {last}"
    )


async def reset_game(channel: QaRunChannel) -> None:
    """Restart the game from its first scene before the next FREE round, and wait for a reading."""
    await channel.dispatch_actions(
        [reset_game_cheat()], "Resetting the game for the next round"
    )
    await channel.wait_for_reading(RESET_READING_SECONDS)


# planner 복구 요청에 싣는 turn 수. 실패가 처음 난 turn 과 같은 실패의 기록은 이 창 밖이어도 싣는다.
REPAIR_TURNS_SHOWN = 10
# planner 복구 요청에 싣는 developer macro 본문의 상한.
MAX_DEVELOPER_SOURCE_CHARS = 4_000


def repair_brief(play: StepPlay, developer: LoadedDeveloperMacros | None = None) -> tuple[str, list[str]]:
    """planner 에게 step 을 다시 써 달라고 할 때의 `(이유, turn 줄)`. 이유에는 막힌 action 과 그 실패들, 그리고 step 이
    시작한 뒤 게임 값이 어떻게 바뀌었는지를 싣는다 — "준비 없이 시전부터 골랐다", "적 HP 가 그대로다" 를 planner 가 볼 수
    있어야 action 하나가 아니라 step 을 고친다(개선 계획 묶음 2)."""
    from app.agents.evaluator.verdict import evidence_text

    request = play.repair_request or {}
    action = request.get("action")
    if action is None or request.get("misses") is None:
        head = f"{request.get('reason')}."
        failed = [turn for turn in play.turns if turn.get("outcome") == "failed"]
    else:
        head = f"The action `{action}` failed {request.get('misses')} times in this step. Its last failure: {request.get('reason')}"
        failed = [turn for turn in play.turns if turn.get("picked") == action and turn.get("outcome") == "failed"]
    first_failure = [f"turn {turn.get('turn')}: {turn.get('picked')} failed ({turn.get('reason')})" for turn in failed[:1]]
    reason = (
        head
        + ("\nIts first failure: " + first_failure[0] if first_failure else "")
        + "\n"
        + evidence_text(play)
        + _hidden_text(play)
        + (
            "\nPaths in your ```observe block that matched no object while the step played, so the decision model saw "
            "nothing through them: " + ", ".join(play.observe_unknown)
            if play.observe_unknown
            else ""
        )
        + _unseen_text(play)
        + _developer_text(action, developer)
        + "\nA failure that is right — its precondition does not hold — is not the mistake; fix what makes the step stall."
    )
    return reason, recent_action_lines(play.turns[-REPAIR_TURNS_SHOWN:], REPAIR_TURNS_SHOWN)


def _joined_rewrites(plays: list[StepPlay]) -> StepPlay:
    """step 하나의 play 들을 한 기록으로(`merge_plays`). 둘 이상이면 다시 쓴 계획을 거쳤다는 것을 남긴다(pe11)."""
    joined = merge_plays(plays)
    joined.rewritten = len(plays) > 1
    return joined


def _unseen_text(play: StepPlay) -> str:
    """실패한 조건이 가리킨, 이 런에서 한 번도 보고되지 않은 객체. 조건이 틀렸을 수 있다는 것을 planner 가 알게 한다 — 실패
    문구("창이 열리지 않았다")만 보면 게임을 탓하고 같은 macro 를 다시 고른다(런 184)."""
    if not play.unseen_selectors:
        return ""
    return (
        "\nA failing condition waited on objects the game never reported in this run: "
        + ", ".join(play.unseen_selectors)
        + ". The game reports only objects with text, behaviour or watched values, so such a condition can never hold:"
        " the macro is wrong, not the game. Read the change from a value or an object that is reported instead."
    )


def _developer_text(action: str | None, developer: LoadedDeveloperMacros | None) -> str:
    """막힌 action 이 developer macro 면 그 본문. planner 는 평소 docstring 만 읽어, 본문의 조건이 틀렸는지 볼 수 없었다(런 184)."""
    if not action or not action.startswith(DEVELOPER_ACTION_PREFIX) or developer is None:
        return ""
    name = action.removeprefix(DEVELOPER_ACTION_PREFIX).split("#", 1)[0]
    usable = developer.usable.get(name)
    if usable is None:
        return ""
    return (
        f"\nThe source of the developer macro `{name}`:\n```python\n{clip(usable.macro.source, MAX_DEVELOPER_SOURCE_CHARS)}\n```\n"
        "When this source is what is wrong, replace it in the ```changes block with your own `def` that does its job"
        " correctly."
    )


def _hidden_text(play: StepPlay) -> str:
    """마지막으로 Jev 에게서 가려졌던 객체·static 묶음. observe 범위가 결과를 가렸는지 planner 가 보게 한다(런 169 step 4: 적과
    플레이어가 가려진 채 턴 종료만 되풀이했다). pe11 부터 turn 기록의 `hidden` 에 있고, 그 전 기록은 Jev state 안에 있다."""
    for turn in reversed(play.turns):
        state = turn.get("jev_state")
        hidden = turn.get("hidden") or (state.get("hidden") if isinstance(state, dict) else None)
        if hidden:
            objects = ", ".join(f"{name} ×{count}" for name, count in (hidden.get("objects") or {}).items())
            statics = ", ".join(hidden.get("statics") or [])
            parts = [part for part in (f"objects {objects}" if objects else "", f"static values {statics}" if statics else "") if part]
            return "\nHidden from the decision model by your ```observe block when play stopped: " + "; ".join(parts)
    return ""


# 앞선 step 판정 한 줄에 싣는 이유의 길이.
MAX_EARLIER_REASON_CHARS = 300


def case_precondition(scenario: QaScenario, step: int) -> str | None:
    """`step` 에서 TC 가 시작하면 그 TC 의 사전 조건, 아니면 `None`(pe16). 사전 조건은 TC 의 첫 step 을 하기 전에 성립해야 한다."""
    item = scenario.steps[step - 1]
    if item.case_id is None or item.case is None or not (item.case.precondition or "").strip():
        return None
    earlier = scenario.steps[step - 2].case_id if step > 1 else None
    return item.case.precondition.strip() if earlier != item.case_id else None


def _with_precondition(play: StepPlay, checked: dict | None) -> StepPlay:
    """play 에 사전 조건 대조 결과를 붙인다. 맞추려고 계획을 다시 썼으면 묶음의 뒤 step 계획은 버린다(`rewritten`)."""
    if checked is not None:
        play.precondition = checked
        if checked.get("rewritten"):
            play.rewritten = True
    return play


def verdict_step(scenario: QaScenario, step: int) -> bool:
    """`step` 이 case 의 판정 step 인가: case 의 마지막 step 이고 기대 결과가 있다(`this_step` 이 `verify` 를 싣는 step)."""
    item = scenario.steps[step - 1]
    if item.case_id is None or item.case is None or not item.case.expected:
        return False
    following = scenario.steps[step].case_id if step < len(scenario.steps) else None
    return following != item.case_id


def earlier_verdicts(scenario: QaScenario, state: QaRunState) -> list[str]:
    """이 런에서 앞선 step 들의 판정, 한 줄씩. evaluator 가 "앞 step 이 이미 이뤘다" 와 "한 번도 안 됐다" 를 가르게 한다 —
    런 171 step 5 는 step 4 가 1 스테이지를 이미 깼는데 그것을 몰라 시작부터 참인 값을 기본값으로 의심해 실패로 판정했다."""
    lines = []
    for result in state.step_results:
        text = step_text(scenario.steps[result.step - 1]) if 0 < result.step <= len(scenario.steps) else ""
        reason = result.message[:MAX_EARLIER_REASON_CHARS]
        lines.append(f"step {result.step} ({text}): {'PASSED' if result.passed else 'FAILED'} — {reason}")
    return lines
