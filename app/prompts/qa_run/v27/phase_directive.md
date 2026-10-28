---
version: v27
note: TC 검증 시도별 판정과 참고 step 분리. 사용자 질문 대기와 별도 issue 규칙 반영.
placeholders: []
---
The run uses OBSERVE, DECIDE (if available), ACT, VERIFY and UPDATE_MEMORY. This cycle applies to a TC verification attempt, never to each reference step. Observe and act as many times as needed. `report_test_case` supplies VERIFY. After a verdict, answer UPDATE_MEMORY with `record_capability_verdict`, `record_new_capability`, `record_knowledge`, `update_knowledge`, `link_knowledge`, `forget_knowledge`, `unlink_knowledge` or `skip_memory_update` before the next cycle. Write only useful discoveries; if none, give `skip_memory_update` an honest `reason`. If a tool is refused, follow the phase guidance in the response.

`observe_scene`, `start_test_case`, `ask_user`, `reply_to_operator`, `report_issue`, `finish_run`, `compact_context`, `pause_game_time`, `resume_game_time`, `include_screen_selector` and `exclude_screen_selector` remain available without a phase restriction.
