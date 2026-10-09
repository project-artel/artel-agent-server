---
version: v19
note: load_skill 도구 description. v19 에서 시스템 프롬프트에서 빼낸 skill 본문을 돌려주는 도구다.
placeholders: [skills]
---
Read one skill: a longer instruction moved out of the system prompt. `name` is one of {skills}. Load a skill before the first time you do what it covers; the system prompt says when each one applies. A skill you loaded earlier may be folded away, or removed when your context is compacted, and then its rules are no longer in front of you: load it again when you need it. `thought` is why you need it now.
