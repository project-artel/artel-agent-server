---
version: v6
note: TC 없이 작성해 달라는 명시 요청이면 case_id 전부 null로 흐름만 작성하라는 한 줄 추가. 나머지는 v5와 동일.
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

Decompose the goal into scenarios. Pick the cases each one should verify out of the project's case list you were given — it holds every case there is. Lay each scenario out as ordered `steps` (one action each); set `case_id` on the step(s) that exercise a case, and leave it null on purely connective actions. For a brand-new scenario, leave `scenario_id` null; to revise one of the existing scenarios above, set its `scenario_id` and return its full `steps`. If the goal above explicitly asks for a scenario without test cases, write the flow from the goal and the context instead, with `case_id` null on every step, and say so in `message`.
