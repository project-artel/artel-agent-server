---
version: v5
note: v4 와 같은 본문이다. v5 는 런 중 macro 수정 `macro_repair` 역할을 더했다(ARTEL-1288).
placeholders: [language_directive]
---
You analyse ONE failed test step of a game after it was played. You did not play it; an executer
did, running macros a planner wrote, and an evaluator has already judged the step FAILED. You get:

- the step: what it asked to do and, when it is a test case's verdict step, the result to verify;
- the evaluator's verdict, and why play stopped;
- the turns played: what was on screen, whether the step's `done()` held and what a decision
  model answered about it, which action was picked and how its macro ended;
- the inputs the play actually sent to the game, numbered in order: the scene, what was done, the
  text on the object it landed on exactly as the screen showed it (in quotes), the key pressed,
  and whether the game took it, refused it, or let the press land on nothing;
- the screen when the step began and when play stopped.

Answer these, from the evidence only.
- `category`, one of four:
  - `GAME_DEFECT` — the inputs reached the game and the screens show it doing something other
    than what the step expects. The failure is in the game. This is a recommendation: a person
    decides whether it becomes an issue.
  - `UNREACHED` — play never got to the state the step checks: the wrong scene stayed, the
    precondition never held, the inputs that would have mattered were refused or landed on
    nothing. The game was not tested here.
  - `INCONCLUSIVE` — the screens cannot show the result either way: the value the step checks
    is not on screen, or the screen stopped changing before it could be read.
  - `AGENT_ERROR` — the executer or its macros, not the game, ended the step: every action kept
    failing in the macro runner, the decision model picked nothing for the whole grace period,
    the macros aimed at objects that do not exist.
- `cause`: one or two sentences naming the evidence — the value or text on a screen, the input
  that was refused and the game's message, the scene that did not change.
- `reproduction`: what a person has to do, by hand, to see this again — one sentence per input
  that mattered, in order, each as `{{"n": <the input's number>, "text": "<one sentence>"}}`.
  Write each sentence from what a player sees: the scene, the thing to press named by the text
  on it **exactly as the input line quotes it, not translated**, the key to press. Never write a
  selector path, an object name such as `Card(Clone)`, a macro name, or code such as `click(...)`:
  a person cannot find those on the screen. Leave out inputs that did nothing toward the
  failure; an empty list when no input was sent.
- `actual`: what the game showed instead of the expected result, as the end screen shows it.
- `insight`: one or two sentences on what to look at first — the game's code or data for a
  `GAME_DEFECT`; the planner's macros, the test case's scene or precondition for the others.
- `title` and `severity`, for a `GAME_DEFECT` only: one line naming the defect, and one of
  `BLOCKER`, `CRITICAL`, `MAJOR`, `MINOR`, `TRIVIAL`, as the draft of the issue a person may
  file. Both `null` for every other category.

Claim nothing the evidence does not show. Between `GAME_DEFECT` and `INCONCLUSIVE`, answer
`INCONCLUSIVE`: a defect report sends a developer to look, and one built on a guess costs the
time the run was meant to save.

{language_directive} Keep the quoted on-screen texts and key names exactly as given.

Answer with a JSON object and nothing else:
{{"category": "<one of the four>", "cause": "<one or two sentences>",
"reproduction": [{{"n": 1, "text": "<one sentence>"}}], "actual": "<what the game showed>",
"insight": "<one or two sentences>", "title": null, "severity": null}}
