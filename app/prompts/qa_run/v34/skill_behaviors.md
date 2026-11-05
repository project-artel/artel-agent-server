---
version: v34
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
description: behaviors the game registered, which run inside the game while you think. Load before your first `start_behavior`.
---
# Behaviors

A real-time game does not wait for you. While you think about the next move, the character stands still, the enemy attacks, and a test case that needs steady input — keep attacking, keep running, dodge — cannot be carried to its end one tool call at a time. The game's developers registered behaviors for this: code that gives input every frame inside the game. Your first message lists the ones this run may start.

## What a behavior is for

Start a behavior for **per-frame input** a step needs but does not verify: keep the hero attacking while you watch whether the boss's health bar drops, keep walking while you check that the map loads ahead. Then keep observing. A behavior does not judge anything; you still read every result and report every verdict.

Never use a behavior for a step that verifies the controls themselves. If a step checks that pressing attack swings the sword, press it yourself — a behavior drives the game from inside and does not go through the input you are testing. Stop any behavior that uses the same controls before such a step.

## Running, ending, stopping

`start_behavior` answers with the behavior's id. While it runs, the pulse view shows a `Behavior running` line with its seconds, its limit and what it reports. It ends by itself when its time runs out, when its work is done, when its target disappears, or when it fails; the end reaches you appended to a later tool result as `Behavior ended`, with the reason. Do not assume it is still running after that line.

A behavior may run across steps and across test cases. `report_step` lists the behaviors still running. When the next test case does not allow one, it is stopped for you before that case's first step. Stop one yourself with `stop_behavior` when the step no longer needs it, and give it more time with `extend_behavior` when it still does.

Use only behaviors on the `Behaviors now` line. One on the `Behaviors not now` line is refused by the game with the reason shown; do not try it. The game runs at most four behaviors at once.

## Targets

A behavior on a component runs on one instance of it. Leave `target` out when there is one instance; otherwise pass part of the selector shown on the `Behaviors now` line, enough to match exactly one.
