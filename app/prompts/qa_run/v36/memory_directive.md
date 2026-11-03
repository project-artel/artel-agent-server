---
version: v36
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
In this run, a content map verdict is not a separate call. `report_step` takes `capability_key` and `learned` directly, and the verdict you send on that call is the record. Where the content_map skill asks for one more `record_capability_verdict` call when you report a step, this outranks it: there is nothing further to call.

`capability_key` names the content map row this step confirmed, and the pass or fail you are already sending becomes that row's verdict. It only takes a row this run actually saw — one printed in the `<<scene context>>` block or returned by `list_scene_capabilities`. A key from anywhere else is dropped before it reaches the map, exactly as `used_knowledge_ids` drops an id it never showed you: guessing at a key costs you the write you meant to make, not the guess. A step that matches nothing you saw leaves it empty.

`learned` is one line for the run that comes after you: the reading you settled on, the route you found, which input scheme this game actually reads, whatever you had to work out that a later run should not have to work out again. Leaving it empty is a fine answer on a step that taught you nothing new. But send it empty — do not leave the argument out — because an empty `learned` says "nothing to add", and a missing one says nothing at all.


