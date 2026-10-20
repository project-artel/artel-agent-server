"""Runs one scenario on the planner–executer engine, in the place `QaRunner` takes for the agent.

`QaExecutionService` calls `run_with_deadline(channel, scenario)` on whichever runner the run's
engine names and reads back the state and, when the scenario did not close itself, why.

Each step: the planner writes the step's macros, the executer plays them with Jev, the evaluator
judges the step and sends the verdict the way the agent's `report_step` does. A step whose macros
are still refused after the planner's rewrites fails without being played. The closing `STATUS`
carries `QaRunState.build_summary`, so Orchestration grades both engines' runs the same way.

An ORDERED scenario plays its steps in order. A FREE scenario is a set of test cases, one per step,
in no order (ARTEL-1149): the next case is the one checked in the scene on screen (`free.pick_case`).
A case whose play never reached its goal and failed is put off, not reported. When a round ends with
cases put off, the game is reset and they are played again, up to `free_max_resets` resets and
`free_case_attempts` plays per case; what is still left is reported failed as UNREACHED. The branch
is per scenario: a session that mixes ORDERED and FREE scenarios is recorded as ORDERED.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.agents.planner_executer import evaluator
from app.agents.planner_executer.developer_macros import NO_DEVELOPER_MACROS, LoadedDeveloperMacros
from app.agents.planner_executer.evaluator import Evaluator, Judge
from app.agents.planner_executer.executer import Executer, StepPlay
from app.agents.planner_executer.free import FreeCase, free_cases, pick_case
from app.agents.planner_executer.macros import RegisteredMacros
from app.agents.planner_executer.planner import Ask, Planner
from app.agents.planner_executer.prompts import step_text
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


@dataclass(frozen=True)
class StepOutcome:
    """One step played and judged, before it is reported."""

    passed: bool
    message: str
    # `done()` held on some turn. A failed step that never reached its goal is put off in FREE.
    reached: bool
    # The planner's macros were refused and the step was not played.
    refused: bool = False


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

        async def play(step: int) -> StepOutcome:
            return await self._play_step(planner, executer, judge, scenario, step)

        if scenario.execution_mode == "FREE":
            await self._run_free(channel, scenario, state, play)
        else:
            for step in range(1, len(scenario.steps) + 1):
                outcome = await play(step)
                await evaluator.report(channel, state, step, outcome.passed, outcome.message)
        state.finished = True
        passed = all(result.passed for result in state.step_results)
        await channel.emit(
            MessageType.STATUS,
            StatusPayload(
                status=StepStatus.COMPLETED,
                result=RunResult.PASSED if passed else RunResult.FAILED,
                message=f"{sum(r.passed for r in state.step_results)}/{state.total_steps} steps passed.",
                summary=state.build_summary(),
            ),
        )

    async def _play_step(
        self, planner: Planner, executer: Executer, judge: Evaluator, scenario: QaScenario, step: int
    ) -> StepOutcome:
        written = await planner.write(scenario, step)
        if written.macros is None:
            return StepOutcome(
                passed=False,
                message=(
                    f"PLANNER-REFUSED: the step's macros were still refused after "
                    f"{self._spec.max_rewrites} rewrites: {written.refusal}"
                ),
                reached=False,
                refused=True,
            )
        play = await executer.play(step, step_text(scenario.steps[step - 1]), written.macros)
        passed, message = await judge.judge(scenario, step, play)
        return StepOutcome(passed=passed, message=message, reached=reached(play))

    async def _run_free(
        self,
        channel: QaRunChannel,
        scenario: QaScenario,
        state: QaRunState,
        play: Callable[[int], Awaitable[StepOutcome]],
    ) -> None:
        """Plays a FREE scenario's cases scene by scene, putting off the ones play never reached."""
        spec = self._spec
        pending = free_cases(scenario)
        resets = 0
        while pending:
            queue = list(pending)
            put_off: list[FreeCase] = []
            while queue:
                case = pick_case(queue, channel.scene.pulse.scene)
                queue.remove(case)
                case.attempts += 1
                outcome = await play(case.step)
                if outcome.passed or outcome.reached or outcome.refused or case.attempts >= spec.free_case_attempts:
                    message = outcome.message
                    if not (outcome.passed or outcome.reached or outcome.refused):
                        message = unreached(case.attempts, resets, outcome.message)
                    await evaluator.report(channel, state, case.step, outcome.passed, message)
                    continue
                case.last_reason = outcome.message
                put_off.append(case)
            if not put_off:
                return
            if resets >= spec.free_max_resets:
                for case in put_off:
                    await evaluator.report(
                        channel, state, case.step, False, unreached(case.attempts, resets, case.last_reason)
                    )
                return
            await reset_game(channel)
            resets += 1
            pending = put_off


def reached(play: StepPlay) -> bool:
    """Whether play got to the step's goal: `done()` held on some turn."""
    return play.stopped == "done" or any(turn.get("done") for turn in play.turns)


def unreached(attempts: int, resets: int, last: str) -> str:
    return (
        f"UNREACHED (도달 못함): play never reached the case's goal in {attempts} "
        f"{'play' if attempts == 1 else 'plays'} and {resets} {'reset' if resets == 1 else 'resets'}. "
        f"Last: {last}"
    )


async def reset_game(channel: QaRunChannel) -> None:
    """Restart the game from its first scene before the next FREE round, and wait for a reading."""
    await channel.dispatch_actions(
        [JsonRpcAction(id=1, method="reset_game", params=[])], "Resetting the game for the next round"
    )
    await channel.wait_for_reading(RESET_READING_SECONDS)
