"""게임 개발자가 MCP `macro_grammar` 로 읽는 문법 글.

`reference.GRAMMAR` 는 QA agent 에게 쓴 글이다. `register_macro`·`run_macro`·`resume_macro` 처럼
개발자에게 없는 도구를 부르고, 게임 쪽에서 무엇이 보이는지는 말하지 않는다 — QA agent 는 그것을
화면으로 본다. 개발자는 게임 코드만 보고 쓰므로 그 둘이 거꾸로 필요하다. 그래서 글을 따로 둔다.

parser 와 어긋나지 않게 이름·인자·상한은 전부 `grammar.py` 의 표에서 뽑는다. 손으로 적는 것은
tool 마다의 한 줄 설명(`TOOL_GUIDE`)과 SDK 가 무엇을 보고하는지뿐이다. `TOOL_GUIDE` 가 `TOOLS` 와
어긋나면 import 시점에 멈춘다.

SDK 에 대한 문장은 `artel-sdk` develop(0.2.0)의 동작이다. 근거 파일은 각 절의 주석에 적는다.
"""

from app.agents.qa.macro.grammar import (
    DECLARABLE_TYPE_NAMES,
    FORBIDDEN_TOOLS,
    MAX_CALL_DEPTH,
    MAX_EXECUTED_STATEMENTS,
    MAX_LOOP_PASSES,
    MAX_STATEMENTS,
    MAX_WAIT_PER_CALL_SECONDS,
    MAX_WAIT_PER_STATEMENT_SECONDS,
    MIN_MATCH_CASES,
    NONE_FITS,
    PAIRED_TOOLS,
    READER_NAMES,
    TOOLS,
    MacroShape,
    ToolSpec,
    allowed_operators,
    operator_names,
)
from app.agents.qa.macro.reference import NUMBER_EQUALITY

# tool 마다 개발자에게 하는 한 줄. 인자 이름·순서·기본값은 `ToolSpec` 에서 뽑으므로 여기 적지 않는다.
TOOL_GUIDE: dict[str, str] = {
    "click": "move the pointer onto `target`, press and release `button`",
    "double_click": "the same as `click`, twice",
    "drag": (
        "press `button` on `from_target`, move onto `to_target`, release there. Order is "
        "from, then to"
    ),
    "move_pointer": "move the pointer onto `target` without pressing (hover)",
    "press_key": (
        "press `key_code` and release it after `duration_seconds` (more than 0, e.g. 0.1)"
    ),
    "enter_text": "type `value` into the input field `target`",
    "hold_mouse_button": "press `button` where the pointer is and keep it down",
    "release_mouse_button": "release `button`",
    "hold_key": "press `key_code` and keep it down — for modifiers such as Shift+click",
    "release_key": "release `key_code`",
    "set_input_axis": (
        "set an Input Manager axis (`Horizontal`, `Vertical`, ...) to `value` in -1..1. "
        "The axis must exist in the project's Input Manager"
    ),
    "set_input_button": "press (`True`) or release (`False`) an Input Manager button axis",
    "pause_game_time": "freeze game time",
    "resume_game_time": "unfreeze game time",
}

if set(TOOL_GUIDE) != {spec.name for spec in TOOLS}:
    # tool 이 늘거나 빠졌는데 이 표를 안 고치면 개발자 글에서 그 tool 이 조용히 빠지거나 없는 tool 을
    # 안내한다. import 시점에 알린다.
    raise RuntimeError(
        "TOOL_GUIDE must describe exactly the tools in grammar.TOOLS: "
        f"missing {sorted({spec.name for spec in TOOLS} - set(TOOL_GUIDE))}, "
        f"extra {sorted(set(TOOL_GUIDE) - {spec.name for spec in TOOLS})}."
    )


def tool_signature(spec: ToolSpec) -> str:
    """`drag(from_target: object, to_target: object, button: int = 0)` 모양."""
    parameters = []
    for parameter in spec.parameters:
        written = f"{parameter.name}: {parameter.declared_type.value}"
        if not parameter.required:
            written += f" = {parameter.default!r}"
        parameters.append(written)
    return f"{spec.name}({', '.join(parameters)})"


_TOOL_TABLE = "\n".join(
    f"- `{tool_signature(spec)}` — {TOOL_GUIDE[spec.name]}" for spec in TOOLS
)
_READERS = ", ".join(f"`{name}()`" for name in READER_NAMES)
_PAIRS = ", ".join(f"`{pair.opens}` / `{pair.closes}`" for pair in PAIRED_TOOLS)
_SHAPE_READS_AS = {
    MacroShape.object_: "an object",
    MacroShape.other: "any other shape",
}
_OPERATOR_TABLE = "\n".join(
    f"- {_SHAPE_READS_AS.get(shape, f'a {shape.value}')} takes "
    f"{operator_names(allowed_operators(shape))}"
    for shape in MacroShape
)
_FORBIDDEN = "\n".join(f"- {why}" for why in FORBIDDEN_TOOLS.values())

DEVELOPER_GRAMMAR = f"""# Writing developer macros for Artel QA

A developer macro is a small script you publish for your game. During a QA run the
Artel QA engine (a planner that picks what to do next and an executer that plays)
reads each macro's NAME, PARAMETERS and DOCSTRING and calls the macro instead of
guessing how your game is operated. Write one macro per thing a tester does
repeatedly and that is easy to get wrong without reading your code: finishing a
battle turn, placing a card, leaving a dialogue, entering a map node.

The text below is everything the checker accepts. `publish_macros` checks a macro
statically when you send it; nothing runs in the game until a QA run calls it.

## Shape

A macro is Python-looking text read with `ast.parse`; it is NOT executed as
Python. One `def` named exactly like the macro is the entry point, declared
`-> None`. Other `def`s in the same source are helpers it may call. Every
parameter and every assignment carries a type: {DECLARABLE_TYPE_NAMES}. A
parameter declared `object` receives something the caller aims at (see
**Aiming**); `string`, `int`, `float` and `bool` receive values. The planner fills
an `object` parameter by writing `selector("...")` or `find(...)` itself, so
prefer a `string` parameter for what it can read on screen (a card's word, an
enemy's name) and find the object inside the macro.

The entry `def` opens with a docstring saying what the macro does, when it is the
right move, and what each parameter means. The QA planner chooses macros by this
docstring alone, so write it for someone who has never seen your code. Strings
anywhere else are refused (use `#` comments); the docstring of a helper is
optional.

```python
def place_spell_card(word: string) -> None:
    \"\"\"Drag the hand card whose word is `word` into the first combine slot.
    Use in TurnBattleScene on the player's turn, with the combine window open.\"\"\"
    slot: object = selector("CombineSystem/CombineZone/Zone1")
    # The card body has the Collider2D; its word is on a child text object, so
    # find the body by name and compare its field instead of matching the label.
    for card in find_all(name="Card(Clone)", under="Hand"):
        if member(card, "Card.word") == word:
            drag(card, slot)
    wait_until(exists(selector("CombineSystem/CombineZone/Zone1/Card(Clone)")), 3,
               "no card landed in Zone1: the drag did not reach a card")
```

Two habits make a macro useful to the planner. **Aim at the object that receives
input** (see **Aiming**). **End every macro that changes the game with a
`wait_until` or `require` on the effect** — a card in the slot, the turn button
active again, the scene name. Without it a press that reached nothing still
returns "passed", and the planner calls the same macro again and again.

**Write a macro so calling it again continues where it stopped.** The game can
show something while a macro runs — a tutorial line after a spell is cast, a
reward window, a confirmation. When a press lands on a panel that covers the
screen and handles no input, the macro stops right there with `INPUT_BLOCKED`
and sends nothing after it; the QA agent clears what is on screen and calls the
macro again. So a macro of several stages must check, before each stage, whether
that stage is already done, and skip it when it is:

```python
def attack(spell: string, element: string) -> None:
    \"\"\"Cast one spell at the first enemy on the player's turn. Safe to call again
    after it stopped: it skips the stages already done.\"\"\"
    zone: object = selector("CombineSystem/CombineZone")
    if static("CombineZone.Instance.IsAwaitingTarget") == False:
        if member(zone, "CombineZone.spellCards.Count") == 0:
            place_card(spell)
        if member(zone, "CombineZone.magicTypeCards.Count") == 0:
            place_card(element)
        click(selector("CombineSystem/CombineZone/Button"))
        wait_until(static("CombineZone.Instance.IsAwaitingTarget") == True, 3,
                   "the cast did not start: both slots need a card")
    pick_first_enemy()
```

Without those checks a second call redoes the first stage — and an action that
toggles, such as Shift-clicking a card that is already in a slot, undoes it.
Keep one macro per thing a player decides (cast this spell, end the turn); do not
split it into a macro per click, because the QA agent then has to choose every
click in the right order. Split only where a player would choose — which card,
which enemy.

**Never wait by pressing a key.** A key that the game ignores, such as `F15`,
still counts as "any key" to a game that reads `Input.anyKeyDown`: it skips a
dialogue line or leaves a result screen. Wait with `wait_until` on what you are
waiting for.

## Actions

Each action is one call with the arguments below, positional in this order or
by keyword. An argument with `= value` may be left out. Count, order and type are
checked when you publish.

{_TOOL_TABLE}

`target`, `from_target` and `to_target` take an object (see **Aiming**), never a
string or a coordinate. `button` is 0 (left), 1 (right) or 2 (middle).

**Key names.** `key_code` is a Unity `KeyCode` name: `"Space"`, `"Return"`,
`"Escape"`, `"LeftShift"`, `"LeftControl"`, `"A"`, `"Alpha1"`, `"UpArrow"`,
`"F1"`, `"Mouse0"`. Case does not matter, but `"shift"`, `"enter"`, `"ctrl"` or
`"1"` are not `KeyCode` names and are refused when you publish. For Shift+click:
`hold_key("LeftShift")`, `click(card)`, `release_key("LeftShift")`.

## How actions reach your game

The SDK feeds every action into both input paths a Unity game usually reads:

- **Legacy Input Manager** (`Input.GetKey`, `GetKeyDown`, `anyKey`, `anyKeyDown`,
  `GetMouseButton*`, `mousePosition`, `GetAxis`, `GetButton*`). At build time the
  SDK rewrites these calls in your game assemblies, so a `press_key` is seen by
  `Input.anyKeyDown` and `Input.GetKeyDown`, and `hold_key("LeftShift")` by
  `Input.GetKey(KeyCode.LeftShift)`. You change nothing in game code. Touch APIs
  are not covered.
- **`OnMouseDown` / `OnMouseUp` / `OnMouseDrag` / `OnMouseEnter` / `OnMouseExit` /
  `OnMouseUpAsButton`** on objects with a `Collider` or `Collider2D`: the SDK casts
  a ray from the camera through the pointer and sends these messages to the
  nearest hit, so `click` and `drag` work on world sprites.
- **uGUI** (`Button`, `EventTrigger`, `IPointer*Handler`, `IDragHandler`) through the
  EventSystem.

The new Input System package (`UnityEngine.InputSystem`) is not driven.

A click lands where the pointer is: the SDK moves the pointer to the centre of
the target's on-screen rectangle (its `RectTransform`, or its `Renderer` bounds)
and presses there. If something else covers that point, the covering object gets
the click — the same as for a player.

## What the game reports (the pulse)

While a QA run plays, the SDK sends a pulse: the objects of every loaded scene,
including additively loaded scenes and the `DontDestroyOnLoad` scene. A macro
can only find, read and aim at objects in the pulse. An object is in it when it
has at least one of:

- a text component — `UnityEngine.UI.Text`, `InputField`, `TextMesh`, or any
  TextMeshPro text (`TextMeshProUGUI` and world-space `TextMeshPro`);
- a component with inspector-wired events (a `Button`'s `onClick`, an
  `EventTrigger`), or a UI control the SDK knows (`Button`, `Toggle`, `Slider`, ...);
- one of your game's components whose fields the build's code analysis watches.

A bare `SpriteRenderer` + `Collider2D` with none of these is NOT reported. A
clickable world object usually qualifies through its own game script; check what
was reported with `list_scene_objects`.

Inactive objects are reported but not targetable: `find`, `selector` and
`exists` only see active ones. Per object the pulse carries:

- `path` — names joined by `/`, e.g. `Canvas/CombineButton`;
- `selector` — the same with sibling indices, e.g. `Canvas[8]/CombineButton[3]`;
- `text` — the first non-empty text of the object's OWN text components (up to
  200 characters). A child's text belongs to the child: a card whose word is on a
  child `nameTMP` object is found with `find(label="Shoot")`, which returns the
  `nameTMP` child, not the card;
- the fields of your game's components on it (read with `member()`).

## Aiming

A target is one of:

- `selector("<path>")`: the hierarchy path from a scene root, as you see it in the
  Hierarchy window. Sibling indices are optional: `selector("Canvas/CombineButton")`
  and `selector("Canvas[8]/CombineButton[3]")` both work. Without indices, the
  first reported object with that path is used, so add indices or use `find`
  when two siblings share a name. Do not put the scene name in front.
- a parameter declared `object` on the `def` line;
- a name bound by `find(...)` or `selector(...)`: `slot: object = selector("...")`.

There is no syntax for a coordinate or an instance id (`#123`): both change
between game launches.

**Aim at the object that receives input**: the one that carries the `Collider` /
`Collider2D` whose `OnMouseDown` your code handles, or the uGUI `Button` /
raycast-target `Graphic`. A text object found with `find(label=...)` is the text
itself. On a world object that text is usually a child without a collider, and a
press aimed there reaches nothing: the action's result says `reached` is null and
the game does not change. (On uGUI a click on a child `Text` is passed up to its
`Button`, so `find(label="Start")` on a button caption works.) To pick a world
object by its word, walk the input objects and compare a field:
`for card in find_all(name="Card(Clone)"):` then
`if member(card, "Card.word") == word:`. There is no syntax for "the parent of".

**Scenes.** Objects in the active scene and in the `DontDestroyOnLoad` scene can be
aimed at — a tutorial or HUD kept across scenes with `DontDestroyOnLoad` is clicked
like any other object. Objects in an additively loaded scene are reported, but the
SDK resolves selectors only against the active scene and the `DontDestroyOnLoad`
scene, so aiming at them fails with "Nothing at selector". Read their values
(`text()`, `member()`, `exists()`) freely.

## Finding things

`find(label=..., name=..., under=...)` takes keywords only and needs `label=` or
`name=`:

- `label=` equals the object's own `text` exactly;
- `name=` equals the object's own name (the last path segment, with or without
  its `[index]`);
- `under=` is a path prefix, cut at a `/`: `under="Hand"` keeps `Hand/...` and
  drops `HandSlot/...`. Indices are optional here too.

A `find` that matches nothing or more than one object stops the macro there and
sends nothing. To act on every match, walk them:
`for card in find_all(name="Card(Clone)", under="Hand"):`.

## Reading values

The readers are {_READERS}. A condition uses one of them, a predicate, or one
comparison:

- `scene()` — the active scene's name, a string;
- `text(target)` — the object's text, a string;
- `exists(target)` / `absent(target)` — whether it is in the pulse and active;
- `actionable(target)` — whether it offers a click, key or pointer handler;
- `member(target, "Type.field")` — a field of one of your components on that
  object, e.g. `member(enemy, "Enemy.hp")`. No attribute is needed: every
  instance field of your game's own components on a reported object is sent
  (auto-properties under the property name);
- `static("Type.field")` — a static field, only when the build's code analysis
  watches it;
- `observable("<key>")` — the legacy `GAME_STATE` channel, off in a default build.
  Prefer `member()`.

`exists`, `absent`, `actionable`, `text` and `member` take a target written as a
`selector(...)` or a bound name, never `find(...)` inline: bind it first
(`enemy: object = find(name="Enemy")`), so a lookup that finds nothing stops at
the assignment. To ask whether something can be found, use
`exists(selector("..."))`.

A reader on the right of an assignment is read once, when that line runs:
`before: int = member(enemy, "Enemy.hp")`, then `click(enemy)`, then
`wait_until(member(enemy, "Enemy.hp") < before, 5, "the attack did no damage")`.
Declare such a name `int`, `float`, `string` or `bool`, never `object`.

## Statements

- an action, a helper `def` from this source, or an imported macro;
- a typed assignment: `card: object = find(label="Shoot")`;
- `if` / `elif` / `else`, `for <name> in find_all(...)`, `for i in range(n)`,
  `while <condition>`;
- `wait_until(<condition>, <seconds>, "<what went wrong>")` — waits until the
  condition holds, reading each new pulse, for at most the given seconds (a
  number written in the macro, more than 0 and at most
  {MAX_WAIT_PER_STATEMENT_SECONDS}). Use it after any action whose result takes
  time (an animation, a scene load, an enemy turn). While waiting, a lookup that
  finds nothing counts as "not yet";
- `require(<condition>, "<what went wrong>")` — checks the screen as it stands
  right after the previous action, without waiting;
- `flag("<message>")`, `ask_verdict("<expected>")`, `checkpoint("<what to look
  at>")` — notes for the QA agent running the macro; most developer macros need
  none of them;
- `match decide("<question>", key=value, ...):` with one `case "<imported macro>":`
  per option (at least {MIN_MATCH_CASES}) — a decision model picks the case. It
  may also pick `{NONE_FITS}`, which pauses the macro for the QA agent.
  `ask("<question>", ...)` is the yes/no form, usable as the whole condition of an
  `if`, `elif`, `while` or `require`.

**Conditions.** One condition is ONE comparison (`==`, `!=`, `>`, `<`, `>=`, `<=`),
ONE reader call that is a bool (`exists`, `absent`, `actionable`), ONE predicate
call, or ONE `ask(...)`. `and`, `or` and `not` are refused: write two `require`
lines for `and`; write a predicate for `or`.

**Predicates.** A `def` declared `-> bool` holds only typed assignments,
`if` / `elif` / `else` and `return`, and every path ends in `return True`,
`return False` or `return <condition>`. It cannot act or wait.

**Comparisons by the shape a value arrives as** (`int` and `float` both arrive as
a number; comparing two different shapes is refused):

{_OPERATOR_TABLE}

{NUMBER_EQUALITY}

## Imports

`from macros import end_turn, place_card` at the top, before the first `def`,
makes other developer macros of the same project callable like helpers. A name
must be published already or sent in the same `publish_macros` call; their order
in the call does not matter. When an imported macro is refused, every macro that
imports it is refused too: fix the imported one and send them together again.

## Refused

`and`, `or`, `not`, a dot (`card.id`), a nested `def`, any other `import`,
`lambda`, an f-string, `return` outside a predicate, a string outside the
docstring, re-assignment, arithmetic outside `-> int` / `-> float` `def`s,
`break`, `continue`, and a list. Also:

{_FORBIDDEN}

**What you hold, you release.** {_PAIRS} must be paired on every path through the
macro, in the same branch and the same loop pass. Nothing releases them for you.

## Limits

At most {MAX_STATEMENTS} statements along the longest written path, and a call
chain at most {MAX_CALL_DEPTH} deep (imports count). While running: a loop turns
at most {MAX_LOOP_PASSES} times, one call runs at most {MAX_EXECUTED_STATEMENTS}
statements, and all `wait_until` lines of one call wait at most
{MAX_WAIT_PER_CALL_SECONDS} seconds together."""
