---
version: v3
note: 시나리오=steps[] 저작 입력 (재설계 2026-08-08, ARTEL-284).
placeholders: [unity_context, game_context, draft, current_scenarios, user_input]
---
Unity context:
{unity_context}

Game context:
{game_context}

Existing draft, if the user is revising one (authoritative; null when starting fresh):
{draft}

Scenarios already in this run (each has a `scenario_id`, `title`, `description`, and its `steps` — each step an `action` with an optional `case_id`). When the user asks to change one of these, edit it by returning it with the SAME `scenario_id` and its full intended `steps`. Empty list means the run has no scenarios yet:
{current_scenarios}

Run goal:
{user_input}

Decompose the goal into scenarios. Use `search_test_cases` to find the cases each one should verify. Lay each scenario out as ordered `steps` (one action each); set `case_id` on the step(s) that exercise a found case, and leave it null on purely connective actions. For a brand-new scenario, leave `scenario_id` null; to revise one of the existing scenarios above, set its `scenario_id` and return its full `steps`.
