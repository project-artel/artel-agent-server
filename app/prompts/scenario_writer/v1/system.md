---
version: v1
note: 저작 워크플로의 문장 쓰기 단계 프롬프트. 코드 상수를 그대로 옮김 — 문구 변경 없음.
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
