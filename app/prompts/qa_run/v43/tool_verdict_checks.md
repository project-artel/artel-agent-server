---
version: v43
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
On a verifying step `checks` is required: per expected item, `expected`, `observed`, `met` (yes/no/unknown) and `sources`. A thing on screen is `pulse` with its `object` path as the pulse view printed it, plus `member` and `value` when you read a value; `scene` is only the scene name; `capture` is the id printed with a screenshot. Cited values are compared with what this run read. A verdict that contradicts its items is sent back. Say `unknown` rather than guess.
