---
version: v18
note: The Skills section of the system prompt, kept in its own file so the run can leave it out when the skills axis is off and the skill bodies are already inlined. Without it, the agent would be told to call a tool it does not have.
placeholders: []
---
## Skills

`load_skill(name)` returns guidance left out of this prompt. Load each at the first moment it applies.

- `knowledge_base` — what to record about the game, where it holds, what to link and cite. Load before your first `record_knowledge`, `update_knowledge`, `link_knowledge` or `unlink_knowledge`.
- `content_map` — confirming or refuting the content map's rows. Load before `record_capability_verdict` or `record_new_capability`, and when a step you report matches a capability the scene context listed.
- `held_state` — held input, axes, paused time, and undoing them. Load before `hold_key`, `hold_mouse_button`, `set_input_axis`, `set_input_button` or `pause_game_time`.
