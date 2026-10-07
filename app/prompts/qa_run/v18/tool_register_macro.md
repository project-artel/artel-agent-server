---
version: v18
note: register_macro 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Make a draft callable by `run_macro`, in this run and later ones. The draft is parsed again and refused if anything in it is not allowed; the refusal names what to write instead. It is written to the content map and is callable for the rest of THIS run either way. A name already registered is updated in place, keeping its `screen` relations. `screens` names more screens by ID — the number in your scene view's content map line, never a scene name — and only ADDS. See the macro skill.
