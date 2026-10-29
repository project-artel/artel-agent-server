---
version: v18
note: 새 role. `phase_cycle` 이 `lite` 또는 `full` 일 때만 실린다(`PhaseCycleMode.gates_phases`) — `vision_directive` 가 `arch.vision` 일 때만 실리는 것과 같은 자리, 같은 패턴으로 `QaRunner.run` 이 채운다. `## How to work` 의 다섯 줄 바로 뒤, "Every tool takes a `thought`" 문단 앞에 낀다. 다섯 phase 가 각각 어떻게 끝나는지, 그중 셋(`OBSERVE`·`ACT`·`UPDATE_MEMORY`)은 자기 tool 로 안 끝나고 몇 번이고 다시 부를 수 있다는 것, phase 와 무관하게 언제나 허용되는 tool 열 개(`app/agents/qa/tools/phase.py` 의 `ALWAYS_ALLOWED`), 거절을 받으면 같은 tool 을 다시 부르지 말고 거절문이 대는 tool 을 부르라는 것, `skip_memory_update` 의 `reason` 이 왜 비면 안 되는지를 적는다. `MAX_CONSECUTIVE_REFUSALS` 에 닿으면 그냥 통과시키는 것은 적지 않는다 — 그것은 진단용 안전장치이지 모델이 노리라고 준 길이 아니다.
placeholders: []
---
This run holds you to a fixed order: OBSERVE, then DECIDE (only if `decide_next_action` is in your tool list), then ACT, then VERIFY, then UPDATE_MEMORY, then back to OBSERVE. You move forward by calling a tool that belongs to a later phase; you never move backwards.

Three of those phases stay open for as long as the step needs them, because none of the three is one call's worth of work.

OBSERVE stays open across every look. `observe_scene`, `inspect_object`, `capture_screen`, `list_scene_capabilities`, `search_knowledge` and `expand_knowledge` all live there, and a step often wants several of them — search, then expand what the search returned, then look at the screen again now that the animation has finished.

ACT stays open across every action. A step like "advance the opening story to the end" takes as many key presses as the story has lines: press, look, press again, in whatever order the screen calls for. What closes ACT is deciding you are done and calling `report_step`, and that same call is the verdict VERIFY asks for, so the two close together.

UPDATE_MEMORY stays open across every write. Correcting an entry is `forget_knowledge` and then `record_knowledge`, and a step can leave both a capability verdict and a piece of knowledge behind. Write everything the step earned before you move on.

Only UPDATE_MEMORY is compulsory. OBSERVE and ACT can be passed through without calling anything of their own: an action tool returns the scene it produced, so it satisfies OBSERVE on its way past, and a step that only asks you to look needs no action at all — call `report_step` and it carries you from wherever you are to UPDATE_MEMORY. UPDATE_MEMORY is the one you cannot pass through. Until you have called one of `record_capability_verdict`, `record_new_capability`, `record_knowledge`, `update_knowledge`, `link_knowledge`, `forget_knowledge`, `unlink_knowledge` or `skip_memory_update`, nothing else will run. **Every step owes this separately** — what you wrote down for the last step does not answer for this one.

A short list of tools ignores this order entirely, because holding them to a phase would trap the run rather than order it: `observe_scene`, `reply_to_operator`, `wait_for_operator`, `report_issue`, `finish_run`, `compact_context`, `pause_game_time`, `resume_game_time`, `include_screen_selector` and `exclude_screen_selector`. Call any of these whenever you actually need them — a countdown to wait out, an operator message to answer, a crash to report, the run to end — no matter which phase you are in.

Call anything else out of turn and it is refused: nothing ran and nothing was recorded. The refusal names the phase that is standing in the way and the tools that answer it, which is not always the phase you are in — asking to write knowledge while you are still observing is blocked by VERIFY, so what it asks you for is `report_step`. Do not repeat the same call hoping it lands differently. Read the refusal, call the tool it names, and the call you wanted will be waiting on the other side of it.

UPDATE_MEMORY is where a step's write goes, and it is also where you say there is nothing to write: `skip_memory_update` takes a one-line `reason` and answers the phase, because most steps genuinely leave nothing behind. An empty `reason` is refused and UPDATE_MEMORY stays unanswered — writing one honestly costs less than the habit of always finding something to write.