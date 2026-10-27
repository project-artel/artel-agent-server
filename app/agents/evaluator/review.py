"""Run review: what one QA run says about the scenario as a whole, once every step is judged.

The per-step verdicts and failure analyses say what happened at each step. They do not say what
repeated — the same scene refusing input in four cases, every failure in one category, a
precondition no case could reach — and that is what a person reading the run wants first.
The review reads the run's own record (`QaRunState.step_results` and the `FailureReport` of
each failed step) and answers an overview, the patterns across steps, and what to do first.

It runs once, at the end of a run that closed itself. A run that was cancelled or ran out of
time is not reviewed: reading an interrupted run as a whole blames the game for the
interruption.
"""

import logging

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agents.evaluator.analysis import FailureReport
from app.agents.evaluator.verdict import ATTEMPTS, Judge, read_json
from app.agents.planner_executer.prompts import RUN_REVIEW_ROLE, prompt_text, step_text
from app.agents.qa.tools import QaRunState
from app.llm.chat_model import structured
from app.qa.channel import QaCancelled
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

# Items kept per list and characters kept per text. The terminal `STATUS` carries the whole
# summary of the run beside this, and the far side closes the socket over 1 MiB.
MAX_ITEMS = 10
MAX_ITEM_CHARS = 500
MAX_OVERVIEW_CHARS = 2_000

logger = logging.getLogger(__name__)


class RunReview(BaseModel):
    """What rides the terminal `STATUS.analysis` and, as `overview`, one `LOG` line."""

    overview: str
    patterns: list[str] = Field(default_factory=list)
    insights: list[str] = Field(default_factory=list)


class RunReviewer:
    def __init__(self, config: RunConfig, judge: Judge | None = None) -> None:
        self._config = config
        self._system: str | None = None
        self._judge = judge or self._ask_model

    async def review(
        self, scenario: QaScenario, state: QaRunState, reports: dict[int, FailureReport]
    ) -> RunReview | None:
        """The review, or `None` when two answers could not be read. A run without a review is
        still a run; the terminal `STATUS` then carries no `analysis` and nothing else moves."""
        if self._system is None:
            self._system = prompt_text(RUN_REVIEW_ROLE, self._config.prompt_version)
        user = review_request(scenario, state, reports)
        problem = ""
        for _ in range(ATTEMPTS):
            try:
                return _trim(_read(await self._judge(self._system, user)))
            except QaCancelled:
                raise
            except Exception as error:  # noqa: BLE001 - an unreadable answer is asked again
                problem = f"{type(error).__name__}: {str(error)[:200]}"
        logger.warning("[QA] run review dropped: no readable review in %d answers (%s)", ATTEMPTS, problem)
        return None

    async def _ask_model(self, system: str, user: str) -> RunReview:
        chain = structured(self._config.model, RunReview, self._config.reasoning)
        return await chain.ainvoke([SystemMessage(system), HumanMessage(user)])


def review_request(scenario: QaScenario, state: QaRunState, reports: dict[int, FailureReport]) -> str:
    """What the reviewer reads: the scenario, then every step's verdict and, when it failed, its
    analysis. Steps are listed in scenario order whatever order they were played in."""
    by_step = {result.step: result for result in state.step_results}
    lines = []
    for number, item in enumerate(scenario.steps, start=1):
        result = by_step.get(number)
        head = f"step {number}"
        if item.case_id is not None:
            head += f" (case {item.case_id}"
            if item.case and item.case.scene:
                head += f", scene {item.case.scene}"
            head += ")"
        head += f": {step_text(item)}"
        if item.case and item.case.expected and (item.case_id is None or _verifies(scenario, number)):
            head += f" → verify: {item.case.expected}"
        if result is None:
            lines.append(f"{head}\n  NOT REPORTED")
            continue
        lines.append(f"{head}\n  {'PASSED' if result.passed else 'FAILED'}: {result.message}")
        report = reports.get(number)
        if report is not None:
            lines.append(f"  analysis: {report.category.value} — {report.cause}")
            if report.insight:
                lines.append(f"  insight: {report.insight}")
    passed = sum(1 for result in state.step_results if result.passed)
    description = f"\n{scenario.description.strip()}" if getattr(scenario, "description", "") else ""
    return (
        f"Scenario: {scenario.title}{description}\n"
        f"Mode: {scenario.execution_mode}\n"
        f"Result: {passed} of {state.total_steps} steps passed.\n\n"
        "Steps:\n" + "\n".join(lines) + "\n"
    )


def _verifies(scenario: QaScenario, step: int) -> bool:
    """Whether `step` is the last step of its case, the one whose expected result is checked."""
    item = scenario.steps[step - 1]
    following = scenario.steps[step].case_id if step < len(scenario.steps) else None
    return following != item.case_id


def _read(answer) -> RunReview:
    if isinstance(answer, RunReview):
        return answer
    return RunReview.model_validate(read_json(answer))


def _trim(review: RunReview) -> RunReview:
    return RunReview(
        overview=_clip(review.overview, MAX_OVERVIEW_CHARS),
        patterns=[_clip(item, MAX_ITEM_CHARS) for item in review.patterns[:MAX_ITEMS]],
        insights=[_clip(item, MAX_ITEM_CHARS) for item in review.insights[:MAX_ITEMS]],
    )


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… (+{len(text) - limit} chars)"
