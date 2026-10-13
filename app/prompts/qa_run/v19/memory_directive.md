---
version: v19
note: v18 의 memory_directive 를 가져온 것. 첫 문단만 고쳐 쓴다 — v18 에서는 이 문단이 system prompt 의 content map 절 안, "The moment to do it is when you report the step." 바로 뒤에 끼어 "the paragraph above it" 이라고 가리켰지만, v19 에서는 그 절이 `skill_content_map.md` 로 나갔으므로 system prompt 의 `## Reading what the game sends back` 끝에 싣고 content_map skill 을 이름으로 가리킨다. `phase_cycle != off` 인 런에서만 실리는 것은 v18 과 같다. 끝에 빈 줄을 둬서 뒤 문단과 붙지 않게 한다. 근거 숫자는 v18/system.md 의 note 와 같다 — `v15` 로 돈 stage 런 27개, tool 호출 1,566회, `record_capability_verdict`·`record_new_capability`·`list_scene_capabilities` 각각 0회.
placeholders: []
---
In this run, a content map verdict is not a separate call. `report_step` takes `capability_key` and `learned` directly, and the verdict you send on that call is the record. Where the content_map skill asks for one more `record_capability_verdict` call when you report a step, this outranks it: there is nothing further to call.

`capability_key` names the content map row this step confirmed, and the pass or fail you are already sending becomes that row's verdict. It only takes a row this run actually saw — one printed in the `<<scene context>>` block or returned by `list_scene_capabilities`. A key from anywhere else is dropped before it reaches the map, exactly as `used_knowledge_ids` drops an id it never showed you: guessing at a key costs you the write you meant to make, not the guess. A step that matches nothing you saw leaves it empty.

`learned` is one line for the run that comes after you: the reading you settled on, the route you found, which input scheme this game actually reads, whatever you had to work out that a later run should not have to work out again. Leaving it empty is a fine answer on a step that taught you nothing new. But send it empty — do not leave the argument out — because an empty `learned` says "nothing to add", and a missing one says nothing at all.


