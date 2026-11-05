---
version: v26
note: cheats skill (ARTEL-1054). 계약은 workspace 의 .plan/general/2026-10-14-game-functions-wire-contract.md. 규칙은 작업 지시가 정한 넷 - precondition 에만 쓴다, 검증할 값을 cheat 로 만들지 않는다, cheat 뒤에 본다, Not now 를 시도하지 않는다.
placeholders: []
description: cheats the game registered, and when one may be used. Load before your first `run_cheat`.
---
# Cheats

The game's developers registered cheats: methods that change the game directly — give gold, set a level, open a stage. Your first message lists the ones this run may call, with their parameters. Call one with `run_cheat`.

## What a cheat is for

A cheat sets up a **precondition**. When a test case needs a state that would take many steps of play to reach — a full wallet, a later stage, an item in the bag — and the case is not about reaching it, a cheat puts the game there. Use it instead of playing the long way, and instead of reporting `SETUP-FAILED:` when a cheat could reach the state.

A cheat never produces the value a step verifies. If a step expects gold to rise after selling an item, sell the item; giving gold with a cheat and then reading the gold back proves nothing about the game. When the only way to reach the expected result is a cheat, the step has not been tested — say so in the verdict.

## Before and after

Use only cheats on the `Cheats now` line of the pulse view. That line lists what your current test case allows and what the game says works right now. A cheat on the `Cheats not now` line is refused by the game, with the reason shown beside it; do not try it — change the game's state first, or reach the precondition another way. A test case may allow fewer cheats than the run has; the refusal names the ones it allows.

After a cheat, look at the screen the answer carries before you act. The game's message says what the handler did; the screen says whether the game shows it. A cheat that answered `ok` and changed nothing on screen is a finding about the cheat, not about the step.

## Targets

A cheat on a component runs on one instance of it. Leave `target` out when there is one instance. When there are several, pass part of the selector shown on the `Cheats now` line — enough to match exactly one. A static cheat takes no target.
