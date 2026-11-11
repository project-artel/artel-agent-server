---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Store a macro draft: a named sequence of actions you can call again, once you have worked it out by hand and will need it again — dealing a card into a slot, walking a dialogue to its end. The source must hold a `def` named exactly `name`, opening with a docstring that says what it does (registering requires it). The draft is parsed and you are told what it would do, or what is wrong. Nothing is registered and nothing runs. Grammar and examples: read the macro skill first.
