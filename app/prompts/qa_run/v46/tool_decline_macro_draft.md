---
version: v46
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Drop the macro draft offered for a successful TC attempt, instead of registering it. Use this ONLY when replaying it would do something wrong, such as a press count that depends on this run's luck or a target absent from the next build. Normally keep a reusable draft with `register_macro`. `name` is the draft's name. `reason` names what would go wrong when replayed; an empty `reason` is refused. A dropped draft is not offered to later runs.
