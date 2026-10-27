"""The Evaluator: judges what a QA engine played, apart from the engine that played it.

A QA run is two modules. The engine plays the scenario — today the planner–executer engine in
`app/agents/planner_executer/`, which hands each step's play over as a `StepPlay`. This package
reads that play and answers for it:

* `verdict.py` — did the step (or, for a FREE scenario, each case of a scene) pass. The run's
  model judges from the turns played and the screens before and after; the engine's own claim
  that it reached the goal is evidence, not the answer.
* `analysis.py` — for a step that failed: which kind of failure it was (`GAME_DEFECT`,
  `UNREACHED`, `INCONCLUSIVE`, `AGENT_ERROR`), the cause, how to see it again from the inputs
  the play really sent, and what to look at. A step the engine stopped on its own is filled in
  by code, without a model call.
* `review.py` — once every step is judged, what the run says about the scenario as a whole:
  an overview, the patterns across steps, and what to do first.
* `send.py` — `send_verdict` puts the verdict and the analysis on the wire as the frames
  Orchestration already records (`STATUS`, and `ISSUE` for a game defect). It builds frames and
  sends them; it decides nothing.

The engine keeps what it needs to drive itself — whether `done()` held, when to stop, what to
retry — and this package owns what leaves the process as a judgement. Only the planner–executer
engine feeds it today; the tool loop's `report_step` still judges for itself.
"""

from app.agents.evaluator.analysis import (
    FailureAnalysis,
    FailureAnalyzer,
    FailureCategory,
    FailureReport,
    analysis_request,
    engine_error,
    sent_inputs,
    unreached,
)
from app.agents.evaluator.send import expected_text, send_verdict
from app.agents.evaluator.review import RunReview, RunReviewer, review_request
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
    "FailureAnalysis",
    "FailureAnalyzer",
    "FailureCategory",
    "FailureReport",
    "Judge",
    "RunReview",
    "RunReviewer",
    "SceneEvaluator",
    "SceneVerdicts",
    "Verdict",
    "analysis_request",
    "engine_error",
    "expected_text",
    "send_verdict",
    "request",
    "review_request",
    "scene_request",
    "sent_inputs",
    "unreached",
]
