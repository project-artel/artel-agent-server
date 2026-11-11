---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: [skills]
---
Read one skill: a longer instruction moved out of the system prompt. `name` is one of {skills}. Load a skill before the first time you do what it covers; the system prompt says when each one applies. A skill you loaded earlier may be folded away, or removed when your context is compacted, and then its rules are no longer in front of you: load it again when you need it. `thought` is why you need it now.
