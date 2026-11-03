"""The planner–executer engine: a second way to run a QA scenario.

The QA agent in `app/agents/qa/` is one model in a tool loop that decides every input itself.
This engine splits that work into three parts:

* planner (`planner.py`) — the run's model writes each step as one macro source: actions, each
  with a docstring saying what it does and when it is the right move, and a `done()` predicate;
* executer (`executer.py`) — plays the step without the model: Jev confirms `done()` and picks
  the action to run, and the macro runner runs it;
* evaluator (`app/agents/evaluator/`) — the run's model judges the step once play stops. It is a
  module of its own, apart from the engine, so that what judges a play is not what played it.

The macro language — its parser and its runner, `app/agents/qa/macro/` — is used as it is. The
loop, the questions put to Jev and when play stops are this package's own.

A run picks its engine with `QaArchSpec.engine`; `agent` stays the default, and nothing here
changes what a run that does not ask for this engine does.
"""
