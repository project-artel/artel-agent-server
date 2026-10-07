"""모델이 읽는 tool 다섯의 설명.

`app/agents/qa/screen.py` 와 `app/agents/qa/capability.py` 의 선례다. tool 파일에 두면
그 파일이 정책 문서가 되고, 설명이 길어질수록 tool 본문이 묻힌다.

**받을 수 있는 것의 목록은 `grammar.py` 에서 조립한다.** `parse_target` 의 선례대로
설명이 이름을 전부 대야 하는데, 손으로 다시 적으면 모델이 읽는 글과 parser 가 실제로
받는 것이 어긋난다 — 그 어긋남은 모델이 거절을 받을 때까지 아무 데도 안 보인다.
"""

from dataclasses import dataclass

from app.agents.qa.macro.grammar import (
    DECLARABLE_TYPE_NAMES,
    FORBIDDEN_TOOLS,
    MAX_CALL_DEPTH,
    MAX_STATEMENTS,
    PAIRED_TOOLS,
    READER_NAMES,
    REPORTING_NAMES,
    TOOL_NAMES,
    MacroShape,
    allowed_operators,
    operator_names,
)

_TOOLS = ", ".join(f"`{name}`" for name in TOOL_NAMES)
_READERS = ", ".join(f"`{name}()`" for name in READER_NAMES)

# 표를 손으로 다시 적지 않는다. 한 칸이 바뀌면 모델이 읽는 글도 함께 바뀌어야 하는데,
# 손으로 적은 표는 그때 조용히 늙는다 — 그 늙음은 모델이 거절을 받을 때까지 아무 데도
# 안 보인다.
#
# 모양 이름 앞에 붙는 관사만 여기서 정한다. `other` 는 열거된 모양 어디에도 안 맞는
# 것 전부라 이름이 아니라 설명으로 읽혀야 한다.
_SHAPE_READS_AS = {
    MacroShape.object_: "an object",
    MacroShape.other: "any other shape",
}
_OPERATOR_TABLE = "\n".join(
    f"- {_SHAPE_READS_AS.get(shape, f'a {shape.value}')} takes "
    f"{operator_names(allowed_operators(shape))}"
    for shape in MacroShape
)

_PAIRS = ", ".join(f"`{pair.opens}` / `{pair.closes}`" for pair in PAIRED_TOOLS)

# 일부러 뺀 이름들의 이유도 `grammar.py` 가 든다. 거절 문장이 그것을 그대로 쓰므로,
# 설명과 거절이 다른 말을 할 자리가 없다.
_FORBIDDEN = "\n".join(
    f"- {why}" for why in (*FORBIDDEN_TOOLS.values(), *REPORTING_NAMES.values())
)

# 문법 전체를 한 번 적는 자리. `write_macro` 와 `edit_macro` 가 같은 것을 받으므로 두
# 설명이 이것을 공유한다.
GRAMMAR = f"""A macro is written as Python-looking text, but it is NOT executed as Python:
it is read with `ast.parse` and anything outside the list below is refused when
you store it, with a refusal that names what is accepted instead.

**The shape.** One `def` named exactly like the macro is its entry point. Any
other `def` in the same source is a helper it may call. Every parameter and
every assignment carries a type, and the types are: {DECLARABLE_TYPE_NAMES}.
Every `def` returns `-> None`, because a macro hands back no value.

**The six statements.**
- an action: {_TOOLS}, or a helper `def` from this same source
- `require(<condition>, "<remedy>")` — both arguments are required. The remedy
  is what the agent reading the failure is told to do about it
- a typed assignment: `card: object = find(label="shoot")`
- `if` / `elif` / `else`
- `flag("<message>")` — tells you what the macro saw, and nothing more
- `ask_verdict(<step>, "<expected>")` — says that scenario step needs judging

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

**Conditions.** One condition is ONE comparison (`==`, `!=`, `>`, `<`, `>=`,
`<=`) or ONE reader call. `and`, `or` and `not` are refused — write two
`require` calls, so each says on its own what to do when it fails. The readers
are: {_READERS}. A condition written without a comparison has to be a bool, so
only `actionable()`, `exists()` and `absent()` stand alone.

**What each shape of value can be compared with.** The shape is the shape the
value ARRIVES as, not the type you declared: `int` and `float` both arrive as a
number, because the SDK rounds before sending. Comparing two different shapes is
refused rather than being false forever.

{_OPERATOR_TABLE}

An object's `==` is decided by which object it is, not by what it holds. Every
other shape — a sprite, an animator state, a number the game could not read —
refuses every operator rather than quietly answering false.

**Refused outright:** `for`, `while`, `and`, `or`, `not`, a dot (`card.id`), a
nested `def`, `import`, `lambda`, an f-string, `return`, a docstring,
re-assignment and arithmetic. And these, each for its own reason:

{_FORBIDDEN}

**What you hold, you release.** {_PAIRS} are counted along every path through
the macro, and a macro that ends any path still holding one is refused when you
register it. That includes holding inside one branch of an `if` and releasing
outside it: hold and release in the same branch, or in neither. Nothing releases
these for you, and a key left down changes every step after the macro.

**The limits, checked when you register rather than while it runs:** at most
{MAX_STATEMENTS} statements along the longest path, and a call chain at most
{MAX_CALL_DEPTH} deep counting the entry point. Caught at registration because
an action already sent to the game cannot be taken back."""


@dataclass(frozen=True)
class MacroExample:
    """모델이 읽는 예시 하나.

    `source` 는 `macro_definition_from_source` 를 실제로 통과하는 글이다. 설명 문자열
    안에 글로만 박아 두면 문법이 바뀔 때 예시가 조용히 거절되는 글이 되고, 그 거절은
    모델이 베껴 써서 런에서 받을 때까지 아무 데도 안 보인다. 그래서 목록으로 들고
    `tests/test_qa_macro_descriptions.py` 가 이것을 돌며 parser 에 넣는다.
    """

    # 진입점 `def` 의 이름. 테스트가 `macro_definition_from_source` 에 그대로 넘긴다.
    name: str
    # 예시 위에 붙는 한 줄. 무엇을 보여 주는 예시인지 말한다.
    shows: str
    source: str


# 첫 예시가 긴 것은 일부러다. 저자가 틀리는 자리는 문법이 아니라 순서와 분기다 —
# 화면을 안 세우고 시작하고, 분기 안에서만 확인하고, `require` 의 둘째 인자에 무엇을
# 할지가 아니라 무엇이 틀렸는지를 적는다. 짧은 조각 여러 개로는 그 셋이 안 보인다.
#
# 둘째 예시가 따로 있는 이유는 `hold_key` / `release_key` 짝과 `enter_text` 가 첫
# 예시의 절차에 자연스럽게 안 들어가기 때문이다. 억지로 끼우면 그 자리에서 왜 누르고
# 왜 푸는지가 안 읽힌다.
EXAMPLES: tuple[MacroExample, ...] = (
    MacroExample(
        name="attack_first_enemy_with",
        shows=(
            "names the screen, opens a panel only when it is shut, runs the "
            "procedure, and says which step now needs judging"
        ),
        source='''def attack_first_enemy_with(card_label: string, element_label: string) -> None:
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

    # A reading cannot be bound to a name — an assignment takes `find(...)`,
    # `selector(...)` or one literal, nothing else. So write the bound down and
    # compare against that instead of against a value read before the click.
    starting_health: float = 100
    enemy: object = find(name="Enemy")
    click(enemy)

    # Two bounds rather than one `==`. A number arrives rounded to four decimals, so
    # a value sitting still still wobbles in its last place.
    require(member(enemy, "Health.current") < starting_health, "The attack landed but took no health.")
    require(member(enemy, "Health.current") > 0, "The enemy died outright; this step wanted chip damage.")

    # `flag` tells you what the macro saw, and nothing more.
    flag("combined the two cards and attacked the first enemy")

    # `ask_verdict` says a scenario step now has something to judge. The step number
    # is written out as a literal, so a macro that asks for a verdict is tied to that
    # one step. The macro judges nothing: you still call `report_step` after it
    # returns.
    ask_verdict(4, "the enemy loses health equal to the combined card's power")


def confirm_combination() -> None:
    # A helper keeps a named sub-procedure in one place. Its statements count toward
    # the same limit as the entry point's.
    button: object = find(name="ConfirmCombine")
    require(actionable(button), "Both slots need a card before Confirm turns on.")
    click(button)
''',
    ),
    MacroExample(
        name="rename_the_save_slot",
        shows="types into a field and releases everything it held",
        source='''def rename_the_save_slot(field_label: string, new_name: string) -> None:
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
''',
    ),
)

_EXAMPLE_SOURCES = "\n\n".join(
    f"A macro that {example.shows}:\n\n```python\n{example.source.strip()}\n```"
    for example in EXAMPLES
)

# 예시 블록. `write_macro` 에만 싣는다 — tool 설명은 매 호출마다 전송되므로 두 설명이
# 이것을 나눠 들면 같은 글을 두 번 보낸다. 쓸 때 읽어야 하는 글이고, 고칠 때는 이미
# 쓴 글이 눈앞에 있다.
EXAMPLES_BLOCK = f"""**Two macros that pass, with the reasoning written into them.** What these do
that a first draft usually does not:

- the screen is named in the first statement, so a wrong-screen call fails on
  that line instead of on an aim ten lines down
- every `require` remedy says what to DO about the failure, because the agent
  reading it has only that sentence to go on
- the `if` opens the panel and the `require` AFTER it checks the panel is open,
  so the check holds whether the branch ran or not
- objects are found by the text they show, with `under=` narrowing where it
  could be ambiguous, so the macro is not tied to one hand or one layout
- a number is bounded with `<` and `>`, never pinned with `==`
- what is held is released in the same body that held it

{_EXAMPLE_SOURCES}"""

# 연속값 권고. 거절이 아니라 관용구를 주는 것이라 `write_macro` 에만 싣는다.
_NUMBER_EQUALITY = """**A caution about `==` on a number.** It is allowed, and on a continuous value
— a position, a timer — it is treacherous. The SDK rounds a number to four
decimals before sending it, so a value sitting still still wobbles in its last
place. The idiom is two `require` calls with `<` and `>` around the value you
expect, rather than one with `==`. This is advice, not a refusal."""

WRITE_MACRO_DESCRIPTION = f"""Write a macro draft: a named sequence of actions you can call again later.

Use this once you have worked out a sequence by hand and expect to need it
again — dealing a card into a slot, walking a dialogue to its end, setting up the
board a step needs. The draft is parsed and you are told what it would do, or
exactly what is wrong with it. **Nothing is registered and nothing runs.**
`register_macro` is what makes it callable.

`name` is what you will call it by, and the source must contain a `def` with
exactly that name — that `def` is the entry point and its parameters are what
`run_macro` will ask you for.

Calling this with a name that already has a draft replaces that draft outright.
To change part of one, read it with `read_macro` and change that part with
`edit_macro`.

{GRAMMAR}

{_NUMBER_EQUALITY}

{EXAMPLES_BLOCK}"""

EDIT_MACRO_DESCRIPTION = f"""Change part of a macro draft by replacing text in it.

`old_text` is matched as plain text against the draft and has to appear EXACTLY
ONCE. Nothing happens if it appears zero times or more than once, and you are
told which — a line number would be worse, because getting one wrong changes the
wrong line silently.

Read the macro with `read_macro` first. This refuses a macro this run has not
read, because changing text you have not seen is changing text you cannot check.

If the name has no draft but is already registered, the registered macro is
copied into a draft and the draft is changed. **The registered macro is left
exactly as it was**, so a run calling it while you edit is unaffected, and
`register_macro` is what replaces it.

{GRAMMAR}"""

READ_MACRO_DESCRIPTION = """Read a macro's source text.

Returns the draft if there is one, and the registered macro's source otherwise.
A name this run has never seen is looked up in the content map, so a macro an
EARLIER run registered comes back here too. This is also what licenses
`edit_macro`: changing text you have not read is changing text you cannot check.

The text comes back as it was written, comments and blank lines included, which
is why the macro's own source is what is stored rather than something printed
back out of a parsed tree."""

REGISTER_MACRO_DESCRIPTION = """Register a macro draft so `run_macro` can call it, in this run and in later ones.

The draft is parsed again and refused if anything in it is not allowed — broken
syntax, an unknown tool name, an untyped parameter, a comparison that could never
be true. Nothing is registered when it is refused, and the refusal names what to
write instead.

A registered macro is written to the content map, so a later run can call it
without writing it again. The answer tells you whether it got there, and the
macro is callable for the rest of THIS run either way. When the answer says the
write could not be CONFIRMED, registering again changes nothing — it may well
have been stored. When the answer names a reason it was refused, that reason is
the thing to fix before registering again.

A name that is already registered is updated in place, so the `screen` relations
that name already carries are kept rather than dropped and rebuilt.

The macro is related to the `screen` you are standing on right now, and
`screens` lets you name more. A screen is named by its ID — the number in the
`content map: you are on screen <id>` line of your scene view — not by a scene
name, and an id from another game build is refused. `screens` ADDS to what is
already there; there is no way to take a relation away. When the run does not
yet know which screen it is standing on, the macro registers with no relation at
all — which is the right record of "nobody knows where this is used yet"."""

RUN_MACRO_DESCRIPTION = """Call a macro you registered earlier, against the screen you are on now.

`arguments` is a mapping from the entry `def`'s parameter names to values. Every
parameter without a default has to be there. An `object` parameter takes a
selector — a Unity hierarchy path — and a coordinate or a `#id` string is refused
before anything reaches the game, for the same reason the macro's own syntax has
no way to write one.

A draft is not callable; `register_macro` first. When a name has both a draft and
a registration, this calls the REGISTERED one, so editing a macro never changes
what a call does until you register the change.

A name this run never registered is looked up in the content map first, so a
macro an EARLIER run registered can be called straight away. Call `read_macro`
on it before you do, to see what it will send.

What comes back says how far it got. The actions that actually reached the game
are listed apart from the ones that did not, and apart again from the ones an
`if` decided to skip — an action already sent cannot be taken back, so calling
the macro again starts from a board that is not where it started last time.

Anything the macro flagged comes back too, and so does any step it says needs
judging. A flagged line is for you to read; a step it names still needs your
`report_step`, and the macro does not report anything itself."""
