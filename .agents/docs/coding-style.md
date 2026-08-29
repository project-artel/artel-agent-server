# Coding Style

## Why

Consistent shape lets a reader predict where behavior lives and what a name
means, so review spends its attention on the change instead of the style.

## Principles

Prefer:
- explicit naming
- small functions
- single responsibility
- early return
- composable logic
- deterministic behavior

## Structure

Prefer:
- shallow nesting
- clear boundaries
- modular design
- immutable-oriented flow
- explicit dependencies

Avoid:
- giant classes
- god objects
- util dumping grounds
- hidden side effects
- deep inheritance
- untyped payload maps

## Data Shapes

Model every payload that crosses a boundary — HTTP request and response,
WebSocket frame, queue message, DB row, LLM tool argument — as a declared type:
a Kotlin `data class`, a Pydantic model, a C# serializable class, a TypeScript
interface. Do not thread `JsonNode`, `Map<String, Any>`, `dict[str, Any]`, or
`Record<string, unknown>` through application code.

An untyped map moves every mistake to runtime and to the far end of the call
chain. A misspelled key, a field that quietly changed type, a value nobody
populated — a DTO surfaces these at the parse boundary with the offending field
named; a map surfaces them as a null three layers away, if at all. The DTO is
also the only place the contract is written down.

**Parse once, at the boundary.** Convert the payload into its typed model where
it enters the process, and let everything downstream take that type. Do not
carry raw JSON deeper "just in case".

Raw JSON is the right choice when:
- the code is passthrough — it stores or forwards the payload without reading
  fields (a `jsonb` column, a proxied body)
- the schema is genuinely open at that point (LLM tool arguments as received),
  and the next step validates it into a typed model
- a test asserts on the serialized wire shape

Existing untyped code is not a defect to sweep. Convert a payload when you are
already changing the code that reads it.

## Naming

Names should reveal intent immediately.

Avoid:
- Manager
- Helper
- Utils
- Temp
- Data
- Thing
- Misc

Prefer:
- domain-specific naming
- action-oriented function names
- explicit variable meaning

### Variables

A variable name says what the value is, in whole words, so a reader who lands on
the line never has to go back to the declaration to find out. Length is the
cheap part. The expensive part is a name that costs a lookup every time it
appears.

**Write the word out.** `response`, not `res`. `index`, not `idx`. `request`,
not `req`. `configuration`, not `cfg`. `message`, not `msg`. `temporary`, not
`tmp`. The exception is an abbreviation the domain already reads as a word of
its own — `id`, `url`, `http`, `json`, `sql`, `qa`, `sdk` — where expanding it
reads worse than leaving it alone.

**Name the value, not its container or its position.** `expiredSessions`, not
`list2`. `sceneCount`, not `count`. `pendingRunIds`, not `arr`. The type is
already on the line; what the reader cannot see is which of the many possible
collections this one is.

**Let the name's shape match the value's shape.** A collection reads as a
plural. A boolean reads as a claim that is true or false — `hasAnchor`,
`isExpired`, `shouldRetry` — never `flag` or `check`. A number that carries a
unit names the unit: `timeoutMilliseconds`, not `timeout`.

**A longer name is not a worse name.** `anchorsMissingScreen` beats `filtered`
at three times the width, because it still means something when quoted in a
review comment or a stack trace. Shorten a name by narrowing the concept behind
it, never by dropping letters out of the words.

Single letters are for a loop index over a numeric range and for a lambda
parameter whose entire scope is one line. Anywhere else they are a defect.

Casing follows the language's own convention. This rule is about which words a
name contains, not how they are cased.

**Rename what you touch.** Much of this codebase predates this rule and reads
that way. That backlog is not a sweep to schedule; it is work to do in passing.
When you change a function, rename the badly named variables inside it in the
same commit. A name you had to decode in order to make your change is exactly
the name to fix while you still have it decoded.

This is not the unrelated cleanup that `## Refactoring` below and `commit.md`
warn against. The rename is inside the change, not beside it. The test is
whether the diff would have opened that region anyway.

Stop at the edge of the change. A rename that reaches into callers, alters a
public signature, or opens a file the change had no other reason to open is a
separate commit and usually a separate issue — say so instead of widening the
diff. And leave a name alone when it is load-bearing outside the code: a
serialized field, a database column, a wire contract, or a name that a log
query, dashboard, or saved search matches on. Renaming those is a behavior
change wearing a rename's clothes.

## Comments

### Language

**Write comments in Korean.** This covers every comment that lives in a source
file: line comments, block comments, and doc comments — KDoc, Python
docstrings, C# XML doc, JSDoc/TSDoc, and SQL/migration comments.

Korean is the language reviewers read. A comment carries the reasoning that the
code cannot, and reasoning written in the reviewer's language is read; reasoning
written in a second language is skimmed.

Keep these in English inside a Korean comment:
- identifiers, type names, API paths, file paths
- library, framework, and tool names
- error codes and exact error strings being quoted

```
// 재시도는 3회까지만 한다. 업스트림이 429를 돌려줄 때 백오프 없이 더 밀면
// 레이트 리밋 창이 갱신되지 않아 복구가 오히려 늦어진다.
```

This rule covers source files only. Documentation, commit messages, and PR
bodies keep the language their own conventions already set — `AGENTS.md`,
`commit.md`, `pull-request.md`.

Which words count as a technical term is decided in
[`../../AGENTS.md`](../../AGENTS.md). Follow its terminology rule whenever you
write a comment: a thing the code names keeps the name the code gave it, in
backticks, even inside a Korean sentence. That rule is wider than this one — it
also covers plan documents and pull request bodies.

That scope is about which language a comment is written in. Which words it
reaches for is settled by `## Word Choice` below, and that rule does reach
commit messages, issue bodies, and pull request bodies.

Existing English comments are not a defect. Rewrite one only when you are
already changing that code; a comment-language sweep is unrelated churn.

### Content

A comment explains a constraint or a non-obvious decision — the rejected
alternative, the failure mode being prevented, the invariant a caller has to
hold. Do not restate what the code already says.

Delete commented-out code instead of parking it. Git history holds it.

## Word Choice

This section is about the words you pick when you write about this code: the
verbs, and the nouns for things the code never named. A thing the code does
name keeps the name the code gave it — `pulse`, `screen`, `capability`,
`anchor` — in backticks, even inside a Korean sentence.

It applies wherever that writing lands: line and block comments, KDoc and
docstrings, SQL comments, test names, commit messages, and issue and pull
request bodies.

**Never invent a Korean word to carry a technical meaning.** Verbs count as
much as nouns. The test is not whether a Korean rendering is possible. It is
whether a Korean speaker who has not read the code would use that word for
this. `발화하다` for `fire` fails it: nobody who has not read the file knows
what it means.

Where the ordinary Korean word is exact, use it. Merging two duplicate `screen`
rows really is `합친다` — `접는다` was reached for because it sounded closer to
the code, and it left the sentence harder to read and no more precise. Where
reaching for a Korean word produces something you had to make up, write the
English word instead. English is a safe answer. A made-up word never is.

**Pick the word that is correct, not the word that sounds considered.** Test a
word by asking whether it actually means this, not whether it sounds right in
the sentence. `capture 를 청구한다` is wrong because `청구` is the word for
collecting money; asking the SDK to take a screen capture is `요청`. Commonness
only breaks a tie: when two words are both correct, take the one the reader
already knows — `만들어낸 말` over `조어`. Never take a vague common word over
an exact one. A sentence that reads smoothly and says nothing is the worse
failure of the two.

**Prefer the precise term over the short ambiguous one**, especially where the
short one already means something else nearby. In prose write `screen capture`,
not `capture`: `content_map` also has a `capture` field whose values are
`editor`, `editor-play`, and `player`, and a reader cannot tell which one a
bare `capture` meant. Identifiers keep the names they have; this is about the
prose around them.

**Write concretely.** Name the thing, say what happens, give the number.
`재시도는 3회까지` beats `적절히 재시도한다`. Figurative or grand phrasing hides
whether the sentence is even true, and a reviewer cannot check a metaphor. A
short plain sentence with a number in it beats a well-turned one.

None of this asks for more Korean. It asks you to stop making words up. Leave
English where English reads naturally.

Existing text is not a defect to sweep. Words like these are already spread
through comments, documents, and branch names here; that is history, not a
standard. Fix the wording in text you are already writing, and leave the file
around it alone.

## Refactoring

Prefer:
- incremental refactoring
- localized changes
- preserving existing architecture

Avoid:
- unnecessary rewrites
- unrelated cleanup
- aesthetic-only refactors
- broad formatting churn

## Final Rule

Code should be:
- easy to reason about
- easy to modify
- easy to extend
- easy to debug
