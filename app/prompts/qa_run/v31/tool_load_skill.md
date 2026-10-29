---
version: v31
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: [skills]
---
Read one skill: a longer instruction moved out of the system prompt. `name` is one of {skills}. Load a skill before the first time you do what it covers; the system prompt says when each one applies. A skill you loaded earlier may be folded away, or removed when your context is compacted, and then its rules are no longer in front of you: load it again when you need it. `thought` is why you need it now.
