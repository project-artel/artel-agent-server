---
version: v31
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Read a macro's source. Returns the draft if there is one, the registered source otherwise; a name this run has not seen is looked up in the content map, so a macro an EARLIER run registered comes back here too. The text is verbatim, docstring, comments and blank lines included. This is also what licenses `edit_macro`: changing text you have not read is changing text you cannot check. Read before you call an unfamiliar macro, to see what it will send. Rules: see the macro skill.
