---
version: v40
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Store a macro draft: a named sequence of actions you can call again, once you have worked it out by hand and will need it again — dealing a card into a slot, walking a dialogue to its end. The source must hold a `def` named exactly `name`, opening with a docstring that says what it does (registering requires it). The draft is parsed and you are told what it would do, or what is wrong. Nothing is registered and nothing runs. Grammar and examples: read the macro skill first.
