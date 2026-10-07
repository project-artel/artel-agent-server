---
version: v18
note: write_macro 도구 description 을 코드에서 옮김. 500자 이하. 문법·예시·수명주기는 macro skill 로 옮겼다.
placeholders: []
---
Store a macro draft: a named sequence of actions you can call again. Use it once you have worked a sequence out by hand and will need it again — dealing a card into a slot, walking a dialogue to its end. The source must hold a `def` named exactly `name`; its parameters are what `run_macro` asks for. The draft is parsed and you are told what it would do, or what is wrong with it. Nothing is registered and nothing runs. Grammar, examples and the rest of the lifecycle: read the macro skill first.
