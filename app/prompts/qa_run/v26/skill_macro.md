---
version: v26
note: macro 문법과 수명주기와 예시 둘. tool_write_macro·tool_read_macro·tool_edit_macro·tool_register_macro·tool_run_macro 다섯이 500자 상한 안에 들어가려면 이 글이 설명 밖에 있어야 한다. tests/test_qa_macro_reference.py 가 문법 절을 app/agents/qa/macro/reference.py 가 grammar.py 로 조립한 글과 맞추고, 예시 둘을 parser 에 넣는다. v20 (ARTEL-984): 진입점 def 의 docstring 이 summary 이고 register_macro 가 그것을 요구한다는 The docstring 절을 더했고, 예시 셋이 docstring 으로 시작한다. v21 (ARTEL-988): wait_until statement 와 -> bool predicate 를 문법 절(Waiting, Predicates, Conditions, 상한)에 더했고, clear_the_wave 예시가 predicate 로 or 를 적고 wait_until 로 승리 화면을 기다린다. v22: import, match decide, ask 와 Jev 가 읽는 context 를 문법에 더하고, 네 번째 예시로 앞의 두 예시를 import 해 전투를 한 턴씩 고르는 orchestration macro 를 실었다. v23 (ARTEL-1056): 상한 절의 호출 깊이를 3 에서 9 로 올렸다.
placeholders: []
description: When you repeat a sequence of actions — the same presses for every dialogue line, the same combine-and-attack for every enemy — write it once as a macro and run it instead of repeating it by hand. Load this skill before your first `write_macro`: it holds the grammar and three worked examples.
---
# Macros — a named sequence of actions you can call again

A macro is written as Python-looking text, but it is NOT executed as Python:
it is read with `ast.parse` and anything outside the list below is refused when
you store it, with a refusal that names what is accepted instead.

**The shape.** One `def` named exactly like the macro is its entry point. Any
other `def` in the same source is a helper it may call. Every parameter and
every assignment carries a type, and the types are: int, float, string, bool, object.
The entry `def` and its helpers return `-> None`: they act and hand back no
value. A `def` declared `-> bool` is a predicate, a condition you name — see
**Predicates** below. A macro can also call macros that are already
registered — see **Imports** below.

**The docstring.** The entry `def` opens with a docstring: a `"""..."""` string
as its first statement, saying what the macro does and when to use it. That
docstring is the macro's summary — later runs see it under the macro's name in
their macro list and in `search_macros` — so `register_macro` refuses an entry
`def` without one. A helper may have one too. A docstring is not a statement:
it does not count toward the limits and nothing runs it. A string anywhere else
is refused; write a `#` comment there.

**The statements.**
- an action: `click`, `double_click`, `drag`, `move_pointer`, `press_key`, `enter_text`, `hold_mouse_button`, `release_mouse_button`, `hold_key`, `release_key`, `set_input_axis`, `set_input_button`, `pause_game_time`, `resume_game_time`, a helper `def` from this same source, or a macro named
  in `from macros import ...`
- `require(<condition>, "<remedy>")` — both arguments are required. The remedy
  is what the agent reading the failure is told to do about it
- a typed assignment: `card: object = find(label="shoot")`
- `if` / `elif` / `else`
- `for <name> in find_all(...)` / `for <name> in range(n)`, and `while <condition>`
- `flag("<message>")` — tells you what the macro saw, and nothing more
- `ask_verdict("<expected>")` — says the step `run_macro` was called with needs
  judging. There is no step number to write: it is always that step
- `checkpoint("<what to look at>")` — hands the turn back to you mid-macro
- `wait_until(<condition>, <seconds>, "<remedy>")` — waits until the condition
  is true, for at most that many seconds
- `match decide("<question>", ...):` with one `case "<imported macro>":` per
  option — Jev picks the case that runs

**Aiming.** A target is `selector("<unity/hierarchy/path>")`, a parameter on the
`def` line, or a name bound by `find(...)` or `selector(...)`. There is no syntax
for a coordinate or a `#id`: both go stale between the run that writes the macro
and the run that calls it. `enter_text` takes a target here even though the tool
itself takes an id — the macro resolves the id for you.

**Finding things.** `find(label=..., name=..., under=...)` takes keywords only
and needs at least one of `label=` or `name=`. `label=` matches the text an
object is showing on screen; `name=` matches the last segment of its selector;
`under=` narrows by path prefix. A lookup that matches nothing, or more than one
thing, is reported where it happened and nothing is sent to the game.

**Binding a reading.** The right-hand side of an assignment is one of four
things: `find(...)`, `selector(...)`, one literal, or one reader call. A reader
call is read when that line runs and never again, so the name keeps the value
from before whatever the macro does next. That is how a macro compares before
and after an action: `before: float = member(enemy, "Enemy.Hp")`, then
`click(enemy)`, then `require(member(enemy, "Enemy.Hp") < before, ...)`. Declare
the name `int`, `float`, `string` or `bool`, never `object` — a reading is a
value, not something to aim at. `text()` and `scene()` always arrive as a string
and `actionable()`, `exists()` and `absent()` as a bool, so those are checked
when you store the macro. What `member()`, `observable()` and `static()` return
is up to the game, so a mismatch there stops the macro on the line that bound it.

**Loops.** Prefer `for` whenever you know what you are walking over: its end is
fixed when it starts. `for card in find_all(label=..., name=..., under=...)`
takes the same keywords as `find` and walks EVERY match, in the order the game
reported them — two cards with the same label, which `find` refuses as
ambiguous, are normal here, and no match at all is zero passes, not a failure.
The matches are taken once, when the loop starts; each pass gets one of them as
`card`, an `object`. `for i in range(n)` counts n passes, with `n` a whole
number or a name declared `int`, and `i` an `int`. `while <condition>` re-reads
its condition before every pass and is for waiting on a change you cannot count
— a health bar reaching zero, a dialogue running out. A loop's name, and every
name bound inside it, ends with the loop. `break`, `continue` and a loop's
`else` are refused: a pass runs its whole body; to stop early, use a `while`
whose condition turns false, and to skip part of a pass, put it under an `if`.

**Waiting.** `require` judges the screen as it stood right after the previous
action, so a loading screen or an animation that takes a few seconds fails it
although the game did nothing wrong. `wait_until(<condition>, <seconds>,
"<remedy>")` reads the condition at once and, while it is false, again every
time the game reports a new pulse, until it is true or the seconds run out. The
seconds are a number written in the macro, more than 0 and at most
30. Nothing is sent to the game while it waits.
While it waits, a lookup that matches nothing counts as "not yet" instead of
stopping the macro, so you can wait for something to appear. When the time runs
out the macro stops with `WAIT_TIMEOUT`, saying how long it waited and how many
pulses arrived; zero pulses means the screen never changed. Wait with this, not
with `while`: a `while` with no action in it does not wait for the game, it
re-reads the same screen until `LOOP_LIMIT`.

**Checkpoints.** `checkpoint("check the slot holds both cards")` ends the
macro's turn right there and hands you what it did so far, marked PAUSED — not
a failure. Look at the screen, then call `resume_macro` with `proceed: true` to
carry on from the next line, with every name it bound still bound, or
`proceed: false` to stop it there and send nothing more. Each checkpoint costs
you a turn, so place one only where a person would want to look: before
committing to something that cannot be undone, or after a step whose result no
`require` can describe.

**Imports.** `from macros import end_turn, cast_pair` at the top of the source,
before the first `def`, makes registered macros callable like helpers, each with
its own parameters. `macros` is the only module, and a name must be registered
in this run or an earlier one: an unknown name is refused when you store the
macro. An imported macro runs the version registered when you CALL it, so
fixing it with `edit_macro` and `register_macro` changes every macro that imports
it. Its own helpers stay its own, and its depth and length count toward this
macro's limits.

**Deciding.** `match decide("<question>", key=value, ...):` asks Jev, a decision
model, which `case` fits the game right now, and runs that case alone. Each
`case "<name>":` names an imported macro, and Jev reads that macro's docstring
as the description of the option — a vague docstring is an option Jev cannot
pick well. A case body is ordinary statements; it usually finds what it needs
and calls that macro. At least 2 cases; no `case _`, no guard.
Jev always has one more option, `none_fits`. When it picks that, the macro
PAUSES as at a checkpoint and shows you the question, the options and what Jev
read: play that part by hand and call `resume_macro` with `proceed: true` to go
on after the `match`, or stop it, write the macro that was missing, import it,
add its `case`, and run again. Every answer is listed under Decisions in the
result. When Jev cannot answer, the macro stops with `JEV_FAILED`.

**Asking.** `ask("<question>", key=value, ...)` is a condition Jev answers: true
when Jev puts yes at 0.5 or above. It is the whole condition of an
`if`, `elif`, `while` or `require`, and nothing else — not one side of a
comparison, not in `wait_until`, where it would ask Jev on every pulse, and not
in a predicate. Prefer a comparison or a predicate whenever the answer is a
value you can read: they cost nothing and never guess.

**What Jev reads.** The keywords of `decide` and `ask` are what Jev sees, under
the names you give them, together with the `scene`. An object name or
`selector(...)` gives that object's path, text and members; `find_all(...)`
gives every match; `under("<path>")` or `under(<object>)` gives that object and
every object below it, without naming them — a whole panel or hand at once; a
name bound to a reading gives its value; a literal gives itself. With no keyword, Jev reads every object on the screen. Each value is cut
at 80 characters and the whole at 6000;
objects past that are left out, and the decision line says how many. Give Jev
what a person would look at to answer the question, not everything. `scene` is
taken, so it cannot be a keyword.

**Conditions.** One condition is ONE comparison (`==`, `!=`, `>`, `<`, `>=`,
`<=`), ONE reader call, ONE predicate call, or ONE `ask(...)`. `and`, `or`
and `not` are refused
— for `and`, write two `require` calls, so each says on its own what to do when
it fails; for `or`, write a predicate. The readers are: `scene()`, `observable()`, `static()`, `member()`, `actionable()`, `text()`, `exists()`, `absent()`. A condition
written without a comparison has to be a bool, so only `actionable()`,
`exists()` and `absent()` stand alone.

**Predicates.** A `def` declared `-> bool` is a predicate. Its body holds only
typed assignments, `if` / `elif` / `else`, and `return`, and every path through
it ends in `return True`, `return False` or `return <condition>`. Call it
wherever a condition goes: `require`, `if`, `while`, `wait_until`, or another
predicate's `return`. That is how to write `or`: one `if` per alternative that
does `return True`, then `return False` at the end. A predicate sends nothing to
the game, so it cannot act, `require`, wait or `checkpoint`, and the entry `def`
cannot be one.

**What each shape of value can be compared with.** The shape is the shape the
value ARRIVES as, not the type you declared: `int` and `float` both arrive as a
number, because the SDK rounds before sending. Comparing two different shapes is
refused rather than being false forever.

- a number takes ==, !=, >, <, >=, <=
- a string takes ==, !=
- a bool takes ==, !=
- a vector2 takes no operator at all
- a vector3 takes no operator at all
- an object takes ==, !=
- any other shape takes no operator at all

An object's `==` is decided by which object it is, not by what it holds. Every
other shape — a sprite, an animator state, a number the game could not read —
refuses every operator rather than quietly answering false.

**Refused outright:** `and`, `or`, `not`, a dot (`card.id`), a nested `def`,
`import` other than `from macros import ...` at the top, `decide(...)` outside
`match`, `lambda`, an f-string, `return` outside a predicate, a string anywhere but the docstring,
re-assignment, arithmetic, `break`, `continue`, and a list or any other call after `for ... in`.
And these, each for its own reason:

- `click_button` is deprecated — it invokes a Button's `onClick` directly, so it sees no occlusion. Write `click` instead
- `reset_game` is not allowed in a macro — a stored script must not be able to throw the run's progress away. Call the tool yourself when a step needs it
- `report_step` is not part of the macro grammar. A verdict needs the scenario, the earlier steps, what the operator said and the knowledge this run read, and a macro holds none of those — use `ask_verdict(<expected>)` to say this step needs judging, and judge it yourself
- `report_case` is not part of the macro grammar: a test case's verdict is derived from its steps' verdicts, so there is nothing for a macro to report
- `report_issue` is not part of the macro grammar. File the defect yourself after the macro returns; `flag(<message>)` is how a macro tells you it saw something

**What you hold, you release.** `hold_key` / `release_key`, `hold_mouse_button` / `release_mouse_button`, `pause_game_time` / `resume_game_time` are counted along every path through
the macro, and a macro that ends any path still holding one is refused when you
register it. That includes holding inside one branch of an `if` and releasing
outside it: hold and release in the same branch, or in neither. Inside a loop,
hold and release in the same pass — a pass that presses without letting go
presses once more on every turn. A `checkpoint` may not sit between a hold and
its release, because the turn would end with the game held. A `wait_until` may
not sit between `pause_game_time` and `resume_game_time`: with game time frozen,
nothing it waits for can change. A `match` may not sit between a hold and its
release either, and each `case` releases what it holds, because `none_fits`
can skip every case. Nothing releases these for you, and a key left
down changes every step after the macro.

**The limits.** Checked when you register: at most 128 statements
written along the longest path, counting each loop body once, and a call chain
at most 9 deep counting the entry point and every imported macro. Checked while it runs,
because how often a loop turns is not known until then: a loop turns at most
50 times, and one call runs at most 1000
statements. Past either, the macro stops with `LOOP_LIMIT`, and what it already
sent stays sent. Each `run_macro` or `resume_macro` call waits at most
60 seconds across all its `wait_until` lines; past that
the macro stops with `WAIT_TIMEOUT`. A
step number in the result is the written line; `(pass N)` says which turn of
the loop around it.

**A caution about `==` on a number.** It is allowed, and on a continuous value
— a position, a timer — it is treacherous. The SDK rounds a number to four
decimals before sending it, so a value sitting still still wobbles in its last
place. The idiom is two `require` calls with `<` and `>` around the value you
expect, rather than one with `==`. This is advice, not a refusal.

## The lifecycle — write, read, edit, register, run, resume

**`write_macro` stores a draft and nothing else.** The draft is parsed and you are
told what it would do, or exactly what is wrong with it. Nothing is registered and
nothing reaches the game. Writing a name that already has a draft replaces that
draft outright; to change part of one, read it and use `edit_macro`.

**`read_macro` returns the draft if there is one, and the registered source
otherwise.** A name this run has never seen is looked up in the content map, so a
macro an EARLIER run registered comes back here too. The text comes back as it was
written, comments and blank lines included.

**`edit_macro` replaces text, and refuses a macro this run has not read.** Changing
text you have not seen is changing text you cannot check. `old_text` is matched as
plain text and has to appear EXACTLY ONCE; nothing happens if it appears zero times
or more than once, and you are told which. If the name has no draft but is already
registered, the registered macro is copied into a draft and the draft is changed —
the registered one is left exactly as it was, so a run calling it while you edit is
unaffected.

**`register_macro` is what makes a draft callable**, in this run and in later ones.
The draft is parsed again and refused if anything in it is not allowed, or if its
entry `def` has no docstring — that docstring is the summary later runs choose by.
A draft without one can be written and edited; add it with `edit_macro` before you
register. A registered
macro is written to the content map, and it is callable for the rest of THIS run
either way. When the answer says the write could not be CONFIRMED, registering again
changes nothing — it may well have been stored. When the answer names a reason it
was refused, that reason is the thing to fix.

A name already registered is updated in place, so the `screen` relations it carries
are kept rather than dropped and rebuilt. The macro is related to the `screen` you
are standing on, and `screens` lets you name more — by ID, the number in the
`content map: you are on screen <id>` line of your scene view, never a scene name.
`screens` ADDS; there is no way to take a relation away.

**`run_macro` calls a registered macro against the screen you are on now.**
`arguments` maps the entry `def`'s parameter names to values, and every parameter
without a default has to be there. An `object` parameter takes a selector; a
coordinate or a `#id` is refused before anything reaches the game. A draft is not
callable. When a name has both a draft and a registration, the REGISTERED one runs,
so editing never changes what a call does until you register the change.

What comes back says how far it got. The actions that reached the game are listed
apart from the ones that did not, and apart again from the ones an `if` skipped — an
action already sent cannot be taken back, so calling the macro again starts from a
board that is not where it started last time. Anything the macro flagged comes back
too, and so does any verdict it asked for. A flagged line is for you to read; a
verdict it asked for is about the step you called it with, and that step still needs
your own `report_step`. A request inside a loop stands once per pass; they are all
about that one step, so read them together and report it once.

**`resume_macro` answers a macro paused at a `checkpoint`.** The answer to `run_macro`
says PAUSED, what reached the game so far and what has not been sent yet. Nothing more
is sent while it waits. Look at the screen if you need to, then call `resume_macro`:
`proceed: true` carries it on from the line after the checkpoint, `proceed: false`
stops it there. Its `step` is the step the macro was called for. Until you answer,
`run_macro` is refused — two things driving the game at once would interleave their
actions. A run that ends with a macro still paused just drops it; a checkpoint never
sits between a hold and its release, so nothing is left held. A macro paused because
Jev found no fitting `case` is answered the same way: `proceed: true` goes on after
that `match`.

## Four macros that pass, with the reasoning written into them

What these do that a first draft usually does not:

- the entry `def` opens with a docstring that says what the macro does and when to
  use it, because that docstring is all a later run reads before choosing it
- the screen is named in the first statement, so a wrong-screen call fails on that
  line instead of on an aim ten lines down
- every `require` remedy says what to DO about the failure, because the agent
  reading it has only that sentence to go on
- the `if` opens the panel and the `require` AFTER it checks the panel is open, so
  the check holds whether the branch ran or not
- objects are found by the text they show, with `under=` narrowing where it could be
  ambiguous, so the macro is not tied to one hand or one layout
- a value read before an action is bound to a name and compared after it, so the
  check holds whatever the starting value was
- a number is bounded with `<` and `>`, never pinned with `==`
- what is held is released in the same body that held it
- a loop walks a `find_all` or a `range` whenever the count is knowable, and keeps
  `while` for repeating an action until something changes
- waiting on the game without acting is `wait_until`, never a `while` with no action
  in it, and an `or` is a predicate with one `if` per alternative
- a `checkpoint` sits only where a person would want to look, because each one costs
  a turn

A macro that names the screen, opens a panel only when it is shut, runs the
procedure, and asks for a verdict on the step that called it:

```python
def attack_first_enemy_with(card_label: string, element_label: string) -> None:
    """Combine two cards from the hand and attack the first enemy with the result.

    Use on the Battle scene when a step asks for one combined attack. Opens the
    combine panel if it is shut, and asks for a verdict on the calling step.
    """
    # Name the screen before touching anything. A macro called on the wrong screen
    # aims at objects that are not there, and every require after this one would
    # then blame the wrong thing.
    require(scene() == "Battle", "Go to the Battle scene before calling this.")

    # The combine panel may already be open from an earlier step, so open it only
    # when it is shut. `absent(...)` and not `not exists(...)`: there is no `not`.
    panel: object = selector("DebugCanvas[4]/CombinePanel[2]")
    if absent(panel):
        opener: object = find(name="CombineButton")
        require(actionable(opener), "Combine is locked this turn; end the turn first.")
        click(opener)

    # Check what the branch was for, outside the branch. This holds whether the panel
    # was already open or this macro just opened it.
    require(exists(panel), "The combine panel did not open; read the screen and retry.")

    # Found by the text they show, so this macro works for any pair the caller names
    # instead of for one hard-coded hand. `under=` keeps the lookup inside the hand,
    # where the same label on a tooltip cannot make it ambiguous.
    card: object = find(label=card_label, under="DebugCanvas[4]/Hand[1]")
    element: object = find(label=element_label, under="DebugCanvas[4]/Hand[1]")
    slot: object = find(name="CombineSlot")

    # The procedure. One statement is one batch, so a statement that fails stops the
    # macro there and everything below it is reported as never sent.
    drag(card, slot)
    drag(element, slot)
    confirm_combination()

    # Read the health BEFORE the attack and bind it. A reading is taken when its
    # line runs and never again, so `before` still holds the old value after the
    # click — that is what makes the comparison below mean "the attack did damage".
    enemy: object = find(name="Enemy")
    before: float = member(enemy, "Health.current")
    click(enemy)

    # Two bounds rather than one `==`. A number arrives rounded to four decimals, so
    # a value sitting still still wobbles in its last place.
    require(member(enemy, "Health.current") < before, "The attack landed but took no health.")
    require(member(enemy, "Health.current") > 0, "The enemy died outright; this step wanted chip damage.")

    # `flag` tells you what the macro saw, and nothing more.
    flag("combined the two cards and attacked the first enemy")

    # `ask_verdict` says the step this macro was called for now has something to
    # judge. It takes no step number: the step is the one `run_macro` was called
    # with, so the same macro asks about whichever step calls it. The macro judges
    # nothing — you still call `report_step` after it returns.
    ask_verdict("the enemy loses health equal to the combined card's power")


def confirm_combination() -> None:
    """Press Confirm on the combine panel once both slots are filled."""
    # A helper keeps a named sub-procedure in one place. Its statements count toward
    # the same limit as the entry point's.
    button: object = find(name="ConfirmCombine")
    require(actionable(button), "Both slots need a card before Confirm turns on.")
    click(button)
```

A macro that walks every matching card, stops for you to look, loops until the
enemy is gone, then waits for the wave to end:

```python
def clear_the_wave(card_label: string) -> None:
    """Play every card with this label, then attack until the enemy is gone.

    Use on the Battle scene to finish a wave. Pauses once so you can check the slot
    before anything is spent, waits for the wave to end, and dismisses the victory
    banner.
    """
    # Name the screen first, as every macro should.
    require(scene() == "Battle", "Go to the Battle scene before calling this.")

    # `for` over everything `find_all` matches. Two cards with the same label are
    # normal here — `find` would refuse them as ambiguous — and an empty hand is
    # zero passes, not a failure. The matches are taken once, when the loop starts.
    slot: object = find(name="CombineSlot")
    for card in find_all(label=card_label, under="DebugCanvas[4]/Hand[1]"):
        drag(card, slot)

    # Look before committing. The macro hands the turn back here: if the slot is
    # wrong, `resume_macro` with `proceed: false` stops it before anything is spent.
    checkpoint("check the slot holds every card you meant to play")

    # `while` re-reads its condition before every pass, and is for a change you
    # cannot count in advance. It stops with LOOP_LIMIT after 50 passes, so a
    # condition that never turns false cannot run forever.
    enemy: object = find(name="Enemy")
    while exists(enemy):
        # Bound inside the loop, so it is found afresh on every pass and ends with
        # the loop.
        attack: object = find(name="AttackButton")
        click(attack)

    # The banner slides in over a few seconds, so a `require` here would judge the
    # screen before it arrived. `wait_until` re-reads the condition on every new
    # pulse, for at most 10 seconds.
    wait_until(wave_is_over(), 10, "The wave did not end; observe whether an enemy is left.")

    # `range(n)` counts passes. The victory banner takes three presses to dismiss.
    for _ in range(3):
        press_key("Space", 0.1)

    flag("played every matching card and attacked until the enemy was gone")


def wave_is_over() -> bool:
    """The victory banner is up, or the game already moved on to the reward screen."""
    # `or` is written as a predicate: one `if` per alternative that returns True,
    # and the last line answers for the rest.
    if exists(selector("DebugCanvas[4]/VictoryBanner[0]")):
        return True
    return scene() == "Reward"
```

A macro that types into a field and releases everything it held:

```python
def rename_the_save_slot(field_label: string, new_name: string) -> None:
    """Type a new name into the save-slot field and save with Ctrl+S."""
    box: object = find(label=field_label)
    require(actionable(box), "The name field is not editable right now.")

    # `enter_text` takes a target here even though the tool itself takes an id: the
    # macro resolves the selector to the object and reads the id for you.
    enter_text(box, new_name)

    # What you hold, you release, on every path through the macro. Nothing releases
    # it for you, and a key left down changes every step after the macro returns.
    hold_key("LeftControl")
    press_key("S", 0.1)
    release_key("LeftControl")

    require(text(box) == new_name, "The field did not keep the text; check it is editable.")
```

A macro for a whole scene. When you enter a scene that already has macros for its
moves, write one like this: it imports those macros and lets Jev pick one per turn,
so the turns cost no model calls until none of them fits:

```python
from macros import attack_first_enemy_with, clear_the_wave

def play_the_battle() -> None:
    """Play the Battle scene turn by turn, letting Jev pick the move for each turn.

    Use on the Battle scene once its moves are registered as macros. Pauses when
    none of them fits, so you can play that turn and add the macro that was missing.
    """
    require(scene() == "Battle", "Go to the Battle scene before calling this.")
    player: object = find(name="Player")

    # `ask` is a condition Jev answers. It is here because "still going" has no
    # single value to compare; when one exists, compare it instead.
    while ask("Is the battle still going?", player=player):
        # The keywords are all Jev sees: the player's members, the health read just
        # now, and every card in the hand. Give it what a person would look at.
        health: float = member(player, "Health.current")
        match decide(
            "Which move fits this turn?",
            health=health,
            hand=find_all(name="Card", under="DebugCanvas[4]/Hand[1]"),
        ):
            # Each case names an imported macro, and Jev reads that macro's
            # docstring to choose it. When none fits, the macro pauses for you.
            case "attack_first_enemy_with":
                attack_first_enemy_with("Fire", "Shoot")
            case "clear_the_wave":
                clear_the_wave("Shoot")

    flag("the battle ended")
```
