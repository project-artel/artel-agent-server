---
version: v38
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Change part of a macro draft by replacing text. `old_text` is matched as plain text and has to appear EXACTLY ONCE; nothing happens if it appears zero times or more, and you are told which — a line number would change the wrong line silently. Call `read_macro` first; this refuses a macro this run has not read. If the name is registered with no draft, the registered macro is copied into a draft and the registered one is left as it was. Grammar and examples: see the macro skill.
