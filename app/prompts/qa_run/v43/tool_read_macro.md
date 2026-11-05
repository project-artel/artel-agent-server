---
version: v43
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Read a macro's source. Returns the draft if there is one, the registered source otherwise; a name this run has not seen is looked up in the content map, so a macro an EARLIER run registered comes back here too. The text is verbatim, docstring, comments and blank lines included. This is also what licenses `edit_macro`: changing text you have not read is changing text you cannot check. Read before you call an unfamiliar macro, to see what it will send. Rules: see the macro skill.
