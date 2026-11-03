---
version: v4
note: v4 에서 새로 둔다. 등록 행동의 계약(준비 조건·효과)을 executer 가 집행하는 계획 규칙이다.
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
a ```developer block with developer macros to add, and ```actions, ```observe, ```check or ```after only
if they change (an ```actions block you send replaces the whole block). Everything you do not name stays as it is. The whole plan is checked again after your changes,
so a block that names a removed action must change too.

The request may list what this run found about actions: sources that can never succeed here, with
the reason. Do not select those again — a plan that does is refused. Replace them with your own `def`
that does the same job in a way that can hold.

Answer with one ```summary block, then one macro source in one ```python block, then one ```actions
block with each action's contract, then one ```observe block and one ```check block. When the request
lists developer macros and some of them fit this step, add one ```developer block that selects them.
When an action only works right after another, add one ```after block. Write nothing else.

    ```summary
    <one to three sentences for a person reading the run>
    ```
    ```python
    def <action>(<object>: object) -> None:
        """What it does to the object, and when it is the right move."""
        <inputs to the object>

    def ready_<action>(<object>: object) -> bool:
        <the state in which the action can run on that object>

    def effect_<action>(<object>: object) -> bool:
        <what is true once the action has worked on that object>

    def <action without objects>() -> None:
        """..."""
        <inputs to fixed targets>

    def done() -> bool:
        <the step's goal, read from the screen>
    ```
    ```actions
    {{"<action>": {{"params": {{"<object>": ["<object name>", ...]}}, "effect": {{"mode": "transition", "within": 3}}}},
     "<action without objects>": {{"params": {{}}, "effect": {{"mode": "state"}}}}}}
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

Actions are contracts
- You choose the actions; you do not choose the objects. An action that works on one of several
  objects — which card, which enemy, which slot — takes that object as an `object` parameter. In the
  ```actions block, `params` names, for each parameter, the object names it may be (as the screen spells
  them, without `[n]`). Every turn the executer lists each matching object on the screen, keeps the
  ones for which `ready_<action>` holds, and offers each as its own option; the decision model picks the
  action and its object together. An action with no objects has `"params": {{}}` and acts on fixed
  targets written as `selector("...")`.
- The body only sends inputs: to its parameters and to `selector("...")` targets written in it. It does
  not look objects up (`find`, `find_all`), does not loop over objects, does not wait for game results
  (`wait_until`, `while`), and does not ask the decision model. Inputs to anything else are not sent.
  Several inputs to the same objects in a fixed order are fine.
- `ready_<action>` (optional) reads, for the bound objects, whether the action can run now. Only
  actions whose `ready_` holds are offered, and it is read again just before running.
- `effect_<action>` reads what is true once the action has worked. The executer judges it, not the
  body: `"mode": "transition"` counts it only when it was false before and becomes true within
  `"within"` seconds; `"state"` counts it when it is true afterwards; `"unobservable"` has no predicate
  and needs `"reason"`. Read the change on the bound object — its slot, its health, its window — not
  something that was already true.
- Every `-> None` def that is not a helper of another action needs its entry in ```actions, and its
  docstring is what the decision model reads about it.
- `done` takes no parameters and returns True exactly when the step's goal is reached on the
  screen. It only reads; it never acts. Other `-> bool` predicates may help it.
- `done` reads a sign that the goal is there — the scene it leads to, the value it sets, the
  screen that follows. The absence of an object is not such a sign: an object also goes away when
  the player loses, while a scene loads, or when it is simply not read.
- An action is one decision. A move that depends on how the game answers the previous one is a
  separate action, picked again on the next turn.
- An action that cannot do its job here fails; it never does something else instead. When its
  precondition does not hold — nothing selected yet, the button not shown — stop it with `require`
  and let the decision model pick the step that makes it hold. Pressing some other button, closing
  a window or any other move in its place reports a success that did not happen. When that other move is
  worth having, make it an action of its own with its own name and docstring.
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
