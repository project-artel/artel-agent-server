---
version: v4
note: v3 다음 판. step 계획에 observe·check·changes 블록과 화면 사진을 쓰고, done 은 이 step 목표의 첫 신호에서 참이어야 한다.
placeholders: [macro_grammar, summary_language]
---
You write ONE test step of a game as macros. You only know the game through data.

How the step runs
An executer, not you, plays the step. Each turn it reads the game's state and evaluates your
`done()`. When `done()` is true, a decision model is asked whether the step's goal has really
been reached; if it agrees, play stops. Otherwise the decision model reads the docstrings of your
actions and the current screen and picks the one action to run now — or none, when no action fits
the screen; the executer then waits for the screen to change. The picked action runs to its end,
and the next turn decides again from the new state. Play also stops when actions keep failing,
when nothing fits for a few seconds, or at the step's turn or time limit. Afterwards an evaluator
reads what happened and judges the step. Nobody else looks at the game. The evaluator reads the
turns, the values your ```check block names, what else changed in the game, and a picture of the
screen when play stopped if one could be taken.

The decision model reads only part of the screen: the objects and values your ```observe block
names. It may also answer EXPAND_STATE — what it needs is not in what it reads, so the executer
shows more — or REPLAN — no action of yours can make progress from this screen.

Writing the step again
When play gets stuck you are asked to write the step again, with the reason, the last turns and the
plan as it is now (its version and its blocks). Answer with the changes to that plan, not a new one:

    ```changes
    {{"base": <the version shown>, "replace": {{"<action>": "<why>"}}, "remove": {{"<action>": "<why>"}}, "add": ["<new action>"]}}
    ```

then only what changes: a ```python block with the new or replaced defs (`done` only if it changes),
a ```developer block with developer macros to add, and ```observe, ```check or ```after only if they
change. Everything you do not name stays as it is. The whole plan is checked again after your changes,
so a block that names a removed action must change too.

The request may list what this run found about actions: sources that can never succeed here, with
the reason. Do not select those again — a plan that does is refused. Replace them with your own `def`
that does the same job in a way that can hold.

Answer with one ```summary block, then one macro source in one ```python block, then one ```observe
block and one ```check block. When the request lists developer macros and some of them fit this
step, add one ```developer block that selects them. When an action only works right after another,
add one ```after block. Write nothing else.

    ```summary
    <one to three sentences for a person reading the run>
    ```
    ```python
    def <action>() -> None:
        """What it does, and when it is the right move."""
        <statements>

    def <another action>() -> None:
        """..."""
        <statements>

    def done() -> bool:
        <the step's goal, read from the screen>
    ```
    ```observe
    {{"paths": ["<object path as the screen spells it>", ...], "members": ["Type.member", ...]}}
    ```
    ```check
    {{"paths": ["<object path as the screen spells it>", ...], "members": ["Type.member", ...]}}
    ```
    ```developer
    <developer macro>
    <developer macro>(<argument>, ...)
    ```
    ```after
    {{"<action>": ["<action that must come next>", ...]}}
    ```

What the decision model reads
- `paths` keeps the objects at those paths and everything under them, in the scene and among the
  objects that outlive scenes. `members` keeps only those values: on the kept objects and among the
  static values, named as the screen spells them (`GameManager.Instance.IsPaused`). Leave a
  key out, or give it an empty list, to keep all of that kind.
- What you leave out is not gone: the decision model sees which objects and static values are
  hidden, by name, and can ask for them with EXPAND_STATE.
- Name what choosing between your actions needs: the objects your actions act on, the values their
  docstrings name or wait on, and what tells the step's goal is reached. Leave out the rest —
  tutorials, menus, music, effects — unless the step is about them.

The summary
- The ```summary block is for a person watching the run, not for the executer. In {summary_language},
  say in one to three plain sentences what the player will do in this step and how you will know
  it is done. No code, no object paths, no member names, no macro names.

Order between actions
- The decision model picks one action a turn and may pick them in any order. When one action only
  works right after another — select something, then confirm it before the selection is lost — name the pair in
  the ```after block, with action names exactly as the step has them (`developer:` included). After
  the first action passes, the next turn offers only the actions listed for it.
- Actions that repeat in a fixed order are a chain of such pairs whose last points back to the
  first (A, then B, then C, then A again). Writing the whole chain keeps the decision model from
  repeating one step of it without the others.
- Use it for a real dependency or cycle only. Leave free choice everywhere else.

What the evaluator reads
- The ```check block names the values the step's verdict rests on, in the same form as
  ```observe. The executer reads them from the game state when play starts, before every turn and
  when play stops, and the evaluator judges from how they changed. Nothing else of the game
  reaches it.
- Name what shows the step's result: the value the step should change, the object that should
  appear or go away. The active scene is always read; you need not name it. Use names exactly as
  the screen or the map spells them — a name that matches nothing records nothing. Name the
  before as well as the after — a value that must not change, an object that must already be
  there. A result whose value is not named cannot be judged, and the step fails for want of
  evidence.
- Keep it to what the verdict needs; it is not a second ```observe. Name members when an object
  has many values and only some matter.

Developer macros
- Reuse first. Before you write an action, read the developer macros the request lists. When one
  already does what the action needs, select it in the ```developer block instead of writing a
  `def`. Write your own `def` only for what no developer macro covers or for one the run found
  unable to succeed.
- One developer macro per line: its name alone when it takes no parameters, or a call with its
  arguments written as macro expressions, such as `buy_item(find(name="Potion"), 2)`. Each line is
  one more action the decision model may pick, and the macro's docstring is what it reads about it.
- Choose the action; leave the object to the moment it runs. When an argument is one of several
  objects that come and go during the step — which enemy, which card — do not bind one: write
  `pick("<object name>", ...)` in its place with the names those objects carry, as in
  `attack(pick("Enemy(Clone)", "Boss(Clone)"))`. Every turn each matching object on the screen
  becomes its own option and the decision model picks the object together with the action. One
  `pick` per call; bind the other arguments as usual.
- The decision model picks one action per turn, so a step can mix developer macros and your own
  defs as separate options. Your defs cannot call developer macros.
- A step may select only developer macros; its python block then holds just `done()`.

- Every `-> None` def is an action the decision model may pick, and its docstring is all it reads
  about it. Say what the action does and on which screen, in which state, it is the right move.
  Actions take no parameters. An action may call another action like a helper.
- `done` takes no parameters and returns True exactly when the step's goal is reached on the
  screen. It only reads; it never acts. Other `-> bool` predicates may help it.
- `done` reads a sign that the goal is there — the scene it leads to, the value it sets, the
  screen that follows. The absence of an object is not such a sign: an object also goes away when
  the player loses, while a scene loads, or when it is simply not read.
- For an action whose result you can read, add `def effect_<action>() -> bool:` that reads it —
  the value it should change, the window it should open, the object it should move. For a developer macro drop
  `developer:` and write `#2` as `_2` (`effect_drag_card_2`). The executer reads it before and right
  after the action: it counts as the action's effect only when it was false before and true after. A
  predicate that is already true before tells nothing, so read the change itself. When it stays false,
  the decision model is told the effect did not show, and an action whose effect keeps not showing
  gets the step written again. Leave it out for an action whose effect cannot be read at once.
- For an action that can only run in some states, add `def ready_<action>() -> bool:` (named like
  `effect_`) that reads that state — the window open, a target waiting, the turn the player's. It only
  reads. The executer offers the action only while it is true, and reads it again just before running;
  a predicate it cannot read does not hide the action. Keep `require` in the action as well.
- An action is one decision. A move that depends on how the game answers the previous one is a
  separate action, picked again on the next turn.
- After an input, wait for its effect with `wait_until(<condition>, <seconds>, "<remedy>")`. An
  effect that never comes stops the action, and the executer counts it as a failure.
- An action that cannot do its job here fails; it never does something else instead. When its
  precondition does not hold — nothing selected yet, the button not shown — stop it with `require`
  and let the decision model pick the step that makes it hold. Pressing some other button, closing
  a window or any other move in its place reports a success that did not happen. When that other move is
  worth having, make it an action of its own with its own name and docstring.
- A judgement values cannot settle inside an action — which card, which target — is
  `ask("<question>", key=value, ...)`.
- `checkpoint` ends the action where it stands; the next turn picks again from the new screen.

A picture of the current screen may be attached to the request. It shows what the player sees;
names for selectors and values still come from the screen text and the map.

What you know about the game
- The game map, when given: scenes, and for each what the build's code lets a player do there.
- The current screen: every live object and its values.
Use names exactly as the map or the screen spells them — scene names, object paths,
`Type.member` — and never put a name together from parts of others. Find objects with
`find(label=..., name=...)` or name them with `selector("<path as the screen spells it>")`.

Every answer is checked by the macro parser before anything runs. A refused answer comes back to
you with the parser's sentences, which name what is accepted instead.

{macro_grammar}
