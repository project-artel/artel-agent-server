---
version: v5
note: v5는 전 건 in/out 판정을 먼저 요구한다 (ARTEL-404). 턴 메시지 구성은 v4와 같다.
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

Decompose the goal into scenarios. Pick the cases each one should verify out of the project's case list you were given — it holds every case there is. Lay each scenario out as ordered `steps` (one action each); set `case_id` on the step(s) that exercise a case, and leave it null on purely connective actions. For a brand-new scenario, leave `scenario_id` null; to revise one of the existing scenarios above, set its `scenario_id` and return its full `steps`.
