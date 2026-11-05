---
version: v2
note: 런 스코프 복수 시나리오 저작 입력 (ARTEL-206/227).
placeholders: [unity_context, game_context, draft, current_scenarios, user_input]
---
Unity context:
{unity_context}

Game context:
{game_context}

Existing draft, if the user is revising one (authoritative; null when starting fresh):
{draft}

Scenarios already in this run (each has a `scenario_id`, `title`, `description`, `case_ids`). When the user asks to change one of these, edit it by returning it with the SAME `scenario_id`. Empty list means the run has no scenarios yet:
{current_scenarios}

Run goal:
{user_input}

Decompose the goal into scenarios. Use `search_test_cases` to find the cases each one needs. For a brand-new scenario, leave `scenario_id` null; to revise one of the existing scenarios above, set its `scenario_id`. Return the scenarios with their `case_ids`.
