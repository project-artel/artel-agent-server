---
version: v18
note: load_skill 도구 description. v18 에서 시스템 프롬프트에서 빼낸 skill 본문을 돌려주는 도구다.
placeholders: [skills]
---
Read one skill: a longer instruction moved out of the system prompt. `name` is one of {skills}. Load a skill before the first time you do what it covers; the system prompt says when each one applies. The text stays in your context, so load it again only after your context was compacted. `thought` is why you need it now.
