---
version: v19
note: The Skills section of the system prompt, kept in its own file so the run can leave it out when the skills axis is off and the skill bodies are already inlined. Without it, the agent would be told to call a tool it does not have. The list of skills is generated into skill_list from each skill file's description frontmatter, one line per skill in name order, so a new skill is listed without editing this file.
placeholders: [skill_list]
---
## Skills

`load_skill(name)` returns guidance left out of this prompt. Load each at the first moment it applies.

{skill_list}
