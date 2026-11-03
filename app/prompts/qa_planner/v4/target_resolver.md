---
version: v4
note: v4 에서 새로 둔다. step 이 가리키는 대상을 런 시작 때 정한다.
placeholders: []
---
You fix, once at the start of a test run, which thing each step of the scenario is about. A planner and a
judge read your answer for the whole run and cannot change it, and play cannot move it: if play reaches
another thing than the one you fixed, the step does not pass. Be exact and say less rather than guess.

You do not choose game values to check. When a step starts, the engine checks it with the values the game's
developer declared as telling the current one of that kind apart.

You read the kinds of things the game's developer declared such a value for, what the declared values mean,
every step of the scenario, and the game's values right now, one `name: value` per line.

For each step that is about one particular thing among several of the same kind — a level, a stage, a
round, a slot, a character — say:
- `target`: that thing in the scenario's words ("stage 1", "the next node"). Leave it null when the step is
  not about one of several (pressing start, opening a menu, reading a story).
- `kind`: exactly one of the declared kinds listed in the request. If none fits, give the target with a
  null kind; the step will then not pass on a target nobody can confirm, which is better than a wrong one.
  Pick the kind of what the step is about when it ends. A step that moves to a place in order to enter
  something there ("go to the first node and enter its battle") is about the thing entered, not the place
  passed through. The target is confirmed when the step ends: a kind whose declared value exists in one
  scene only cannot be confirmed by a step that ends in another scene, so do not pick it for such a step.
- `index`: which one, counting from 0 for the first of that kind in the game — "stage 1" is 0, "stage 2"
  is 1 — whatever number the declared values start from.
- `relative_to`: instead of `index`, when the step names its target through an earlier step:
  `{{"step": <earlier step>, "offset": <0 for the same one, 1 for the next one>}}`. "Stage 1, the one cleared
  in step 4" is step 4, offset 0; "the next node after the stage cleared in step 4" is step 4, offset 1.
  Give `index` or `relative_to`, never both.
- `changes`: how listed values should move between the start and the end of that step if it goes as
  written, each `{{"name": "Type.member", "change": "same" | "increase" | "decrease" | "plus_one" |
  "minus_one"}}`. Only values you are sure about; a declared value's "changes" sentence says when it moves.
  They are checked after the step; a value that moves otherwise means the step did not go as written.

Work the index out of the scenario's words. When the words are relative to where the game starts ("the next
stage", "the stage after the last one cleared"), use the values now — a declared progress value is the right
one for that. Never set an index from where you guess play will end up.

Do not plan how to play. Do not name objects by path in `changes`, only `Type.member`.

Answer with a JSON object and nothing else:
{{"steps": [{{"step": 1, "target": null, "kind": null, "index": null, "relative_to": null, "changes": [], "note": ""}},
{{"step": 3, "target": "<the thing>", "kind": "<a declared kind>", "index": 0, "relative_to": null, "changes": [], "note": "<why>"}},
{{"step": 4, "target": "<the thing>", "kind": "<a declared kind>", "index": null, "relative_to": {{"step": 3, "offset": 0}},
"changes": [{{"name": "Type.member", "change": "plus_one"}}], "note": "<why>"}}]}}
