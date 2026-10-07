---
version: v18
note: read_macro 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Read a macro's source. Returns the draft if there is one, the registered source otherwise; a name this run has not seen is looked up in the content map, so a macro an EARLIER run registered comes back here too. The text is verbatim, comments and blank lines included. This is also what licenses `edit_macro`: changing text you have not read is changing text you cannot check. Read before you call an unfamiliar macro, to see what it will send. Rules: see the macro skill.
