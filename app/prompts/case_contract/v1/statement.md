---
version: v1
note: 사용자가 대화에서 한 말(질문의 답 · 그냥 알려 준 것)에서 게임 사실만 골라 TC 계약 수정으로 바꾼다. 범위 · 선호는 계약이 아니라 그 턴의 요청이라 건드리지 않는다.
placeholders: [question, said, terms, start, cases]
---
A user of a game QA tool said something in the scenario-authoring chat. If it states how the game works, turn it into changes to the test-case contracts so the tool remembers it. If it is not a statement about the game — what to include in a scenario, how long, a preference, a greeting, a request — return no patches.

ops:
- needs: TC tc_id needs name==value before it can run.
- not_needs: TC tc_id does NOT need name (drop that requirement).
- leaves: after TC tc_id runs, name=value holds ('+20' for a counter).
- start: name=value holds when a new game starts (no tc_id).

Rules:
- Use the glossary names exactly when one means the thing the user talks about; otherwise a short new name.
- Only what the user said. Do not add what you guess. If the user says they do not know, return no patches.
- A statement about moving between screens ("시작하기를 누르면 광장") is not a contract change unless it says something must hold; return no patches for it.

The tool's last question (may be empty): {question}
What the user said: {said}

Glossary (name | kind | options | meaning): 
{terms}
Start state now: {start}

TCs (id | action → expected || needs | leaves):
{cases}
