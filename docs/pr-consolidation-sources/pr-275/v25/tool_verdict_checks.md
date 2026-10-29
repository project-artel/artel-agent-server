---
version: v25
note: verdict_evidence axis(ARTEL-1133) 에서 판정 도구 설명 뒤에 붙는 checks 안내. v24 의 나머지 파일은 그대로 옮겼다(version 줄만 v25).
placeholders: []
---
On a verifying step `checks` is required: per expected item, `expected`, `observed`, `met` (yes/no/unknown) and `sources`. A thing on screen is `pulse` with its `object` path as the pulse view printed it, plus `member` and `value` when you read a value; `scene` is only the scene name; `capture` is the id printed with a screenshot. Cited values are compared with what this run read. A verdict that contradicts its items is sent back. Say `unknown` rather than guess.
