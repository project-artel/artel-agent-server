---
version: v19
note: edit_macro 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Change part of a macro draft by replacing text. `old_text` is matched as plain text and has to appear EXACTLY ONCE; nothing happens if it appears zero times or more, and you are told which — a line number would change the wrong line silently. Call `read_macro` first; this refuses a macro this run has not read. If the name is registered with no draft, the registered macro is copied into a draft and the registered one is left as it was. Grammar and examples: see the macro skill.
