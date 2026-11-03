---
version: v32
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Drop the macro draft offered for a step you passed, instead of registering it. Use this ONLY when replaying the draft in the next run would do something wrong, such as a press count that depends on this run's luck or a target that is not there on the next build. A passed step's draft is normally registered with `register_macro`. `name` is the draft's name. `reason` names what would go wrong when it is replayed; an empty `reason` is refused. A dropped draft is not offered to later runs.
