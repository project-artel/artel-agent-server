---
version: v1
note: 자연어 TC 저작 — TC 계약을 뽑기 전에 프로젝트의 상태 이름 사전(용어집)을 만든다. 측정(등불 골짜기 E1~E3)에서 이름이 하나로 맞아야 질문의 답이 다른 TC 와 이어졌다.
placeholders: [existing, about, cases]
---
You build the glossary of game state for a game whose test cases (TCs) are written in plain language by its users. A later step will describe every TC only with these terms, and a program will order TCs by them, so the glossary decides what counts as the same thing.

Rules:
- One term per thing that can change on its own. Two things are separate terms when some TC changes one and leaves the other: having a lamp and the lamp being lit are separate if a TC turns the lamp off while you keep it. If no TC ever separates them, they are one term.
- Something switched back and forth (on/off, open/closed, day/night) is kind=value with its options, never two has-terms.
- An amount that is added or spent (coins, ore you can hold several of) is kind=counter.
- Being at a screen or place is not a term — every TC already names its screen and walking is free. The only exception is a place you can enter only once something holds (dark, locked, guarded); then "inside X" is a has-term.
- Use the word the TCs use. Put other words the TCs use for the same thing in aliases.
- start: what holds when a new game begins. From the text when it says so; otherwise plain default (items, quests taken, unlocks: 없음; counts: 0) with basis=default; 'unknown' when neither tells (which option a value starts at, unless the text says).
- List in not_same_as any close term that must stay separate, with the reason.
{existing}
What the user told us about the game: {about}

TCs (id | screen | precondition | action | expected):
{cases}
