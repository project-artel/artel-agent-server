"""Folding stale scene views out of what the model reads.

Every QA tool result carries a full scene view (`SceneMemory.render` in
`app/qa/scene.py`), and every tool message stays in the conversation forever —
`observe_scene` puts one there directly, and every acting tool does too, by way
of the shared `_run` in `app/agents/qa/tools.py`. A run of even a handful of
steps ends up with dozens of near-identical dumps in context by the time it
matters most: late in the run, with the least room left to reason.

`fold_stale_scenes` is the fix. Called right before a message list goes to the
model, it collapses the scene view inside every tool message except the newest
`keep`, leaving a short, honest placeholder in its place. It is model-input-only
by construction: it takes a list and returns a new one, so a caller can apply it
to what a model call is about to see without touching anything else that reads
the same messages — the WebSocket timeline, qa_log, and the console logger in
`app/agents/qa/runner.py` all read the channel or the logger directly, never
this function's output, so they keep the full text regardless.

Wiring: `QaRunner.run` builds its agent with `langchain.agents.create_agent`
and drives it with `agent.astream(...)`, so there is no per-turn message list
the caller re-passes — LangGraph owns it internally for the run. The hook point
is `create_agent(..., middleware=[...])`: a `wrap_model_call` middleware
receives a `ModelRequest` (its `.messages` excludes the system message) and a
`handler`, and can call `request.override(messages=fold_stale_scenes(request.messages))`
before invoking `handler(request)`. That changes only what the one model call
receives, not the graph's own state, so nothing has to survive a checkpoint or
be un-done afterwards. `runner.py` wires this in.
"""

from __future__ import annotations

import re

from langchain_core.messages import BaseMessage, ToolMessage

from app.qa.scene import SCENE_VIEW_END, SCENE_VIEW_START_PREFIX, SCENE_VIEW_START_SUFFIX

# How many of the newest scene views survive folding, in full — counted across
# the whole message list, not per tool or per step. One tunable, so raising it
# back up if a scenario turns out to need more lookback is a one-line change.
#
# One, not two, since the live view: `SceneMemory.render_now` is appended to
# every model call, so the current scene is always in front of the model without
# a tool result having to carry it. What the newest tool view still adds is the
# scene AT the moment an action landed, tied to that action's outcome lines —
# worth one. A second would be two stale snapshots under a fresh one.
DEFAULT_KEEP_SCENES = 1

# Matches exactly what `SceneMemory.render` wrapped its output in: the start
# marker (which carries the observation number), the view body, and the end
# marker. `re.DOTALL` so the body's newlines count as "any character" — the
# view is always multi-line.
_VIEW_PATTERN = re.compile(
    re.escape(SCENE_VIEW_START_PREFIX)
    + r"(?P<at>\d+)"
    + re.escape(SCENE_VIEW_START_SUFFIX)
    + r".*?"
    + re.escape(SCENE_VIEW_END),
    re.DOTALL,
)


def _placeholder(at: str) -> str:
    # Deliberately not built from `SCENE_VIEW_START_PREFIX`/`SUFFIX`: reusing that
    # syntax here would make a folded message look, to the regex above, like it
    # might contain a fresh view worth re-matching. Plain text reads clearly as
    # "not a live marker" and is what the model needs anyway — which observation
    # this was, that it is stale, and what to do about it.
    return (
        f"[scene view from observation {at} folded — it is stale, the scene has "
        "moved on since then. Call observe_scene if you need to see it again.]"
    )


def _fold_content(content: str) -> str:
    """Replace every marked view in `content` with a placeholder.

    A tool message carries at most one view under the current tools, but this
    folds every match it finds rather than assuming that, so it stays correct
    if that ever changes.
    """
    return _VIEW_PATTERN.sub(lambda match: _placeholder(match.group("at")), content)


def fold_stale_scenes(
    messages: list[BaseMessage], keep: int = DEFAULT_KEEP_SCENES
) -> list[BaseMessage]:
    """Return `messages` with all but the newest `keep` scene views folded.

    Expects the message list a LangChain tool-calling agent hands to a model
    call: a mix of `HumanMessage`, `AIMessage`, and `ToolMessage`, in
    chronological order. Only `ToolMessage.content` is ever inspected — a
    scene view can only appear there, since it comes from `SceneMemory.render`
    by way of `observe_scene`/`_run` in `app/agents/qa/tools.py`. Every other
    message, and every `ToolMessage` that carries no view (a failed action, an
    unanswered look), is returned unchanged.

    A "view" is the exact span between the markers `SceneMemory.render` puts
    around its own output — never a guess at where `scene: ...` text starts or
    ends. That guarantees a fold either removes a whole view or none of it, and
    it means the action-outcome lines `_run` writes above the view and the
    operator block `with_operator_messages` (see `app/qa/channel.py`) appends
    below it are untouched: the fold only ever replaces the marked span.

    `keep` counts views across the WHOLE list, most-recent first — not per
    tool call or per scenario step. Everything past `keep` gets replaced with
    a short placeholder naming the observation it was and telling the model to
    call `observe_scene` again if it needs that view back.

    Pure: never mutates `messages` or any message inside it. Returns a new
    list; messages that need no change are the very same objects, and folded
    ones are shallow copies (`model_copy`) with new `content`. Idempotent for
    a fixed `keep`: a message this function has already folded carries no
    markers, so folding the result again finds the same `keep` newest views
    and changes nothing further.
    """
    result: list[BaseMessage] = list(messages)
    kept = 0
    # Newest first, so "keep the newest `keep`" is a plain counter; the
    # returned list keeps the input's original order since only values at
    # existing indices are replaced.
    for index in range(len(result) - 1, -1, -1):
        message = result[index]
        if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
            continue
        if not _VIEW_PATTERN.search(message.content):
            continue
        kept += 1
        if kept <= keep:
            continue
        result[index] = message.model_copy(update={"content": _fold_content(message.content)})
    return result
