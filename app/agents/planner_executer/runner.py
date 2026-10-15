"""Runs one scenario on the planner–executer engine, in the place `QaRunner` takes for the agent.

`QaExecutionService` calls `run_with_deadline(channel, scenario)` on whichever runner the run's
engine names and reads back the state and, when the scenario did not close itself, why.

Each step: the planner writes a step module, the executer plays it, the evaluator judges it with
its `check` and sends the verdict the way the agent's `report_step` does. A step whose module is
still refused after the planner's rewrites fails without being played. The closing `STATUS`
carries `QaRunState.build_summary`, so Orchestration grades both engines' runs the same way.
"""

import asyncio
from typing import Protocol

from app.agents.planner_executer import evaluator
from app.agents.planner_executer.executer import Executer
from app.agents.planner_executer.g import GameHandle
from app.agents.planner_executer.macro_host import MacroJev
from app.agents.planner_executer.planner import Planner, Written
from app.agents.planner_executer.runs import RegisteredMacros
from app.agents.qa.runner import _step_plan
from app.agents.qa.tools import QaRunState
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import LogCategory, MessageType, RunResult, StatusPayload, StepStatus
from app.qa.jev import JevClient
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

# How long the run waits for a first reading before it plays from an empty screen.
FIRST_READING_SECONDS = 5.0


class StepWriter(Protocol):
    """Where a step's module comes from: the planner in a run."""

    async def write(self, scenario: QaScenario, step: int, g: GameHandle) -> Written: ...


class PlannerExecuterRunner:
    def __init__(
        self,
        config: RunConfig,
        writer: StepWriter | None = None,
        jev: MacroJev | None = None,
    ) -> None:
        self._config = config
        self._writer = writer
        assert config.arch.planner is not None, "resolve_arch fills `planner` for this engine"
        self._spec = config.arch.planner
        # func 의 macro 가 묻는 `decide`·`ask` 의 client. tool loop 의 macro host 와 같은
        # `app/qa/jev.py` 의 `JevClient` 다. tests 가 바꾼다.
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
        owned_jev = JevClient() if self._jev is None else None
        g = GameHandle(channel)
        watcher = asyncio.create_task(g.watch())
        deadline = self._config.arch.deadline_seconds
        try:
            await asyncio.wait_for(
                self._run(channel, scenario, state, g, self._jev or owned_jev), timeout=deadline
            )
        except asyncio.TimeoutError:
            return state, f"The run exceeded its {int(deadline)}s limit."
        except QaCancelled:
            return state, None
        except Exception as error:  # noqa: BLE001 - the reason has to reach the timeline
            return state, f"The run stopped on an error: {type(error).__name__}: {error}"
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if owned_jev is not None:
                await owned_jev.aclose()
        return state, None

    async def _run(
        self,
        channel: QaRunChannel,
        scenario: QaScenario,
        state: QaRunState,
        g: GameHandle,
        jev: MacroJev | None,
    ) -> None:
        # planner 가 답을 검사할 때와 executer 가 func 를 돌릴 때 같은 등록된 macro 를 읽는다.
        registered = RegisteredMacros(channel)
        writer = self._writer or Planner(self._config, channel, self._spec, registered=registered)
        if not await g.fresh(timeout=FIRST_READING_SECONDS):
            await channel.note(
                f"No reading arrived in {FIRST_READING_SECONDS:g}s; writing from an empty screen.",
                LogCategory.SYSTEM,
            )
        executer = Executer(g, channel, self._spec, jev=jev, registered=registered)
        for step in range(1, len(scenario.steps) + 1):
            written = await writer.write(scenario, step, g)
            if written.problems:
                await evaluator.report(
                    channel, state, step, False,
                    "PLANNER-REFUSED: the step module was still refused after "
                    f"{self._spec.max_rewrites} rewrites: " + "; ".join(written.problems),
                )
                continue
            played = await executer.play(step, written.source)
            if played.rejected is not None or played.module is None:
                await evaluator.report(channel, state, step, False, played.rejected or played.stopped)
                continue
            passed, message = await evaluator.evaluate(g, played.module.check, played.stopped)
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
