---
version: v44
note: Game Function의 사전조건 준비와 읽기 전용 관측 사용 안내. ARTEL-1415.
placeholders: []
description: cheats the game registered, and when one may be used. Load before your first `call_game_function`.
---
# Cheats

The game's developers registered cheats: methods that change the game directly — give gold, set a level, open a stage. Your first message lists the ones this run may call, with their parameters. Call one with `call_game_function`.

## What a cheat is for

Use setup functions to reach the starting state. Perform the action being tested through its required game path. Keep setup out of the action and its expected result.

Judge the TC from the action and result you actually observe, not from a successful cheat call. Follow any explicit TC restrictions on cheats or on how an action must be performed. Report which cheat you used and what you observed. Report PASSED when observations establish the expectation, FAILED when they show a mismatch, and INCONCLUSIVE only when evidence is insufficient to determine whether the expectation was met.

For example, set XP just below the next level threshold, collect the XP orb through player movement, and observe whether the skill selection screen opens.

## Before and after

Use only cheats on the `Cheats now` line of the pulse view. That line lists what your current test case allows and what the game says works right now. A cheat on the `Cheats not now` line is refused by the game, with the reason shown beside it; do not try it — change the game's state first, or reach the precondition another way. A test case may allow fewer cheats than the run has; the refusal names the ones it allows.

After a cheat, look at the screen the answer carries before you act. The game's message says what the handler did; the screen says whether the game shows it. A cheat that answered `ok` and changed nothing on screen is a finding about the cheat, not about the step.

## Targets

A cheat on a component runs on one instance of it. Leave `target` out when there is one instance. When there are several, pass part of the selector shown on the `Cheats now` line — enough to match exactly one. A static cheat takes no target.

Read-only functions return a value without changing the game. Use them before or after the real action. If the result includes a query-N id, cite that id in a query evidence source and copy the returned value exactly.
