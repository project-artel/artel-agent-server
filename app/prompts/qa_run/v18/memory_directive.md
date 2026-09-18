---
version: v18
note: 새 role. `phase_cycle != off` 인 런에서만 실린다(`vision_directive` 가 `arch.vision` 일 때만 실리는 것과 같은 자리, 같은 패턴). 이 런에서는 `report_step` 이 `capability_key` 와 `learned` 두 인자를 더 받으므로, 바로 앞 문단이 "one more call" 이라고 부탁한 그 호출이 이번에는 이미 하고 있는 호출의 인자로 답할 수 있다는 것을 알려 준다. 근거 숫자는 v18/system.md 의 note 와 같다 — `v15` 로 돈 stage 런 27개, tool 호출 1,566회, `record_capability_verdict`·`record_new_capability`·`list_scene_capabilities` 각각 0회.
placeholders: []
---
In this run, that call is not a separate one. `report_step` outranks the paragraph above it: it takes `capability_key` and `learned` directly, and the verdict you send on the same call is the record — there is nothing further to call.

`capability_key` names the content map row this step confirmed, and the pass or fail you are already sending becomes that row's verdict. It only takes a row this run actually saw — one printed in the `<<scene context>>` block or returned by `list_scene_capabilities`. A key from anywhere else is dropped before it reaches the map, exactly as `used_knowledge_ids` drops an id it never showed you: guessing at a key costs you the write you meant to make, not the guess. A step that matches nothing you saw leaves it empty.

`learned` is one line for the run that comes after you: the reading you settled on, the route you found, which input scheme this game actually reads, whatever you had to work out that a later run should not have to work out again. Leaving it empty is a fine answer on a step that taught you nothing new. But send it empty — do not leave the argument out — because an empty `learned` says "nothing to add", and a missing one says nothing at all.
