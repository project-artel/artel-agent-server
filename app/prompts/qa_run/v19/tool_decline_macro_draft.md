---
version: v19
note: decline_macro_draft 도구 description. 통과한 step 의 초안은 등록하는 것이 기본이고, 이 tool 은 다음 런이 그 초안을 재생하면 무엇이 틀어지는지 이유로 댈 수 있을 때만 쓰게 한다. 500자 이하.
placeholders: []
---
Drop the macro draft offered for a step you passed, instead of registering it. Use this ONLY when replaying the draft in the next run would do something wrong, such as a press count that depends on this run's luck or a target that is not there on the next build. A passed step's draft is normally registered with `register_macro`. `name` is the draft's name. `reason` names what would go wrong when it is replayed; an empty `reason` is refused. A dropped draft is not offered to later runs.
