# 2026-10-13 — Macro `import`, `match decide(...)` and `ask(...)` backed by Jev

- Date: 2026-10-13
- Jira: not filed yet
- Status: Draft
- Base: `origin/develop` at 3a7a666 (ARTEL-984 and ARTEL-988 merged)

## Goal

When the agent enters a scene, it writes one **orchestration macro** for that scene. The
orchestration macro imports the macros already registered for the scene, loops, and on every
pass asks the Jev decision model which imported macro fits the current state. It then calls that
macro. When Jev answers that none of the imported macros fits, the macro pauses and hands the turn
back to the agent, which plays that pass by hand or writes the missing macro and adds it.

Three grammar additions make this possible:

1. `from macros import a, b` — call another registered macro like a helper.
2. `match decide("<question>", key=value, ...)` with one `case "<imported macro>":` per option —
   Jev picks the case. The option text Jev reads is each imported macro's docstring.
3. `ask("<question>", key=value, ...)` — a bool from Jev, usable as a whole condition of
   `if`, `while` and `require`.

For both 2 and 3, **the agent writes the context Jev reads** as keyword arguments.

## Non-goals

- orchestration-server changes. `definition` stays a jsonb passthrough and imports are resolved
  from `source` on every read, the same way stored macros are already re-parsed.
- Moving `JevScenarioRouter` and `PulseRelevanceJudge` onto the new Jev client. Follow-up.
- `ask` inside `wait_until` (one Jev call per `pulse`) and inside a predicate `def` (predicates
  are evaluated synchronously). Both are refused when the macro is stored.
- Context made of a change history (`since="action"`), screen captures, scenario steps or
  `knowledge`. Variants D/E from the 2026-10-13 discussion; added only when a run shows the need.
- A probability threshold on `ask` or `decide`. `JevScenarioRouter` measured the probability
  moving by up to 0.100 between identical calls (ARTEL-944), so a tunable threshold would be
  tuning noise.

## Context / Constraints

- Stage on 2026-10-13: 7 `checkpoint` statements in 5 of 21 macros. 4 wait, 2 wait and look,
  1 hands an input to the agent, 0 decide whether to go on. The waits move to `wait_until`
  (ARTEL-988). So `decide` is a new capability, not a replacement for `checkpoint`.
- `parser.py:357-364` refuses `Import`/`ImportFrom`; `ast.Match` falls through to the generic
  refusal at `parser.py:811`. Python is 3.11+, so `ast.Match` exists.
- `macro_definition_from_source(name, source)` is a pure function. Fetching a registered macro is
  async (`channel.read_macro`, `macro_tools.py:268`). So imports are fetched **before** parsing
  and handed to the parser.
- The runner only resolves `self.definition.function(name)` (`runner.py:541, :855`), and every
  call-graph check in `_CallGraph` assumes callees live in the same definition.
- `_evaluate` is synchronous and `require`/`if`/`while`/`wait_until` all go through it. `ask`
  therefore cannot be an ordinary reader; it is a separate condition kind that the three
  statement handlers await before they evaluate.
- Jev request shape (both existing callers): `{"model", "state", "questions": {name: {"type",
  "instructions", "criteria"}}}`. `choice` criteria map option → `{"what", "not_for"?,
  "examples"?}` and the answer is `answers[name]["choice"]`; `noul` answers
  `answers[name]["noul"]`, the probability of `true`. Jev returns no free text.
- Limits that bound the orchestration loop and stay as they are: `MAX_LOOP_PASSES` 50,
  `MAX_EXECUTED_STATEMENTS` 1000, `MAX_WAIT_PER_CALL_SECONDS` 60, `MAX_CALL_DEPTH` 3.

## Design

### 1. `import`

```python
from macros import end_turn, cast_pair_first_target
```

- Top level only, before the first `def`. Module name is exactly `macros`. No `as`, no `*`.
- An imported name may not collide with a `def` in the same source.
- Called exactly like a helper, as a statement with arguments bound against the imported entry's
  parameters. A predicate can not be imported (the entry is never `-> bool`).
- **Resolution:** `imported_names(source) -> tuple[str, ...]` is a cheap pre-pass over
  `ast.parse`. The tool layer fetches each name — this run's book first (`state.macros.registered`),
  then `channel.read_macro` — parses it recursively with its own imports, and passes the parsed
  definitions to `macro_definition_from_source(name, source, imports=...)`. A missing name is
  `MACRO_IMPORT_NOT_FOUND`; a cycle (`a` imports `b` imports `a`) is `MACRO_IMPORT_CYCLE`.
- **Shape:** `MacroDefinition.imports: tuple[MacroDefinition, ...] = ()`. Each import keeps its own
  entry and helpers, so helper names in two macros never collide. The stored `definition_json`
  carries the copies at register time, but nothing reads it back; every read re-parses `source`
  against the macros registered **now**.
- **Call graph:** an imported macro is one node whose depth is its own depth and whose statement
  count is its own entry count. `depth_of(caller) = 1 + depth_of(import)`, so
  orchestration → `fight_rain_stage` → `rain_pair` is depth 3, the current limit.
  `reject_unpaired` and `reject_checkpoint_while_holding` walk into imports through the same
  lookup.
- **Runner:** `_Frame` gains `definition`. `_helper` looks the callee up in the calling frame's
  definition: a local `def` first, then `imports` — and an imported entry runs in a frame whose
  `definition` is that import, so its own helpers resolve.

### 2. `match decide(...)`

```python
from macros import end_turn, cast_pair_first_target, heal_with_holy

def play_undead_boss() -> None:
    """Play the undead boss fight in TurnBattleScene, one decision per turn."""
    player: object = find(name="Word")
    while scene() == "TurnBattleScene":
        boss: object = find(name="Boss_Undead(Clone)")
        hp: float = member(player, "Player.Hp")
        match decide("What should the player do this turn?", hp=hp, boss=boss, hand="Card(Clone)"):
            case "end_turn":
                end_turn()
            case "cast_pair_first_target":
                spell: object = find(name="Card(Clone)", label="Shoot")
                element: object = find(name="Card(Clone)", label="Fire")
                cast_pair_first_target(spell, element, "Boss_Undead(Clone)")
            case "heal_with_holy":
                heal_with_holy()
```

- Subject is exactly one `decide(...)` call. Patterns are string literals naming an imported
  macro. No guard, no `case _`, no duplicate. At least two cases.
- The case body is an ordinary body (assignments, `find`, `require`, calls…). It does not have to
  call the macro it is named after, but the option Jev reads **is that macro's docstring**:
  `criteria[name] = {"what": <docstring>}`. A good docstring is a good option; ARTEL-984 already
  makes the docstring mandatory at register time.
- The runner adds one more option, `none_fits`, with
  `{"what": "None of the other options fits the current state."}`. When Jev picks it, the macro
  **pauses** — same mechanism as `checkpoint`, rendered as `NO FITTING MACRO`, with the question,
  the options and the `state` Jev read. `resume_macro(proceed=true)` skips that `match` and goes
  on (the agent played that pass by hand); `proceed=false` stops the macro so the agent can
  register a new macro, add a `case` and run again.
- Each decision is recorded in `MacroRunResult.decisions` and rendered under "Decisions:"
  (`step 4 (pass 3): decide picked cast_pair_first_target`).
- Counting: a `match` counts 1 + the largest case body, like `if`.
- Jev failure (HTTP error, missing key, unknown option returned) stops the macro with
  `DECIDE_FAILED`. It does not fall back to the agent silently.

### 3. `ask(...)`

```python
while ask("Is the battle still undecided?", boss=boss, hp=hp):
    end_turn()
```

- A whole condition of `if`/`elif`, `while` and `require`; not an operand of a comparison, not in
  `wait_until`, not in a predicate.
- Jev `noul` question; `true` when the probability is at least 0.5. The probability is recorded
  in `observed["ask"]` so a failure payload shows it.
- Failure stops with `ASK_FAILED`.

### 4. Context the agent writes

Keyword arguments to `decide`/`ask` become keys of the Jev `state`. `scene` is always added.

| Argument | What lands in `state[key]` |
|---|---|
| `boss=boss` — an `object` name (parameter, `find`, `for`) | `{"path", "text", members...}`: every member of that object that the `pulse` holds, minus those judged cosmetic when `pulse_relevance` is on |
| `hp=hp` — a reading bound earlier | the value as it was bound |
| `goal="beat the boss"` — a string/int/float/bool literal | the literal |
| no keyword at all | `{"objects": ...}`: every live held object, same member rule, capped |

- Values are clipped to 80 characters each (`relevance._render_value`) and the whole `state` to
  `MAX_DECIDE_STATE_CHARS` (6000, about 1.5k tokens). Clipping is reported in the decision line.
- A keyword named `scene` is refused (reserved).

### 5. Jev client

`app/qa/jev.py`: `JevClient` with `async def ask(...) -> float` and
`async def choose(...) -> str`, typed request/answer models (`JevQuestion`, `JevChoice`), the same
headers and URL as `relevance._post`, one `httpx.AsyncClient` per run, closed in the runner's
`finally` next to `pulse_relevance`. `MacroHost` gains `async def decide(request) -> str` and
`async def ask(request) -> float`; `_Host` forwards to the run's client. Tests use a fake host.

### 6. Prompt and label

- `qa_run/v22` copied from v21. `skill_macro.md` gains Import / Decide / Ask paragraphs and one
  worked example (the boss fight above); `macro_memory_directive.md` tells the agent to write an
  orchestration macro when it enters a scene that already has macros. `reference.py` `GRAMMAR`
  follows. Lock regenerated.
- `QA_ARCH_LABEL = "v25-macro-decide"` with its paragraph. The tool set and schemas do not move,
  so `arch_fingerprint` stays; only the label pin in `tests/test_qa_arch.py` changes.

## Approach (Checklist)

- [x] **Step 0: Recon**
- [ ] **Step 1: Contract** (serial) — `model.py` (`MacroImport` via `MacroDefinition.imports`,
  `MacroMatchStatement`, `MacroMatchCase`, `MacroDecideCall`, `MacroAskCondition`,
  `MacroContextArgument`), `grammar.py` (`DECIDE`, `ASK`, `IMPORT_MODULE = "macros"`,
  `NONE_FITS`, `MAX_DECIDE_STATE_CHARS`), `errors.py` (`MACRO_IMPORT_NOT_FOUND`,
  `MACRO_IMPORT_CYCLE`, `DECIDE_FAILED`, `ASK_FAILED`), the `MacroHost` additions and
  `MacroDecideRequest`/`MacroAskRequest`. Model tests.
- [ ] **Step 2a: parser track** — `parser.py`, `tests/test_qa_macro_parser.py`.
- [ ] **Step 2b: runner track** — `runner.py`, new `app/agents/qa/macro/context.py` (state
  builder), `tests/test_qa_macro_runner.py`, `tests/test_qa_macro_context.py`.
- [ ] **Step 2c: Jev client track** — `app/qa/jev.py`, `tests/test_qa_jev.py`.
- [ ] **Step 3: tools** (after 2a–2c) — `macro_tools.py` (import resolution, `_Host.decide/ask`,
  `_render` for decisions and `NO FITTING MACRO`), `app/agents/qa/runner.py` (client lifetime),
  `tests/test_qa_macro_tools.py`.
- [ ] **Step 4: prompt and label** — `qa_run/v22`, `reference.py`, lock, `arch.py`, tests.
- [ ] **Step 5: full test suite.**
- [ ] **Step 6: measurement** — `qa-run-local`, WordVenture, before (v21 prompt, v24 label) and
  after (v22, v25), same model.

## Validation

- **Commands:** `LANGSMITH_TRACING=false .venv/bin/python -m pytest -q`
- New tests: import resolution (found, missing, cycle, collision, depth through imports,
  hold/release through imports), `match` parsing refusals, runner `decide` picking each case and
  `none_fits` pausing, `ask` true/false and failure, context building for each argument kind and
  clipping, tool rendering.

## Risks & Rollback

- **Risks:**
  - An orchestration loop over a whole fight meets `MAX_WAIT_PER_CALL_SECONDS` (60) quickly when
    each pass waits for the enemy turn. It stops with `WAIT_TIMEOUT` and the agent runs it again;
    the measurement shows how often.
  - An imported macro changed after the orchestration macro was registered changes what it does.
    Intended (latest wins) but surprising; the decision line names the macro that ran.
  - Jev picks badly with thin docstrings. `none_fits` is the safety valve.
- **Rollback:** `git revert`. A stored source using `import`, `match` or `ask` is then refused on
  read with `MACRO_NODE_REJECTED`, and only that macro becomes unusable.

## Open Questions

- Should the orchestration macro get a larger per-call wait budget than 60 seconds?
- Jira issue for this work (summary and parent epic).
