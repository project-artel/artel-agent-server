---
version: v39
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Read a macro's source. Returns the draft if there is one, the registered source otherwise; a name this run has not seen is looked up in the content map, so a macro an EARLIER run registered comes back here too. The text is verbatim, docstring, comments and blank lines included. This is also what licenses `edit_macro`: changing text you have not read is changing text you cannot check. Read before you call an unfamiliar macro, to see what it will send. Rules: see the macro skill.
