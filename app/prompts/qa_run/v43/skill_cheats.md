---
version: v43
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
description: cheats the game registered, and when one may be used. Load before your first `run_cheat`.
---
# Cheats

The game's developers registered cheats: methods that change the game directly — give gold, set a level, open a stage. Your first message lists the ones this run may call, with their parameters. Call one with `run_cheat`.

## What a cheat is for

Use available cheats to reach or change the state needed for a test quickly. Unless the TC explicitly restricts them, prefer cheats over lengthy manual play. Cheats are not limited to preconditions: do not reject one merely because it changes a state mentioned in the TC or performs part of its test action. Use one instead of reporting `SETUP-FAILED:` when it can reach the needed state.

Judge the TC from the action and result you actually observe, not from a successful cheat call. Follow any explicit TC restrictions on cheats or on how an action must be performed. Report which cheat you used and what you observed. Report PASSED when observations establish the expectation, FAILED when they show a mismatch, and INCONCLUSIVE only when evidence is insufficient to determine whether the expectation was met.

For example, if the TC asks whether clearing Act1 allows the player to enter Act2, use a cheat to clear Act1, then check that the game allows entry and enter Act2. You do not need to win every fight manually. Calling a cheat that jumps straight to Act2 only shows that the cheat reached Act2; it does not establish that clearing Act1 enabled the transition.

## Before and after

Use only cheats on the `Cheats now` line of the pulse view. That line lists what your current test case allows and what the game says works right now. A cheat on the `Cheats not now` line is refused by the game, with the reason shown beside it; do not try it — change the game's state first, or reach the precondition another way. A test case may allow fewer cheats than the run has; the refusal names the ones it allows.

After a cheat, look at the screen the answer carries before you act. The game's message says what the handler did; the screen says whether the game shows it. A cheat that answered `ok` and changed nothing on screen is a finding about the cheat, not about the step.

## Targets

A cheat on a component runs on one instance of it. Leave `target` out when there is one instance. When there are several, pass part of the selector shown on the `Cheats now` line — enough to match exactly one. A static cheat takes no target.
