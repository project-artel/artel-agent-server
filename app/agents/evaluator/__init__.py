"""The Evaluator: judges what a QA engine played, apart from the engine that played it.

A QA run is two modules. The engine plays the scenario — today the planner–executer engine in
`app/agents/planner_executer/`, which hands each step's play over as a `StepPlay`. This package
reads that play and answers for it:

* `verdict.py` — did the step (or, for a FREE scenario, each case of a scene) pass. The run's
  model judges from the turns played and the screens before and after; the engine's own claim
  that it reached the goal is evidence, not the answer.
* `report.py` — sends the verdict the way the tool loop's `report_step` does, so Orchestration
  records every engine's steps in one shape.

The engine keeps what it needs to drive itself — whether `done()` held, when to stop, what to
retry — and this package owns what leaves the process as a judgement. Only the planner–executer
engine feeds it today; the tool loop's `report_step` still judges for itself.
"""

from app.agents.evaluator.report import report
from app.agents.evaluator.verdict import (
    ATTEMPTS,
    CASES_PER_JUDGEMENT,
    MAX_TURNS_SHOWN,
    CaseVerdict,
    Evaluator,
    Judge,
    SceneEvaluator,
    SceneVerdicts,
    Verdict,
    request,
    scene_request,
)

__all__ = [
    "ATTEMPTS",
    "CASES_PER_JUDGEMENT",
    "MAX_TURNS_SHOWN",
    "CaseVerdict",
    "Evaluator",
    "Judge",
    "SceneEvaluator",
    "SceneVerdicts",
    "Verdict",
    "report",
    "request",
    "scene_request",
]
