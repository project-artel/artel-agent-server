---
version: v1
note: 자연어 TC 저작 — 용어집 이름만 써서 TC 마다 필요한 것 · 남기는 것 · 상식 추정을 적는다. 순서는 저작 모델이 정하고, 이 답은 그 문맥과 코드 검사의 재료다.
placeholders: [terms, cases]
---
Describe each TC below with the game-state terms from the glossary ONLY — never invent a name. A program will order TCs by your answer.

For every TC:
- has-terms: requires (present=true must hold / present=false must not hold), produces, removes.
- value-terms: requires_values / sets_values with one of the term's options.
- counter-terms: counters_need (">=" at least, "<" not enough) / counters_add (+ gained, - spent). Only these two
  comparisons exist: "N 이하" (at most N) is "<" N+1, "N 초과" (more than N) is ">=" N+1.
- requires_any: alternatives, one group must hold in full.
- "after doing X" in a precondition means what X leaves behind is required.
- assumed_requires: conditions NOT written anywhere that common sense about games says the action needs (a weapon to defeat an enemy). Use the glossary term that is that thing (a sword for "a weapon"). Only when fairly sure.
- A TC that only observes or is refused changes nothing.
- Being at a screen is not a requirement (see the glossary rule).
- arrives_at: if doing the TC takes the player to a DIFFERENT screen — the expected result says the screen
  changes, or the action is going, entering or leaving somewhere — the screen it ends on, named the way the
  TCs' screen column names it when one fits ("광장"). Empty when the player stays on the same screen.

Glossary (name | kind | options | meaning | aliases):
{terms}

TCs (id | screen | precondition | action | expected):
{cases}
