---
version: v35
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Use existing macros when they fit a TC's reference procedure; `search_macros` can find macros omitted from the initial list. Inspect the scene before and after running one. A macro does not decide whether the TC passed. `write_macro`, `edit_macro`, `read_macro` and `search_macros` have no phase restriction. `register_macro` can answer UPDATE_MEMORY when you have a reusable sequence to keep. A TC verdict never requires registering a macro or reporting each reference step.
