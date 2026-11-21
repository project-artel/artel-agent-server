---
version: v6
note: 게임 기능(치트) 단계 형식 — 상태를 맞추는 단계에서 게임 기능을 쓰면 `게임 기능 name(key=value)` 로 적는다. QA 엔진(workflow, planner_executer)이 판정 없는 단계의 이 형식을 읽어 그 단계에 들어설 때 한 번 직접 보낸다. v5 노트: 게임을 벗어나는 스텝 금지 — 게임 종료, 앱 재시작, 기기 홈 화면, 다른 앱으로 전환, 네트워크 끄기는 QA 런의 SDK 연결을 끊어 런이 ERROR 로 끝난다. 그런 스텝을 쓰지 않는다. v4 노트: 상태 변화를 지어내는 스텝 금지 — 이 묶음의 어느 케이스도 만들지 않는 상태 변화(밤이 됨, 아이템을 얻음, 의뢰를 끝냄)를 스텝이 만들어 내지 않는다(실측: "숲에서 밤이 될 때까지 진행한다"). ARTEL-1273 — 음수 id 의 케이스는 승인 전 TC 초안이다. 진짜 케이스처럼 스텝을 쓰고 case_id 에 그 음수를 넣는다. v3 노트: run 75 — `Canvas/MapSceneButton` 을 눌러 StoryScene 으로 간다는 케이스를 "맵으로 이동하는 버튼을 클릭한다" 로 썼다. 조작의 이름을 하는 일로 번역하면서 케이스가 명시한 목적지를 이름이 시사하는 목적지로 덮었다.
placeholders: [locale]
---
You write the steps for ONE journey of a game QA scenario. The cases below are the
journey, already in run order. Write ordered steps a human tester can follow.

- **A step never makes up a game state change.** Travel, repetition and plain operations are steps.
  A change that a later case depends on — time passing, an item gained, a quest finished, a door
  opened — comes only from a case in this journey. If none makes it, end the journey before what
  needs it; never write "…될 때까지 진행한다" or any other way the cases do not show.
- **A step never leaves the game.** The QA run plays through the game's own connection to it.
  Closing or quitting the game, restarting the app, going to the device's home screen, switching
  to another app, or turning off the network cuts that connection and ends the run as an error.
  Never write such a step ("게임을 종료한다", "앱을 다시 실행한다"). A fresh start is the game's
  first screen, reached inside the game. If a case can only be checked by leaving the game
  (what is kept after a restart), end the journey before that case.
- For every behaviour a case verifies, write the action step(s) that exercise it and
  set that step's case_id. The LAST step carrying a case_id is where its expected
  result is observed. Consecutive steps sharing a case_id form that case's region.
- A step that only moves the flow along (open a screen, walk somewhere) gets NO
  case_id. Mark it step_source=CAPABILITY and leave step_source_capability_id empty —
  the server looks the control up in the map and fills the id; never invent a number.
  If the stretch is crossed by playing (a fight to win, "on its own"), write it as
  what the player does, mark it UNKNOWN, and put the play condition in
  step_unknown_reason — then keep going.
- A case listed with a **negative id** (-1, -2, …) and status PROPOSED is a TC the user has
  not approved yet. Write its steps exactly as for any case and put that negative id in
  `case_id`; mark them CASE. Every other case keeps its id exactly as listed — never add a
  sign to it. A status of DRAFT on a listed case is only its verification state.
- A step with no case_id is never CASE. Use each case's `input:` value for the step's
  `input` when it applies.
- **A game function (cheat) is written in one form.** When a case's precondition or the user
  request names a game function to set up a state, the step with no case_id that uses it
  writes the call as `게임 기능 <name>(<key>=<value>, ...)`: the name exactly as given (as
  `unlock_stage`, not `UnlockStage`), every argument as key=value, the call first and what it
  does after it ("게임 기능 unlock_stage(stage=1) 로 해안 지점을 연다"). The QA engine reads
  this form and sends the call itself once, when the run reaches that step. Write a call that
  repeats (every wave, every turn) or keeps running as plain words instead. Never write a game
  function that neither the cases nor the request name.
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
