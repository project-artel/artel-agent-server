---
version: v24
note: verdict_evidence axis(ARTEL-1133) 에서 판정 도구 설명 뒤에 붙는 checks 안내. v23 의 나머지 파일은 그대로 옮겼다(version 줄만 v24).
placeholders: []
---
On a verifying step `checks` is required: per expected item, `expected`, `observed`, `met` (yes/no/unknown) and `sources` — `pulse` (object, member and value as the pulse view printed them), `capture` (the id printed with the screenshot) or `scene`. Cited values are compared with what this run read; a mismatch marks the verdict uncertain. A verdict that contradicts its items is sent back. Say `unknown` rather than guess.
