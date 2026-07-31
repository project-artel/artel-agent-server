---
version: v2
note: 런 스코프 복수 시나리오 저작 입력 (ARTEL-206/227).
placeholders: [unity_context, game_context, draft, user_input]
---
Unity context:
{unity_context}

Game context:
{game_context}

Existing draft, if the user is revising one (authoritative; null when starting fresh):
{draft}

Run goal:
{user_input}

Decompose the goal into scenarios, use `search_test_cases` to find the cases each one needs, and return the scenarios with their `case_ids`.
