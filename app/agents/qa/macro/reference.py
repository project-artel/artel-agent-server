"""`grammar.py` 가 받는 것을 글로 조립한다. 모델이 읽는 글은 아니다.

모델이 읽는 것은 `app/prompts/qa_run/v19/skill_macro.md` 다. prompt 는 데이터로
버전 디렉터리에 살고 `prompts-lock.json` 이 그 본문을 해시하므로, 코드가 런타임에
조립한 글로 대신할 수 없다 — 해시할 파일이 없어진다.

**그래서 이 모듈은 그 파일이 맞는지 재는 기준이다.** `tests/test_qa_macro_reference.py`
가 [GRAMMAR] 와 [NUMBER_EQUALITY] 를 `skill_macro.md` 안에서 글자 그대로 찾는다.
`grammar.py` 에 tool 하나가 늘면 여기서 조립한 글이 달라지고 그 테스트가 깨지며,
깨진 메시지가 skill 파일의 어느 절을 고치라고 말한다.

손으로 다시 적는 길을 안 두는 이유는 그것이 조용히 늙기 때문이다. 설명과 parser 가
어긋나면 그 어긋남은 모델이 예시를 베껴 쓰고 거절을 받는 런에서야 처음 보인다.
"""

from app.agents.qa.macro.grammar import (
    DECLARABLE_TYPE_NAMES,
    FORBIDDEN_TOOLS,
    MAX_CALL_DEPTH,
    MAX_EXECUTED_STATEMENTS,
    MAX_LOOP_PASSES,
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

**The statements.**
- an action: {_TOOLS}, or a helper `def` from this same source
- `require(<condition>, "<remedy>")` — both arguments are required. The remedy
  is what the agent reading the failure is told to do about it
- a typed assignment: `card: object = find(label="shoot")`
- `if` / `elif` / `else`
- `for <name> in find_all(...)` / `for <name> in range(n)`, and `while <condition>`
- `flag("<message>")` — tells you what the macro saw, and nothing more
- `ask_verdict("<expected>")` — says the step `run_macro` was called with needs
  judging. There is no step number to write: it is always that step
- `checkpoint("<what to look at>")` — hands the turn back to you mid-macro

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

**Checkpoints.** `checkpoint("check the slot holds both cards")` ends the
macro's turn right there and hands you what it did so far, marked PAUSED — not
a failure. Look at the screen, then call `resume_macro` with `proceed: true` to
carry on from the next line, with every name it bound still bound, or
`proceed: false` to stop it there and send nothing more. Each checkpoint costs
you a turn, so place one only where a person would want to look: before
committing to something that cannot be undone, or after a step whose result no
`require` can describe.

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

**Refused outright:** `and`, `or`, `not`, a dot (`card.id`), a nested `def`,
`import`, `lambda`, an f-string, `return`, a docstring, re-assignment,
arithmetic, `break`, `continue`, and a list or any other call after `for ... in`.
And these, each for its own reason:

{_FORBIDDEN}

**What you hold, you release.** {_PAIRS} are counted along every path through
the macro, and a macro that ends any path still holding one is refused when you
register it. That includes holding inside one branch of an `if` and releasing
outside it: hold and release in the same branch, or in neither. Inside a loop,
hold and release in the same pass — a pass that presses without letting go
presses once more on every turn. A `checkpoint` may not sit between a hold and
its release, because the turn would end with the game held. Nothing releases
these for you, and a key left down changes every step after the macro.

**The limits.** Checked when you register: at most {MAX_STATEMENTS} statements
written along the longest path, counting each loop body once, and a call chain
at most {MAX_CALL_DEPTH} deep counting the entry point. Checked while it runs,
because how often a loop turns is not known until then: a loop turns at most
{MAX_LOOP_PASSES} times, and one call runs at most {MAX_EXECUTED_STATEMENTS}
statements. Past either, the macro stops with `LOOP_LIMIT`, and what it already
sent stays sent. A step number in the result is the written line; `(pass N)`
says which turn of the loop around it."""


# 연속값 권고. 거절이 아니라 관용구를 주는 것이라 `GRAMMAR` 밖에 둔다 — skill 파일도
# 두 절을 나란히 싣는다.
NUMBER_EQUALITY = """**A caution about `==` on a number.** It is allowed, and on a continuous value
— a position, a timer — it is treacherous. The SDK rounds a number to four
decimals before sending it, so a value sitting still still wobbles in its last
place. The idiom is two `require` calls with `<` and `>` around the value you
expect, rather than one with `==`. This is advice, not a refusal."""
