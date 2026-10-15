"""The planner–executer engine: a second way to run a QA scenario.

The QA agent in `app/agents/qa/` is one model in a tool loop that decides every input itself.
This engine splits that work into three parts:

* planner (`planner.py`) — the run's model writes each step as a module: funcs that each answer
  one state of the game, with a Python `when` that reads it and a macro `run` with the inputs it
  takes, plus the step's `done`, `lost` and `check`;
* executer (`executer.py`) — plays those funcs turn by turn without calling the model: the one
  func whose `when` holds, or, when several do, the one a decision model (Jev) picks by their
  descriptions, asked the way a macro's `match decide(...)` asks;
* evaluator (`evaluator.py`) — judges the step with its `check` once play stops.

A run picks its engine with `QaArchSpec.engine`; `agent` stays the default, and nothing here
changes what a run that does not ask for this engine does.
"""
