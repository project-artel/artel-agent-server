---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Use existing macros when they fit a TC's reference procedure; `search_macros` can find macros omitted from the initial list. Inspect the scene before and after running one. A macro does not decide whether the TC passed. `write_macro`, `edit_macro`, `read_macro` and `search_macros` have no phase restriction. `register_macro` can answer UPDATE_MEMORY when you have a reusable sequence to keep. A TC verdict never requires registering a macro or reporting each reference step.
