"""Runs one scenario on the planner–executer engine, in the place `QaRunner` takes for the agent.

`QaExecutionService` calls `run_with_deadline(channel, scenario)` on whichever runner the run's
engine names and reads back the state and, when the scenario did not close itself, why.

Each step: the planner writes the step's macros, the executer plays them with Jev, the evaluator
judges the step and sends the verdict the way the agent's `report_step` does. A step whose macros
are still refused after the planner's rewrites fails without being played. The closing `STATUS`
carries `QaRunState.build_summary`, so Orchestration grades both engines' runs the same way.
"""

import asyncio

from app.agents.planner_executer import evaluator
from app.agents.planner_executer.evaluator import Evaluator, Judge
from app.agents.planner_executer.executer import Executer
from app.agents.planner_executer.macros import RegisteredMacros
from app.agents.planner_executer.planner import Ask, Planner
from app.agents.qa.runner import _step_plan
from app.agents.qa.tools import QaRunState
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import MessageType, RunResult, StatusPayload, StepStatus
from app.qa.jev import JevClient
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

# How long the run waits for a first reading before it writes from an empty screen.
FIRST_READING_SECONDS = 5.0


class PlannerExecuterRunner:
    def __init__(
        self,
        config: RunConfig,
        ask: Ask | None = None,
        judge: Judge | None = None,
        jev: JevClient | None = None,
    ) -> None:
        """`ask` (planner), `judge` (evaluator) and `jev` are replaced by tests; a run uses the run's
        model for the first two and `app/qa/jev.py`'s client for Jev."""
        self._config = config
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
            step_texts=[step.action for step in scenario.steps],
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
        planner = Planner(self._config, channel, self._spec, RegisteredMacros(channel), ask=self._ask)
        executer = Executer(channel, self._spec, jev)
        judge = Evaluator(self._config, self._judge)
        for step in range(1, len(scenario.steps) + 1):
            written = await planner.write(scenario, step)
            if written.macros is None:
                await evaluator.report(
                    channel, state, step, False,
                    f"PLANNER-REFUSED: the step's macros were still refused after "
                    f"{self._spec.max_rewrites} rewrites: {written.refusal}",
                )
                continue
            play = await executer.play(step, scenario.steps[step - 1].action, written.macros)
            passed, message = await judge.judge(scenario, step, play)
            await evaluator.report(channel, state, step, passed, message)
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
