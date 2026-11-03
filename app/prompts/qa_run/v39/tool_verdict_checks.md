---
version: v39
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
On a verifying step `checks` is required: per expected item, `expected`, `observed`, `met` (yes/no/unknown) and `sources`. A thing on screen is `pulse` with its `object` path as the pulse view printed it, plus `member` and `value` when you read a value; `scene` is only the scene name; `capture` is the id printed with a screenshot. Cited values are compared with what this run read. A verdict that contradicts its items is sent back. Say `unknown` rather than guess.
