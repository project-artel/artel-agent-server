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
from collections.abc import Awaitable, Callable

from app.agents.evaluator import (
    Evaluator,
    FailureAnalyzer,
    FailureReport,
    Judge,
    RunReviewer,
    SceneEvaluator,
    engine_error,
    send_verdict,
    unreached as unreached_report,
)
from app.agents.planner_executer.decisions import MAX_REFUSAL_CHARS, clip, note_decision
from app.agents.planner_executer.developer_macros import NO_DEVELOPER_MACROS, LoadedDeveloperMacros
from app.agents.planner_executer.executer import Executer, StepPlay
from app.agents.planner_executer.free import FreeCase, candidates, free_cases, next_scene, same_scene
from app.agents.planner_executer.macros import RegisteredMacros
from app.agents.planner_executer.planner import Ask, Planner
from app.agents.planner_executer.prompts import screen, step_text
from app.agents.qa.runner import _step_plan
from app.agents.qa.tools import QaRunState
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import JsonRpcAction, MessageType, RunResult, StatusPayload, StepStatus
from app.qa.jev import JevClient
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

# How long the run waits for a first reading before it writes from an empty screen.
FIRST_READING_SECONDS = 5.0
# How long a FREE round waits for the game's first reading after `reset_game`.
RESET_READING_SECONDS = 10.0


class PlannerExecuterRunner:
    def __init__(
        self,
        config: RunConfig,
        ask: Ask | None = None,
        judge: Judge | None = None,
        jev: JevClient | None = None,
        developer: LoadedDeveloperMacros = NO_DEVELOPER_MACROS,
    ) -> None:
        """`ask` (planner), `judge` (evaluator) and `jev` are replaced by tests; a run uses the run's
        model for the first two and `app/qa/jev.py`'s client for Jev. `developer` 는
        `QaExecutionService` 가 런 시작에 한 번 읽고 파싱해 시나리오마다 같은 것을 넘긴다."""
        self._config = config
        self._developer = developer
        assert config.arch.planner is not None, "resolve_arch fills `planner` for this engine"
        self._spec = config.arch.planner
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
        planner = Planner(
            self._config, channel, self._spec, RegisteredMacros(channel), ask=self._ask, developer=self._developer
        )
        executer = Executer(channel, self._spec, jev)
        judge = Evaluator(self._config, self._judge)
        # 실패 분석과 런 리뷰(ARTEL-1267). `analysis="off"` 면 둘 다 없고 `STATUS` 는 pe2 그대로다.
        analysing = self._spec.analysis == "on"
        self._analyzer = FailureAnalyzer(self._config, self._judge) if analysing else None
        self._reviewer = RunReviewer(self._config, self._judge) if analysing else None
        self._reports: dict[int, FailureReport] = {}

        if scenario.execution_mode == "FREE":
            await self._run_free(channel, scenario, state, planner, executer, SceneEvaluator(self._config, self._judge))
        else:
            for step in range(1, len(scenario.steps) + 1):
                written = await planner.write(scenario, step)
                if written.macros is None:
                    message = (
                        f"PLANNER-REFUSED: the step's macros were still refused after "
                        f"{self._spec.max_rewrites} rewrites: {written.refusal}"
                    )
                    await self._send_verdict(channel, state, scenario, step, False, message, self._engine_error(message))
                    continue
                play = await executer.play(step, step_text(scenario.steps[step - 1]), written.macros)
                passed, message = await judge.judge(scenario, step, play)
                analysis = None if passed else await self._analyse(scenario, step, play, message)
                await self._send_verdict(channel, state, scenario, step, passed, message, analysis, play)
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
    ) -> None:
        """One step's verdict and, for a failed step, its analysis — kept for the run review too.

        `play` is the play the verdict is about; its frames go out as `played`. A step that was never
        played (refused macros, an unreached FREE case) passes none.
        """
        if analysis is not None:
            self._reports[step] = analysis
        await send_verdict(
            channel, state, step, passed, message, analysis=analysis, played=play.played() if play else None
        )

    async def _analyse(
        self, scenario: QaScenario, step: int, play: StepPlay, message: str
    ) -> FailureReport | None:
        """The failure analysis of a judged step, or `None` when the run does not analyse."""
        if self._analyzer is None:
            return None
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
            play = await executer.play(target.step, f"Go to the scene `{scene}`.", written.macros)
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
        [JsonRpcAction(id=1, method="reset_game", params=[])], "Resetting the game for the next round"
    )
    await channel.wait_for_reading(RESET_READING_SECONDS)
