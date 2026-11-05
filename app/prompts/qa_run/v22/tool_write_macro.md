---
version: v22
note: write_macro 도구 description 을 코드에서 옮김. 500자 이하. 문법·예시·수명주기는 macro skill 로 옮겼다. v20 은 진입점 docstring 이 summary 이고 등록에 필요하다는 것을 더했다 (ARTEL-984).
placeholders: []
---
Store a macro draft: a named sequence of actions you can call again, once you have worked it out by hand and will need it again — dealing a card into a slot, walking a dialogue to its end. The source must hold a `def` named exactly `name`, opening with a docstring that says what it does (registering requires it). The draft is parsed and you are told what it would do, or what is wrong. Nothing is registered and nothing runs. Grammar and examples: read the macro skill first.
