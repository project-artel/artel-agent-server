---
version: v42
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Drop the macro draft offered for a successful TC attempt, instead of registering it. Use this ONLY when replaying it would do something wrong, such as a press count that depends on this run's luck or a target absent from the next build. Normally keep a reusable draft with `register_macro`. `name` is the draft's name. `reason` names what would go wrong when replayed; an empty `reason` is refused. A dropped draft is not offered to later runs.
