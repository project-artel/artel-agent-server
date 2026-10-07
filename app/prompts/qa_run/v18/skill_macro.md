---
version: v18
note: macro 문법과 수명주기와 예시 둘. tool_write_macro·tool_read_macro·tool_edit_macro·tool_register_macro·tool_run_macro 다섯이 500자 상한 안에 들어가려면 이 글이 설명 밖에 있어야 한다. tests/test_qa_macro_reference.py 가 문법 절을 app/agents/qa/macro/reference.py 가 grammar.py 로 조립한 글과 맞추고, 예시 둘을 parser 에 넣는다.
placeholders: []
---
# Macros — a named sequence of actions you can call again

A macro is written as Python-looking text, but it is NOT executed as Python:
it is read with `ast.parse` and anything outside the list below is refused when
you store it, with a refusal that names what is accepted instead.

**The shape.** One `def` named exactly like the macro is its entry point. Any
other `def` in the same source is a helper it may call. Every parameter and
every assignment carries a type, and the types are: int, float, string, bool, object.
Every `def` returns `-> None`, because a macro hands back no value.

**The six statements.**
- an action: `click`, `double_click`, `drag`, `move_pointer`, `press_key`, `enter_text`, `hold_mouse_button`, `release_mouse_button`, `hold_key`, `release_key`, `set_input_axis`, `set_input_button`, `pause_game_time`, `resume_game_time`, or a helper `def` from this same source
- `require(<condition>, "<remedy>")` — both arguments are required. The remedy
  is what the agent reading the failure is told to do about it
- a typed assignment: `card: object = find(label="shoot")`
- `if` / `elif` / `else`
- `flag("<message>")` — tells you what the macro saw, and nothing more
- `ask_verdict("<expected>")` — says the step `run_macro` was called with needs
  judging. There is no step number to write: it is always that step

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

**Conditions.** One condition is ONE comparison (`==`, `!=`, `>`, `<`, `>=`,
`<=`) or ONE reader call. `and`, `or` and `not` are refused — write two
`require` calls, so each says on its own what to do when it fails. The readers
are: `scene()`, `observable()`, `static()`, `member()`, `actionable()`, `text()`, `exists()`, `absent()`. A condition written without a comparison has to be a bool, so
only `actionable()`, `exists()` and `absent()` stand alone.

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

**Refused outright:** `for`, `while`, `and`, `or`, `not`, a dot (`card.id`), a
nested `def`, `import`, `lambda`, an f-string, `return`, a docstring,
re-assignment and arithmetic. And these, each for its own reason:

- `click_button` is deprecated — it invokes a Button's `onClick` directly, so it sees no occlusion. Write `click` instead
- `reset_game` is not allowed in a macro — a stored script must not be able to throw the run's progress away. Call the tool yourself when a step needs it
- `report_step` is not part of the macro grammar. A verdict needs the scenario, the earlier steps, what the operator said and the knowledge this run read, and a macro holds none of those — use `ask_verdict(<expected>)` to say this step needs judging, and judge it yourself
- `report_case` is not part of the macro grammar: a test case's verdict is derived from its steps' verdicts, so there is nothing for a macro to report
- `report_issue` is not part of the macro grammar. File the defect yourself after the macro returns; `flag(<message>)` is how a macro tells you it saw something

**What you hold, you release.** `hold_key` / `release_key`, `hold_mouse_button` / `release_mouse_button`, `pause_game_time` / `resume_game_time` are counted along every path through
the macro, and a macro that ends any path still holding one is refused when you
register it. That includes holding inside one branch of an `if` and releasing
outside it: hold and release in the same branch, or in neither. Nothing releases
these for you, and a key left down changes every step after the macro.

**The limits, checked when you register rather than while it runs:** at most
128 statements along the longest path, and a call chain at most
3 deep counting the entry point. Caught at registration because
an action already sent to the game cannot be taken back.

**A caution about `==` on a number.** It is allowed, and on a continuous value
— a position, a timer — it is treacherous. The SDK rounds a number to four
decimals before sending it, so a value sitting still still wobbles in its last
place. The idiom is two `require` calls with `<` and `>` around the value you
expect, rather than one with `==`. This is advice, not a refusal.

## The lifecycle — write, read, edit, register, run

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
The draft is parsed again and refused if anything in it is not allowed. A registered
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
your own `report_step`.

## Two macros that pass, with the reasoning written into them

What these do that a first draft usually does not:

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

A macro that names the screen, opens a panel only when it is shut, runs the
procedure, and asks for a verdict on the step that called it:

```python
def attack_first_enemy_with(card_label: string, element_label: string) -> None:
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
    # A helper keeps a named sub-procedure in one place. Its statements count toward
    # the same limit as the entry point's.
    button: object = find(name="ConfirmCombine")
    require(actionable(button), "Both slots need a card before Confirm turns on.")
    click(button)
```

A macro that types into a field and releases everything it held:

```python
def rename_the_save_slot(field_label: string, new_name: string) -> None:
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
