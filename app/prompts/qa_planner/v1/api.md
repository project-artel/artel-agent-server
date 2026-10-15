---
version: v1
note: planner–executer 뼈대의 첫 prompt(ARTEL-1080). `g` 의 읽기 API 와, func 의 `run` 을 쓰는 macro 문법. 문법 글은 tool loop agent 가 읽는 것과 같은 글(`app/agents/qa/macro/reference.py` 의 `GRAMMAR`·`NUMBER_EQUALITY`)이 `macro_grammar` 자리에 들어가고, 그 위에 이 engine 에서 그 문법이 무엇을 뜻하는지를 적었다.
placeholders: [macro_grammar]
---
`g` — what the Python of a step module reads. It sends nothing.

Reading (synchronous, from the latest readings; none of these wait):
  g.scene -> str | None                     current scene name
  g.objects(match=None, tag=None) -> [Obj]  live objects; `match` is a case-insensitive
                                            substring of the selector/path
  g.find(match) -> Obj | None               first live match
  g.exists(match) -> bool
  g.value(match, member, default=None)      member value on the first match; `member` is
                                            "member" or "Type.member"
  g.text(match, default=None) -> str | None text the object shows
  g.texts() -> [str]                        every text on screen
  g.static(name, default=None)              a static value ("member" or "Type.member")
  Obj fields: .address, .selector, .id, .tag, .text, .rect, .values,
              .value(member, default=None)

Waiting (in `check`):
  await g.expect(lambda: <bool>, "what was expected", timeout=3)   the step's assertion;
                                            failing it fails the step
  await g.wait_until(lambda: <bool>, timeout=10, what="...")   returns once true, else raises
                                            WaitTimedOut. Re-checked on every reading.

What the game went through since this step began (from readings; for `check`):
  g.scenes_visited() -> [str]               scenes in order, current last
  g.seen_text("fragment") -> bool           a text containing it was on screen at some point
  g.seen_object("fragment") -> bool         an object whose selector contains it was live at
                                            some point
  g.at_step_start(n, "Type.member", match=None, default=None)
                                            the value the game reported when step n began
                                            (a static, or an object member with `match`;
                                            "text" with `match` is the text it showed)
  g.objects(match, include_off=True)        also objects switched off right now (Obj.live)

A func's `run`: a macro
Each func's `run` is a string holding a macro, which the executer runs when it picks the func.
The grammar below is the one the tool-loop QA agent writes its macros in, and the same parser
checks yours when you answer. It is written for that agent; here is what it means for a func:

- There are no macro tools here. A func's macro is checked when you answer, and run when its
  func is picked; nothing is registered, and `run_macro`, `resume_macro`, `register_macro` and
  `search_macros` do not exist. The entry `def run_<name>()` takes no parameters; a docstring on
  it is welcome but not required.
- `from macros import ...` reaches only macros already registered for this build by the
  tool-loop agent. You are not shown which exist, so import one only when a name you were given
  says it is a registered macro. `match decide(...)` picks among imported macros, so it needs
  them too; without them, decide with `if` on values, or `ask(...)`.
- A refused `require`, a `wait_until` that runs out, a lookup that matches nothing, a press that
  reaches nothing: each stops the macro, and the executer records it against the func as a
  failure, with the line and what was read. Funcs failing several turns in a row end play.
- `checkpoint("...")` ends the func's turn on that line, without a failure; the next turn
  decides again from the new state and starts the picked func's macro from its first line.
  Split a func in two rather than pausing inside it.
- `decide` answering `none_fits` counts as the func failing: its cases do not cover the state.
- `flag` and `ask_verdict` are only recorded: `check` judges the step.
- `reset_game` is not available.

{macro_grammar}
