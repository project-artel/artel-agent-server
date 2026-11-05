# 2026-07-29 — 지난 씬 뷰 접기 (fold stale scene views from model input)

- Date: 2026-07-29
- Jira: ARTEL-180
- Status: Draft

## Goal

Every QA tool result carries a full scene view (`SceneMemory.render`), and every
tool message stays in the conversation forever. A 10-step run accumulates dozens
of near-identical scene dumps in context, which degrades the agent late in a run.

Fold the scene body out of all but the most recent `N` tool messages, right
before the message list goes to the model — leaving a short, honest placeholder
in its place. Everything else in the tool result (action outcome lines, the
operator block) must survive untouched.

## Non-goals

- Do not change what the WebSocket timeline (LOG/CHAT), qa_log, or the console
  logger in `runner.py` show — those keep the full text. Only the model's input
  changes.
- Do not truncate a view by character count — fold the whole view or none of it.
- Do not touch `app/agents/qa/prompt.py`, `agent.py`, `schemas.py`, `__init__.py`,
  `app/config.py`, `app/main.py`, `app/api/**`, `app/qa/service.py`,
  `app/qa/schemas.py`, `pyproject.toml`, `Dockerfile`, or anything under
  `app/prompts/` — owned by the ARTEL-179 worker, editing concurrently in a
  sibling worktree.
- Minimal footprint in `app/agents/qa/runner.py`: the coordinator lifted the
  no-edit restriction on this file so the fold can be wired end-to-end, but
  ARTEL-179 is rewriting `SYSTEM_PROMPT`/prompt assembly in the same file at the
  same time. Touch as few lines as possible to keep their merge cheap.

## Context / Constraints

- `app/qa/scene.py` (`SceneMemory.render`) builds the view: an
  "actionable" list, an "on screen" list, and every unchanged observable,
  reprinted in full every call.
- `app/agents/qa/tools.py`: `observe_scene` and the shared `_run` (every acting
  tool routes through it) both append `channel.scene.render(...)` to the tool
  result, with `with_operator_messages` (see `app/qa/channel.py`) appending an
  operator block afterward when the operator spoke mid-run.
- `app/agents/qa/runner.py` builds the agent with `langchain.agents.create_agent`
  and drives it with `agent.astream(...)`, all within one call — the message
  history that accumulates is internal to the LangGraph run, not something the
  caller re-passes turn by turn. `create_agent` accepts `middleware=[...]`, and
  `AgentMiddleware` supports a `wrap_model_call` hook that receives a
  `ModelRequest` (with `.messages`, excluding the system message) and a
  `handler`; `request.override(messages=...)` swaps what actually reaches the
  model for that one call without touching the graph's persisted state. This is
  the wiring point: model input only, nothing else observes it.
- Folding needs a reliable way to find exactly where a rendered view starts and
  ends inside a tool message that may also carry action-outcome lines above it
  and an operator block below it. Guessing off `"scene: "` text is fragile (nothing
  stops a game's own scene name or an observable value from containing it).
  Since this repo owns both the producer (`scene.py`) and the consumer
  (`context.py`), the plan adds an explicit, honest marker pair around the
  view in `render()`'s own output, carrying the observation number, so folding
  never has to guess.

## Approach (Checklist)

- [x] **Step 0: Recon** — read `scene.py`, `tools.py`, `channel.py`, `runner.py`
  (read-only at first, then editable per coordinator's scope change),
  `AGENTS.md`, workflow/testing/commit docs, existing tests.
- [x] **Step 1: Implementation**
  - `app/qa/scene.py`: wrap `render()`'s returned text in a start marker
    (`<<scene view N>>`) and end marker (`<<end scene view>>`), via small
    public constants (`SCENE_VIEW_START_PREFIX`, `SCENE_VIEW_START_SUFFIX`,
    `SCENE_VIEW_END`) so `context.py` builds its match pattern from the same
    source rather than duplicating literals.
  - `app/agents/qa/context.py` (new): `DEFAULT_KEEP_SCENES = 2` and
    `fold_stale_scenes(messages, keep=DEFAULT_KEEP_SCENES) -> list`. Pure
    function: scans `ToolMessage` content for the marker pair, keeps the
    newest `keep` views intact (counting across the whole list), and replaces
    the exact marked span in every earlier one with a placeholder that names
    the observation and says to call `observe_scene` again. Returns a new
    list; untouched messages are the same objects; folded ones are
    `model_copy` shallow copies with new `content`.
  - `app/agents/qa/runner.py`: add a `wrap_model_call` middleware (built via
    the `@wrap_model_call` decorator on a small module-level function, sync —
    `fold_stale_scenes` does no I/O) that calls
    `request.override(messages=fold_stale_scenes(request.messages))` before
    `handler(request)`, and pass it as `middleware=[...]` to `create_agent`.
    Keep the diff to the smallest possible number of lines: one new
    import, one new small function/middleware object, one changed call site.
- [x] **Step 2: Tests**
  - `tests/test_qa_scene.py`: the render output carries the start/end markers;
    update the one test that asserted the exact tail of `render()`'s output.
  - New `tests/test_qa_agents_context.py`:
    - only the newest `keep` views survive in full
    - older ones become the placeholder (names the right observation)
    - action-outcome lines and the operator block survive folding intact
    - a message with no scene view is returned unchanged (same object)
    - non-`ToolMessage` messages pass through unchanged
    - folding is idempotent
    - regression: total content length across N tool messages stops growing
      linearly as N rises past `keep`
  - New/updated runner test proving the fold applies in a real `astream` run
    (build a fake `BaseChatModel` that returns canned tool calls, monkeypatch
    `build_chat_model`, drive a couple of `observe_scene` calls past `keep`,
    and assert the model actually received the placeholder rather than raw
    scene text on the later turns).
- [x] **Step 3: Rollout / Rollback** — no flag; behavior-preserving improvement
  gated purely by `DEFAULT_KEEP_SCENES`. Rollback is `git revert` of the
  commits, or bumping `DEFAULT_KEEP_SCENES` very high to make folding a no-op
  in practice.

## Validation

- **Commands to run:**
  `cd /Users/jeong-yunseong/development/asm/artel/dev/.worktrees/ARTEL-180 && /Users/jeong-yunseong/development/asm/artel/dev/artel-agent-server/.venv/bin/python -m pytest -q`
- **Expected output:** 111 pre-existing + new tests, all passing.

## Risks & Rollback

- **Risks:**
  - Concurrent edit to `runner.py` by the ARTEL-179 worker — merge conflict
    expected and accepted per the coordinator; kept the touched region small
    and documented exact lines in the final report.
  - `wrap_model_call` sync-vs-async: `runner.run` uses `agent.astream`; a
    sync `wrap_model_call` middleware must still work under async execution —
    verified empirically before committing to the approach.
  - Adding markers to `render()`'s output changes exact-string test
    expectations elsewhere; searched the test suite for exact `endswith`/
    equality checks on rendered scene text before changing it.
- **Rollback steps:** `git revert` the commits touching `context.py`,
  `scene.py`, `tools.py` (if touched), `channel.py` (if touched), and the
  `runner.py` wiring hunk.

## Open Questions

- None blocking; `DEFAULT_KEEP_SCENES = 2` matches the value suggested in the
  issue's design decisions.
