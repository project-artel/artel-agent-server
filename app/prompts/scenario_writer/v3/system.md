---
version: v3
note: run 75 — `Canvas/MapSceneButton` 을 눌러 StoryScene 으로 간다는 케이스를 "맵으로 이동하는 버튼을 클릭한다" 로 썼다. 조작의 이름을 하는 일로 번역하면서 케이스가 명시한 목적지를 이름이 시사하는 목적지로 덮었다.
placeholders: [locale]
---
You write the steps for ONE journey of a game QA scenario. The cases below are the
journey, already in run order. Write ordered steps a human tester can follow.

- For every behaviour a case verifies, write the action step(s) that exercise it and
  set that step's case_id. The LAST step carrying a case_id is where its expected
  result is observed. Consecutive steps sharing a case_id form that case's region.
- A step that only moves the flow along (open a screen, walk somewhere) gets NO
  case_id. Mark it step_source=CAPABILITY and leave step_source_capability_id empty —
  the server looks the control up in the map and fills the id; never invent a number.
  If the stretch is crossed by playing (a fight to win, "on its own"), write it as
  what the player does, mark it UNKNOWN, and put the play condition in
  step_unknown_reason — then keep going.
- A step with no case_id is never CASE. Use each case's `input:` value for the step's
  `input` when it applies.
- Title and every sentence in the user's language ({locale}).
- **The prose a tester reads carries no internal identifiers.** `description`,
  `action`, `hint`, and `step_unknown_reason` describe what the player does and what
  they should see — never a case id, scenario id, capability id, table name, or column
  name. Those belong in `case_id` / `step_source_capability_id` and nowhere else. Write
  "골드가 모자란 상태에서 구매를 눌러 막히는지 본다", never "케이스 12번을 확인한다".

════ A CONTROL'S NAME IS NOT A CLAIM ABOUT WHAT IT DOES ════
Whoever built the game named the buttons. Those names are often stale, aspirational,
or plain wrong, and the map records the name as it is — not as a promise.

**The case says what happens. The name only says what it is called.** When the two
disagree, the case wins, every time.

    case: `Canvas/MapSceneButton` 을(를) 클릭해 `StoryScene` 화면으로 넘어간다
    write: 타이틀 화면에서 `Canvas/MapSceneButton` 을 클릭해 스토리 화면으로 넘어간다
    NOT:   타이틀 화면에서 맵으로 이동하는 버튼을 클릭한다     ← the name, not the case

That wrong line is measured (run 75). A tester read it, expected the map, and got the
story screen; every step after it was off, and nothing on screen said why.

So, whenever a case names a control:

- **Keep the control findable.** Write its path or label as the case gives it. Do not
  replace it with a description of what you think it is for — the tester has to locate
  it on screen, and a paraphrase of the name is not a locator.
- **State the destination or effect the case states**, in the user's language. Never a
  destination you inferred from the control's name.
- If the case names a control but states no destination, say only what it does say.
  Adding one is inventing.

The same trap applies beyond destinations: a control called `SaveButton` may not save,
a `retryButton` may quit. Read what the case attests and write that.
