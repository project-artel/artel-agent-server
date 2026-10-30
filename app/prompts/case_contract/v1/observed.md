---
version: v1
note: QA 가 통과시킨 TC 를 실행하는 동안 바뀐 게임 값(코드 이름)을 용어집 이름으로 옮겨, 계약이 빠뜨린 '남기는 것'만 더한다. 화면 잠금 · 타이머처럼 금방 되돌아가는 값은 버린다. 요구는 더하지 않는다 — 실행이 무엇을 요구했는지는 값의 변화로 알 수 없다.
placeholders: [terms, start, cases, observed]
---
A QA run executed the test cases below and they passed. While each one ran, the game's own variables changed as listed — code names, `before→after`. Use them to fill in what a TC **leaves behind** when its contract misses it, so the next scenario can count on it.

Add a `leaves` patch only when all of these hold:
- The changed variable is clearly one of the glossary states — by meaning, not by spelling. `Inventory.hasLamp false→true` is 등불 = 1 if the glossary has a has-state 등불.
- It is a lasting game state the TC's action caused: an item gained or lost, a quest taken or finished, a door opened, a counter that grew. Not a UI lock, input flag, timer, animation, camera, or anything that flips back during play.
- The TC's contract does not already leave it.

Value forms, exactly as the glossary uses them: has-state '1' or '0'; value-state one of its options; counter '+N' or '-N', the difference between after and before.
Use the glossary names exactly. Never add a new name, never add `needs`, `not_needs` or `start`. If you are not sure, skip it — a missing fact costs a question later, a wrong one breaks scenarios.

Glossary (name | kind | options | meaning):
{terms}
Start state now: {start}

TCs (id | action → expected || needs | leaves):
{cases}

What changed while each TC ran:
{observed}
