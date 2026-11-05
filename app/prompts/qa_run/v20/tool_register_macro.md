---
version: v20
note: register_macro 도구 description 을 코드에서 옮김. 500자 이하. v20 은 docstring 이 없는 진입점 def 를 거절한다는 것을 더했다 (ARTEL-984).
placeholders: []
---
Make a draft callable by `run_macro`, in this run and later ones. Refused if anything in the draft is not allowed, or if its entry `def` has no docstring — the summary later runs choose by. It is written to the content map and is callable for the rest of THIS run either way. A name already registered is updated in place, keeping its `screen` relations. `screens` adds screens by ID — the number in your scene view's content map line, never a scene name. See the macro skill.
