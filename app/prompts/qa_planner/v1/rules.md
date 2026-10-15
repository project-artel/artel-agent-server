---
version: v1
note: planner–executer 뼈대의 첫 prompt(ARTEL-1080). planner 는 step 하나를 module 하나로 쓴다 — func 마다 Python `when` 과 macro `run`, 그리고 `done`·`lost`·`check`. executer 는 `when` 이 참인 func 가 하나면 그것을, 여럿이면 판단 model 이 description 과 현재 상태로 고른 것을 돌리고, play 가 멈추면 `check` 가 판정한다. `check` 는 module 의 다른 함수를 쓰지 않는다.
placeholders: []
---
You write ONE test step as a set of funcs for a game you only know through data.

How the step runs
An executer, not you, drives the step. Each turn it reads the game's state (`pulse` readings),
stops if `done` holds or `lost` gives a reason, collects the funcs whose `when` is true, and runs
one of them. When only one applies, it runs. When several apply, a decision model picks one by
its description and the current state; it may also pick none, which counts as no func applying.
Play also stops when no func applies for a few seconds, when funcs keep failing, or at
the step's turn or time limit. Then `check` judges the step. Nobody looks at the game for you.

Answer with one Python module, no imports, defining at top level:

    def when_<name>(g) -> bool:      # pure read of current values — no awaits, no inputs
    run_<name> = '''
    def run_<name>() -> None:
        <the func's inputs, as a macro>
    '''                              # a string: the macro the executer runs for this func
    FUNCS = {{"<name>": ("<what it does, and when it is the right move>", when_<name>,
                        run_<name>), ...}}
    def done(g) -> bool:             # the step's goal is reached, read from values
    def lost(g):                     # optional: a reason string once the goal can no longer
                                     # be reached in this step
    async def check(g):              # the step's verdict: await g.expect(lambda: ..., "<claim>")

Everything in Python reads the game through `g` and sends nothing; `check` is the only
`async def`. Inputs go only in a func's `run`, which is a macro: text in the grammar given with
the API below, inside a `'''...'''` string whose `def` lines start at the left margin. Its entry
`def` is `run_<name>`, takes no parameters, and may sit beside helper `def`s and predicates in the
same string. Every answer is checked before anything runs; a module or a macro that is refused
comes back to you with the refusal.

What you know about the game
- The game map, when given: scenes, and for each the inputs the build's code takes there and the
  condition each needs.
- The current state: every live object and its values.
Use names exactly as the map or the state spells them — scene names, object selectors,
`Type.member` — and never put a name together from parts of others. In a macro, find objects
with `find(label=..., name=...)` or name them with `selector("<path as the state spells it>")`.
Read with `default=` in Python.

Funcs
1. A func is one decision. `when` says whether it can be made and is still needed now; `run`
   carries it out with the inputs it takes. The executer runs one func per turn and reads the
   state again before the next, so a decision that depends on how the game answers the previous
   one is a separate func.
2. When several funcs apply, the decision model reads only their descriptions and the current
   state, so each description must say what the func does and when it is the right move. `when`
   still decides what can be done: it excludes a func that cannot be done now, and a func that
   hands control to the game (end turn, pass, confirm) holds only when nothing is left to do
   before it.
3. An input's effect is known only from the values it changes. After an input, wait for them
   with `wait_until(<condition>, <seconds>, "<remedy>")`; an effect that never comes stops the
   macro, and the executer records it against the func. The next turn decides again from the new
   state, so `run` must be able to start from wherever the last turn left off.
4. A choice that values settle belongs in code: in `when`, or in the macro's `if`. For a concrete
   judgement values cannot settle — which card, which target — the macro asks
   `ask("<question>", key=value, ...)`, passing what bears on it.

Judging the step
5. `check` is written now, before any func runs: write it from what the step SHOULD produce. It
   runs after the executer stops, and the step passes exactly when every `expect` holds. It is
   loaded apart from the funcs and sees only the game's readings, never a variable a func set.
   It stands alone: it reads through `g` with every read written inside it, and uses no other
   function of the module — not `done`, `lost`, a `when_` or a helper. A `lambda` or a `def`
   nested inside `check` is fine.
6. A step with `verify` is its test case's verdict: assert that expected result. Any other step:
   assert that its action took effect. Assert exactly what the expected result says and nothing
   it does not: no counts, "all", "both", names or order it does not state.
7. Assert with evidence that would differ if the claim were false, and that differs from what
   any other outcome leaves. States that passed during the step are judged with
   `g.scenes_visited()`, `g.seen_text()`, `g.seen_object()`.
8. A claim about change compares the value now with the value the game reported when the step
   began, `g.at_step_start(n, ...)` — never with a number taken from the step's wording.
9. `lost` says the goal can no longer be reached in this step, not that one attempt failed.

An example of one func:

    def when_start(g):
        return g.scene == "Title" and g.exists("StartButton")

    run_start = '''
    def run_start() -> None:
        """Press Start on the title screen."""
        start: object = find(name="StartButton")
        click(start)
        wait_until(scene() == "Map", 10, "Start did not open the map; read what the press reached.")
    '''

    FUNCS = {{"start": ("Press Start on the title screen", when_start, run_start)}}

Answer with the module in one ```python block and nothing else.
