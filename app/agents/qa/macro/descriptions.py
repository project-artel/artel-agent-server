"""모델이 읽는 tool 다섯의 설명.

`app/agents/qa/screen.py` 와 `app/agents/qa/capability.py` 의 선례다. tool 파일에 두면
그 파일이 정책 문서가 되고, 설명이 길어질수록 tool 본문이 묻힌다.

**받을 수 있는 것의 목록은 `grammar.py` 에서 조립한다.** `parse_target` 의 선례대로
설명이 이름을 전부 대야 하는데, 손으로 다시 적으면 모델이 읽는 글과 parser 가 실제로
받는 것이 어긋난다 — 그 어긋남은 모델이 거절을 받을 때까지 아무 데도 안 보인다.
"""

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

{_NUMBER_EQUALITY}"""

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
without writing it again. The answer tells you whether it got there. If it did
not, the macro is still callable for the rest of THIS run — do not register it
again on that account, because a second attempt changes nothing.

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
