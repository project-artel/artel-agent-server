"""Folding stale scene views out of what the model reads.

Every QA tool result carries a full scene view (`SceneMemory.render` in
`app/qa/scene.py`), and every tool message stays in the conversation forever —
`observe_scene` puts one there directly, and every acting tool does too, by way
of the shared `ToolContext.run` in `app/agents/qa/tools/tool_context.py`. A run of
even a handful of
steps ends up with dozens of near-identical dumps in context by the time it
matters most: late in the run, with the least room left to reason.

`fold_stale_scenes` is the fix. Called right before a message list goes to the
model, it collapses the scene view and the pulse view inside every tool message
except the newest `keep`, leaving a short, honest placeholder in its place — in
batches, once more than `DEFAULT_MAX_FULL_VIEWS` full views have piled up, so the
prompt prefix stays the same between batches (see that constant). It is model-input-only
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
from dataclasses import dataclass

from langchain_core.messages import BaseMessage, ToolMessage

from app.agents.qa.knowledge import (
    NEIGHBOUR_BLOCK_END,
    NEIGHBOUR_BLOCK_START_PREFIX,
    NEIGHBOUR_BLOCK_START_SUFFIX,
)
from app.agents.qa.tools.skill_tools import (
    SKILL_BLOCK_END_PREFIX,
    SKILL_BLOCK_END_SUFFIX,
    SKILL_BLOCK_START_PREFIX,
    SKILL_BLOCK_START_SUFFIX,
)
from app.qa.pulse import PULSE_VIEW_END, PULSE_VIEW_START
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

# 지난 view 를 한 번에 몇 개까지 그대로 두었다가 `fold` 하나.
#
# `fold` 는 매 모델 호출마다 graph 가 가진 원본 목록에서 처음부터 다시 계산된다. 그래서 새 view 가
# 올 때마다 바로 앞 view 를 `fold` 하면(종전의 `keep=1`) 매 호출이 직전 호출의 프롬프트 중간을
# 고쳐 쓴다. `cache` 경계는 프롬프트 끝에 하나뿐이라(`app/llm/chat_model.py`) 그동안 써 둔 `cache` 가
# 전부 안 맞고, 대화 전체가 매 호출 정가로 다시 읽힌다 — ARTEL-621 이 없앤 것과 같은 실패다.
#
# 그래서 batch 로 `fold` 한다. 전문으로 남은 view 가 이 수를 넘는 순간에만, 가장 새것 `keep` 개를
# 빼고 전부 `fold` 한다. 그 사이의 호출은 앞을 안 고치고 끝에 덧붙기만 하므로 `cache` 가 맞는다.
# 8 이면 `cache` 를 다시 쓰는 것이 8 호출에 한 번이고, 그 사이 끝에 쌓이는 view 는 많아야 8 개다
# (L1 런 실측으로 view 한 개가 평균 약 7k 자, `run_macro` 는 약 27k 자).
DEFAULT_MAX_FULL_VIEWS = 8

# 자리표를 알아보는 접두. 계측(`app/agents/qa/runner.py`)이 "접힌 것" 과 "전문으로 남은 것" 을
# 세려면 둘을 가릴 단서가 필요한데, 자리표는 일부러 마커 문법을 안 쓰므로(아래 `_placeholder`
# 참고) 문구의 앞머리가 그 단서다. 문구를 고칠 사람이 이것도 같이 보도록 꺼내 둔다.
FOLDED_VIEW_PREFIX = "[scene view from observation "
# `pulse` view 의 `placeholder` 가 시작하는 문구. 위와 같은 이유로 꺼내 둔다.
FOLDED_PULSE_VIEW_PREFIX = "[pulse view "

# Matches exactly what `SceneMemory.render` wrapped its output in: the start
# marker (which carries the observation number), the view body, and the end
# marker. `re.DOTALL` so the body's newlines count as "any character" — the
# view is always multi-line.
#
# `pulse` view 는 `PulseMemory.render` 가 처음부터 `<<pulse>>` 와 `<<end pulse>>` 로 감싸 낸다.
# 끝 마커가 있으므로 그 사이만 바꾸면 되고, 위의 도구 몸통("replay_step_3 ran to the end. …")
# 이나 아래의 `<<scene context>>` 블록은 마커 밖이라 안 건드린다. 머리 줄(`reading N · frame F
# · scene S`)을 따로 잡는 것은 `placeholder` 가 어느 `reading` 이었는지 말하게 하려는 것이다.
#
# 한 패턴에 둘을 나란히 둔다. GAME_STATE 가 오는 빌드에서는 `pulse` view 가 scene view 마커
# **안에** 들어가는데(`SceneMemory.render`), 왼쪽부터 찾으므로 scene view 가 먼저 걸려 안의
# `pulse` view 까지 통째로 먹는다. 따로 찾으면 한 view 를 둘로 세고, 이미 바꾼 자리를 또 바꾼다.
_VIEW_PATTERN = re.compile(
    re.escape(SCENE_VIEW_START_PREFIX)
    + r"(?P<at>\d+)"
    + re.escape(SCENE_VIEW_START_SUFFIX)
    + r".*?"
    + re.escape(SCENE_VIEW_END)
    + "|"
    + re.escape(PULSE_VIEW_START)
    + r"\n(?P<head>[^\n]*)"
    + r".*?"
    + re.escape(PULSE_VIEW_END),
    re.DOTALL,
)


def _placeholder(at: str) -> str:
    # Deliberately not built from `SCENE_VIEW_START_PREFIX`/`SUFFIX`: reusing that
    # syntax here would make a folded message look, to the regex above, like it
    # might contain a fresh view worth re-matching. Plain text reads clearly as
    # "not a live marker" and is what the model needs anyway — which observation
    # this was, that it is stale, and what to do about it.
    return (
        f"{FOLDED_VIEW_PREFIX}{at} folded — it is stale, the scene has "
        "moved on since then. Call observe_scene if you need to see it again.]"
    )


def _pulse_placeholder(head: str) -> str:
    # 머리 줄에서 `frame` 만 뺀다. 어디에 있었는지를 말하는 것은 `reading` 번호와 `scene` 이고,
    # Unity 프레임 번호는 그 둘 옆에서 읽는 쪽이 쓸 데가 없다.
    #
    # 뒤 문장은 `fold` 직후에 오는 도구 결과가 가진 값을 전부 다시 그린다는 뜻이다
    # (`PulseMemory.redraw_all_values_next`). 지운 것을 어디서 다시 찾는지 말해 두지 않으면,
    # agent 는 사라진 값을 지금도 아는 것처럼 행동하거나 같은 것을 다시 묻는다.
    where = " · ".join(part for part in head.split(" · ") if not part.startswith("frame "))
    return (
        f"{FOLDED_PULSE_VIEW_PREFIX}{where} folded — every value it showed is redrawn "
        "by the first tool result after the fold]"
    )


def _view_placeholder(match: re.Match[str]) -> str:
    if match.group("at") is not None:
        return _placeholder(match.group("at"))
    return _pulse_placeholder(match.group("head"))


def count_full_views(content: str) -> int:
    """`content` 안에 전문으로 남은 scene view 와 `pulse` view 의 수."""
    return sum(1 for _ in _VIEW_PATTERN.finditer(content))


def _fold_content(content: str, count: int) -> str:
    """`content` 안의 view 를 앞에서부터 `count` 개 `placeholder` 로 바꾼다.

    지금의 도구는 메시지 하나에 view 를 많아야 하나 싣지만, 둘 이상이어도 오래된 것(앞)부터
    바꾸므로 "가장 새것만 남긴다" 가 메시지 경계와 무관하게 성립한다.
    """
    return _VIEW_PATTERN.sub(_view_placeholder, content, count=count)


def _views_to_fold(total: int, keep: int, max_full_views: int) -> int:
    """원본 목록에 view 가 `total` 개 있을 때, 오래된 것부터 몇 개를 `fold` 하나.

    `fold` 는 매 호출 원본에서 다시 계산되므로, "넘으면 `keep` 개만 남긴다" 를 그대로 옮기면
    넘은 뒤로 매 호출 `fold` 하게 된다. 그래서 경계를 `total` 만의 함수로 둔다: `fold` 하는 수가
    `max_full_views + 1 - keep` 단위로만 늘고, 그 사이에는 같은 값이라 앞이 안 바뀐다. 남는 전문
    view 는 언제나 `keep` 개 이상 `max_full_views` 개 이하다.
    """
    if total <= max_full_views:
        return 0
    batch = max_full_views + 1 - keep
    return batch * ((total - keep) // batch)


@dataclass(frozen=True)
class SceneFold:
    """`fold_scenes` 의 결과. 모델에 보낼 목록과, 그 안에서 `fold` 된 view 의 수."""

    messages: list[BaseMessage]
    # 오래된 것부터 `fold` 된 view 의 수. 이 값이 지난 호출보다 커졌다는 것이 "방금 새 batch 를
    # `fold` 했다" 이고, runner 가 그것을 보고 다음 `pulse` view 가 값을 전부 다시 그리게 한다.
    views_folded: int


def fold_scenes(
    messages: list[BaseMessage],
    keep: int = DEFAULT_KEEP_SCENES,
    max_full_views: int = DEFAULT_MAX_FULL_VIEWS,
) -> SceneFold:
    """`fold_stale_scenes` 와 같되, 몇 개를 `fold` 했는지도 함께 돌려준다.

    전문 view 가 `max_full_views` 개를 넘을 때만 `fold` 하고, 그때는 가장 새것 `keep` 개만
    남긴다(`_views_to_fold`). `max_full_views == keep` 이면 종전처럼 매 호출 `keep` 개만 남긴다.
    """
    max_full_views = max(max_full_views, keep)
    view_counts = [_full_views_in(message) for message in messages]
    views_folded = _views_to_fold(sum(view_counts), keep, max_full_views)

    result: list[BaseMessage] = list(messages)
    remaining = views_folded
    # 오래된 것부터 센다. 반환하는 목록은 같은 자리의 값만 바꾸므로 순서가 그대로다.
    for index, count in enumerate(view_counts):
        if remaining <= 0:
            break
        if count == 0:
            continue
        folding_here = min(count, remaining)
        message = result[index]
        result[index] = message.model_copy(
            update={"content": _fold_content(message.content, folding_here)}
        )
        remaining -= folding_here
    return SceneFold(messages=result, views_folded=views_folded)


def _full_views_in(message: BaseMessage) -> int:
    if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
        return 0
    return count_full_views(message.content)


def fold_stale_scenes(
    messages: list[BaseMessage],
    keep: int = DEFAULT_KEEP_SCENES,
    max_full_views: int = DEFAULT_MAX_FULL_VIEWS,
) -> list[BaseMessage]:
    """Return `messages` with stale scene views and `pulse` views folded.

    Expects the message list a LangChain tool-calling agent hands to a model
    call: a mix of `HumanMessage`, `AIMessage`, and `ToolMessage`, in
    chronological order. Only `ToolMessage.content` is ever inspected — a
    view can only appear there, since it comes from `SceneMemory.render`
    by way of `observe_scene` (`app/agents/qa/tools/observation_tools.py`) and
    `ToolContext.answer` (`app/agents/qa/tools/tool_context.py`). Every other
    message, and every `ToolMessage` that carries no view (a failed action, an
    unanswered look), is returned unchanged.

    A "view" is the exact span between the markers its renderer puts around its
    own output — `<<scene view N>>` … `<<end scene view>>` or `<<pulse>>` …
    `<<end pulse>>` — never a guess at where the text starts or ends. That
    guarantees a fold either removes a whole view or none of it, and it means
    the action-outcome lines written above the view, and the `<<scene context>>`
    block and operator block (see `app/qa/channel.py`) appended below it, are
    untouched: the fold only ever replaces the marked span.

    `keep` 과 `max_full_views` 는 목록 **전체**에서 view 를 센다 — 도구나 스텝마다가 아니다.
    전문 view 가 `max_full_views` 개를 넘을 때만 새것 `keep` 개를 빼고 전부 `fold` 한다
    (`DEFAULT_MAX_FULL_VIEWS` 에 이유가 있다).

    Pure: never mutates `messages` or any message inside it. Returns a new
    list; messages that need no change are the very same objects, and folded
    ones are shallow copies (`model_copy`) with new `content`. Idempotent for
    a fixed `keep` and `max_full_views`: a folded view carries no markers, so the
    result holds at most `max_full_views` full views and folding it again changes
    nothing further.
    """
    return fold_scenes(messages, keep, max_full_views).messages


# How many of the newest neighbour blocks survive folding, counted per message
# — one search produces one message, so this is "the newest N searches keep
# their neighbours".
DEFAULT_KEEP_NEIGHBOUR_BLOCKS = 1

_NEIGHBOUR_PATTERN = re.compile(
    re.escape(NEIGHBOUR_BLOCK_START_PREFIX)
    + r"(?P<of>[^>]*)"
    + re.escape(NEIGHBOUR_BLOCK_START_SUFFIX)
    + r".*?"
    + re.escape(NEIGHBOUR_BLOCK_END),
    re.DOTALL,
)


def _neighbour_placeholder(of: str) -> str:
    # Plain text rather than the marker syntax, for the reason `_placeholder`
    # gives. Names the entry so the instruction is actionable: unlike a folded
    # scene, which `observe_scene` gets back with no argument, this one needs an id.
    return (
        f"[neighbours of {of} folded. Call expand_knowledge on {of} to see them "
        "again — they were volunteered by the search, not asked for.]"
    )


def _fold_neighbours(content: str) -> str:
    return _NEIGHBOUR_PATTERN.sub(
        lambda match: _neighbour_placeholder(match.group("of")), content
    )


def fold_stale_knowledge(
    messages: list[BaseMessage], keep: int = DEFAULT_KEEP_NEIGHBOUR_BLOCKS
) -> list[BaseMessage]:
    """Return `messages` with all but the newest `keep` neighbour blocks folded.

    Same contract as `fold_stale_scenes`: pure, model-input only, idempotent,
    `ToolMessage.content` only, unchanged messages returned as the same objects.

    **Only the neighbour block is folded. A hit's own summary and description are
    never touched**, and that line is the whole design.

    `fold_stale_scenes` folds a scene because the game moved on and
    `observe_scene` gets it back for nothing. A knowledge description is not
    stale — the documentation did not change while the run was going — and
    getting it back costs a search out of a budget of six. Folding it would tell
    the agent to spend a scarce resource undoing the fold, which is a materially
    worse bargain than the scene case.

    The neighbour block is the opposite on both counts. It was never asked for —
    the search volunteered it — and it is exactly recoverable by
    `expand_knowledge`, which has its own separate allowance. So this bounds the
    growth the graph feature introduced, and leaves the older debt that
    `app/agents/qa/knowledge.py` records about unfolded search results exactly
    where it is rather than quietly settling it inside an unrelated change.

    Interaction with compaction: `SummarizationMiddleware` replaces old messages
    wholesale, so a folded block may be summarised away entirely. Not a conflict —
    this fold is model-input only and never touches what is stored.
    """
    result: list[BaseMessage] = list(messages)
    kept = 0
    for index in range(len(result) - 1, -1, -1):
        message = result[index]
        if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
            continue
        if not _NEIGHBOUR_PATTERN.search(message.content):
            continue
        kept += 1
        if kept <= keep:
            continue
        result[index] = message.model_copy(
            update={"content": _fold_neighbours(message.content)}
        )
    return result


# How many of the newest loaded skills survive folding, counted per message — one
# `load_skill` call produces one message, so this is "the newest N loads keep
# their text". One, because a skill is read right before the work it covers, and
# the work the agent is doing now is the one the newest load was for.
DEFAULT_KEEP_SKILLS = 1

# The end marker names the skill again, and `(?P=name)` requires it to be the same
# name, so a span always runs from one skill's start to that same skill's end.
_SKILL_PATTERN = re.compile(
    re.escape(SKILL_BLOCK_START_PREFIX)
    + r"(?P<name>[^>\s]+)"
    + re.escape(SKILL_BLOCK_START_SUFFIX)
    + r".*?"
    + re.escape(SKILL_BLOCK_END_PREFIX)
    + r"(?P=name)"
    + re.escape(SKILL_BLOCK_END_SUFFIX),
    re.DOTALL,
)


def _skill_placeholder(name: str) -> str:
    # Plain text rather than the marker syntax, for the reason `_placeholder`
    # gives. It says outright that the rules are gone, because an agent that
    # remembers loading a skill would otherwise act as if it still had the text.
    return (
        f'[skill {name} folded to save context. Its rules are no longer in front '
        f'of you. Call load_skill("{name}") to read it again.]'
    )


def _fold_skills(content: str) -> str:
    return _SKILL_PATTERN.sub(lambda match: _skill_placeholder(match.group("name")), content)


def fold_stale_skills(
    messages: list[BaseMessage], keep: int = DEFAULT_KEEP_SKILLS
) -> list[BaseMessage]:
    """Return `messages` with all but the newest `keep` loaded skills folded.

    Same contract as `fold_stale_scenes`: pure, model-input only, idempotent,
    `ToolMessage.content` only, unchanged messages returned as the same objects.

    A skill is the span `load_skill` (`app/agents/qa/tools/skill_tools.py`) wraps
    between `<<skill NAME>>` and `<<end skill NAME>>`. Only that span is replaced:
    the "already loaded" note a reload puts above it stays, and so does anything
    else in the message.

    A skill body runs to about 10,000 characters. Left alone, every skill the run
    ever loaded would be resent on every turn until compaction, which is the cost
    moving the skills out of the system prompt was meant to remove. The trade is
    the one `fold_stale_knowledge` makes for neighbour blocks: the text is exactly
    recoverable, here by `load_skill` with the name the placeholder gives, and
    reading it again costs one tool call out of no allowance.

    Interaction with compaction: `SummarizationMiddleware` replaces old messages
    wholesale, so a folded skill may be summarised away entirely. Not a conflict —
    this fold is model-input only and never touches what is stored.
    """
    result: list[BaseMessage] = list(messages)
    kept = 0
    for index in range(len(result) - 1, -1, -1):
        message = result[index]
        if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
            continue
        if not _SKILL_PATTERN.search(message.content):
            continue
        kept += 1
        if kept <= keep:
            continue
        result[index] = message.model_copy(update={"content": _fold_skills(message.content)})
    return result
