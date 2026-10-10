"""Folding stale blocks out of what the model reads.

Every QA tool result carries a full scene view (`SceneMemory.render` in
`app/qa/scene.py`), and every tool message stays in the conversation forever —
`observe_scene` puts one there directly, and every acting tool does too, by way
of the shared `ToolContext.run` in `app/agents/qa/tools/tool_context.py`. A run of
even a handful of
steps ends up with dozens of near-identical dumps in context by the time it
matters most: late in the run, with the least room left to reason.

The scene view is not the only block that goes stale. A `search_knowledge` result
carries a neighbour block nobody asked for, a `load_skill` result carries about
10,000 characters of rules, and an `on_demand` screenshot is resent on every turn
after it arrives. `fold_context` is the one place all four are folded.

`fold` 는 네 종류를 따로따로 하지 않고 한 번에 한다. 오래된 block 하나를 `placeholder` 로 바꾸는
것은 이미 보낸 프롬프트의 중간을 고쳐 쓰는 일이라, 그 자리부터 끝까지 `cache` 가 안 맞는다.
종류마다 제 시점에 `fold` 하면 그 손실을 종류 수만큼 따로 낸다. L1 런 6개(try 178–183)의
LangSmith trace 에서 `cache` 를 못 읽은 입력의 72–90% 가 `fold` 때문이었고, `pulse` view 의 batch
`fold` 와 별개로 실행된 skill `fold` 가 한 번에 평균 약 28,000 token 을 다시 쓰게 했다. 그래서
후보마다 점수를 매기고, `fold` 하지 않은 후보의 점수 합계가 기준을 넘는 호출 한 번에 전부
`fold` 한다. 그 사이의 호출은 앞을 안 고치고 끝에 덧붙기만 하므로 `cache` 가 맞는다.

It is model-input-only by construction: it takes a list and returns a new one,
so a caller can apply it to what a model call is about to see without touching
anything else that reads the same messages — the WebSocket timeline, qa_log, and
the console logger in `app/agents/qa/runner.py` all read the channel or the
logger directly, never this function's output, so they keep the full text
regardless.

Wiring: `QaRunner.run` builds its agent with `langchain.agents.create_agent`
and drives it with `agent.astream(...)`, so there is no per-turn message list
the caller re-passes — LangGraph owns it internally for the run. The hook point
is `create_agent(..., middleware=[...])`: a `wrap_model_call` middleware
receives a `ModelRequest` (its `.messages` excludes the system message) and a
`handler`, and can call `request.override(messages=...)` before invoking
`handler(request)`. That changes only what the one model call receives, not the
graph's own state. What does have to survive between calls is which blocks were
already folded, and the runner keeps that on `QaRunState.folded_blocks`.
`runner.py` wires this in.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

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
from app.agents.qa.vision import CAPTURE_MESSAGE_KEY, is_capture_message, without_image
from app.qa.pulse import PULSE_VIEW_END, PULSE_VIEW_START
from app.qa.scene import SCENE_VIEW_END, SCENE_VIEW_START_PREFIX, SCENE_VIEW_START_SUFFIX


class FoldKind(StrEnum):
    """`fold` 의 후보가 되는 block 의 종류."""

    scene_view = "scene_view"
    knowledge_neighbours = "knowledge_neighbours"
    skill = "skill"
    image = "image"


# How many of the newest scene views survive folding, in full — counted across
# the whole message list, not per tool or per step.
#
# One, not two, since the live view: `SceneMemory.render_now` is appended to
# every model call, so the current scene is always in front of the model without
# a tool result having to carry it. What the newest tool view still adds is the
# scene AT the moment an action landed, tied to that action's outcome lines —
# worth one. A second would be two stale snapshots under a fresh one.
DEFAULT_KEEP_SCENES = 1

# How many of the newest neighbour blocks survive folding — one search produces
# one message, so this is "the newest search keeps its neighbours".
DEFAULT_KEEP_NEIGHBOUR_BLOCKS = 1

# How many of the newest loaded skills survive folding. One, because a skill is
# read right before the work it covers, and the work the agent is doing now is
# the one the newest load was for.
DEFAULT_KEEP_SKILLS = 1

# How many of the newest `on_demand` screenshots keep their picture. The agent
# judges against what it is looking at now; older screenshots have already been
# reasoned about, and their conclusions are in the transcript. Without a cap the
# transcript resends every screenshot on every turn, so the cost of a run grows with
# the square of the number of captures.
DEFAULT_KEEP_IMAGES = 2

_KEEP_BY_KIND = {
    FoldKind.scene_view: DEFAULT_KEEP_SCENES,
    FoldKind.knowledge_neighbours: DEFAULT_KEEP_NEIGHBOUR_BLOCKS,
    FoldKind.skill: DEFAULT_KEEP_SKILLS,
    FoldKind.image: DEFAULT_KEEP_IMAGES,
}

# `fold` 하지 않은 후보의 점수 합계가 이 값에 이르면 그 호출에서 전부 `fold` 한다. 점수는 후보가
# 차지하는 글자 수다.
#
# 56,000 은 종전 batch 의 크기에 맞춘 값이다. 종전에는 전문 view 가 8개를 넘을 때 `fold` 했고, L1
# 런 실측으로 view 한 개가 평균 약 7,000자이므로 오래된 view 약 8개, 56,000자가 쌓일 때마다
# `cache` 를 한 번 다시 썼다. 같은 값으로 두어야 이 변경을 측정한 결과에서 "네 종류를 한 번에
# 모았다" 는 효과만 따로 읽힌다.
DEFAULT_FOLD_THRESHOLD_CHARS = 56_000

# 그림 한 장의 점수. 그림은 글자 수가 없으므로 token 으로 어림해 글자로 바꾼다: 화면 한 장이
# 약 1,000 token 이고, 글자는 token 당 약 4자다.
IMAGE_SCORE_CHARS = 4_000

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

_NEIGHBOUR_PATTERN = re.compile(
    re.escape(NEIGHBOUR_BLOCK_START_PREFIX)
    + r"(?P<of>[^>]*)"
    + re.escape(NEIGHBOUR_BLOCK_START_SUFFIX)
    + r".*?"
    + re.escape(NEIGHBOUR_BLOCK_END),
    re.DOTALL,
)

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


def _neighbour_placeholder(match: re.Match[str]) -> str:
    # Plain text rather than the marker syntax, for the reason `_placeholder`
    # gives. Names the entry so the instruction is actionable: unlike a folded
    # scene, which `observe_scene` gets back with no argument, this one needs an id.
    #
    # Only the neighbour block is ever folded, never a hit's own summary and
    # description. A knowledge description is not stale — the documentation did
    # not change while the run was going — and getting it back costs a search out
    # of a budget of six. The neighbour block was never asked for, and
    # `expand_knowledge`, which has its own allowance, gets it back exactly.
    of = match.group("of")
    return (
        f"[neighbours of {of} folded. Call expand_knowledge on {of} to see them "
        "again — they were volunteered by the search, not asked for.]"
    )


def _skill_placeholder(match: re.Match[str]) -> str:
    # Plain text rather than the marker syntax, for the reason `_placeholder`
    # gives. It says outright that the rules are gone, because an agent that
    # remembers loading a skill would otherwise act as if it still had the text.
    name = match.group("name")
    return (
        f'[skill {name} folded to save context. Its rules are no longer in front '
        f'of you. Call load_skill("{name}") to read it again.]'
    )


# 도구 결과 안의 block 을 찾는 패턴과, 찾은 block 을 바꿀 `placeholder`. 그림은 도구 결과가
# 아니라 따로 실린 메시지라 여기 없다.
_TEXT_BLOCKS = {
    FoldKind.scene_view: (_VIEW_PATTERN, _view_placeholder),
    FoldKind.knowledge_neighbours: (_NEIGHBOUR_PATTERN, _neighbour_placeholder),
    FoldKind.skill: (_SKILL_PATTERN, _skill_placeholder),
}


def count_full_views(content: str) -> int:
    """`content` 안에 전문으로 남은 scene view 와 `pulse` view 의 수."""
    return sum(1 for _ in _VIEW_PATTERN.finditer(content))


@dataclass(frozen=True)
class FoldCandidate:
    """`fold` 할 수 있는 block 하나.

    `key` 는 호출이 바뀌어도 같은 block 을 가리켜야 한다. 목록의 자리는 압축이 옛 메시지를
    요약으로 바꾸면 움직이므로 쓰지 않고, 도구 결과는 `tool_call_id` 와 그 안에서 몇 번째
    block 인지로, 그림은 `capture_id` 로 만든다.
    """

    kind: FoldKind
    key: str
    score: int


@dataclass(frozen=True)
class ContextFold:
    """`fold_context` 의 결과."""

    messages: list[BaseMessage]
    # 이 호출까지 `fold` 된 block 의 `key`. runner 가 다음 호출에 `already_folded` 로 돌려준다.
    folded_keys: frozenset[str]
    # 이 호출에서 새로 `fold` 한 block 의 종류. 비어 있으면 앞이 하나도 안 바뀌었다는 뜻이다.
    newly_folded_kinds: frozenset[FoldKind]


def fold_context(
    messages: list[BaseMessage],
    kinds: Iterable[FoldKind],
    already_folded: frozenset[str] = frozenset(),
    threshold_chars: int = DEFAULT_FOLD_THRESHOLD_CHARS,
) -> ContextFold:
    """Return `messages` with stale blocks of `kinds` folded, in batches.

    A block is stale once it is not among the newest few of its kind
    (`DEFAULT_KEEP_*`, counted across the whole list). A stale block in
    `already_folded` is always folded again, so the prompt a previous call sent
    stays byte-identical. The other stale blocks are folded only when their
    scores add up to `threshold_chars`, and then all of them at once, whatever
    their kind. `threshold_chars=0` folds every stale block on every call.

    A "block" is the exact span between the markers its renderer puts around its
    own output, never a guess at where the text starts or ends: `<<scene view N>>`
    … `<<end scene view>>` or `<<pulse>>` … `<<end pulse>>`, a neighbour block, or
    `<<skill NAME>>` … `<<end skill NAME>>`. A fold either replaces a whole block
    or none of it, so the action-outcome lines above a view, the `<<scene context>>`
    and operator blocks below it, a hit's own text, and the "already loaded" note
    above a reloaded skill are never touched. An image is the picture on an
    `on_demand` capture turn, and folding it leaves the caption.

    Pure: never mutates `messages` or any message inside it. Messages that need
    no change are the very same objects, and folded ones are copies with new
    content.
    """
    enabled = frozenset(kinds)
    candidates = [
        candidate for message in messages for candidate in _candidates_in(message, enabled)
    ]
    stale = _stale(candidates)

    pending = [candidate for candidate in stale if candidate.key not in already_folded]
    folded_keys = {candidate.key for candidate in stale if candidate.key in already_folded}
    newly_folded_kinds: frozenset[FoldKind] = frozenset()
    if pending and sum(candidate.score for candidate in pending) >= threshold_chars:
        folded_keys |= {candidate.key for candidate in pending}
        newly_folded_kinds = frozenset(candidate.kind for candidate in pending)

    return ContextFold(
        messages=[_fold_message(message, enabled, folded_keys) for message in messages],
        folded_keys=frozenset(folded_keys),
        newly_folded_kinds=newly_folded_kinds,
    )


def fold_every_stale_block(
    messages: list[BaseMessage], kinds: Iterable[FoldKind]
) -> list[BaseMessage]:
    """`messages` with every stale block of `kinds` folded, ignoring batches."""
    return fold_context(messages, kinds, threshold_chars=0).messages


def _candidates_in(message: BaseMessage, kinds: frozenset[FoldKind]) -> list[FoldCandidate]:
    if FoldKind.image in kinds and is_capture_message(message) and _has_image(message):
        capture_id = message.additional_kwargs[CAPTURE_MESSAGE_KEY]
        return [FoldCandidate(FoldKind.image, f"image:{capture_id}", IMAGE_SCORE_CHARS)]
    if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
        return []
    candidates = []
    for kind, (pattern, _) in _TEXT_BLOCKS.items():
        if kind not in kinds:
            continue
        for ordinal, match in enumerate(pattern.finditer(message.content)):
            key = f"{kind}:{message.tool_call_id}:{ordinal}"
            candidates.append(FoldCandidate(kind, key, len(match.group(0))))
    return candidates


def _has_image(message: BaseMessage) -> bool:
    return isinstance(message.content, list) and any(
        isinstance(block, dict) and block.get("type") == "image_url"
        for block in message.content
    )


def _stale(candidates: list[FoldCandidate]) -> list[FoldCandidate]:
    """`candidates` 중 종류마다 가장 새것 몇 개를 뺀 나머지. 순서는 그대로 둔다."""
    newest_kept: set[str] = set()
    for kind, keep in _KEEP_BY_KIND.items():
        of_kind = [candidate.key for candidate in candidates if candidate.kind is kind]
        newest_kept.update(of_kind[-keep:] if keep > 0 else [])
    return [candidate for candidate in candidates if candidate.key not in newest_kept]


def _fold_message(
    message: BaseMessage, kinds: frozenset[FoldKind], folded_keys: set[str]
) -> BaseMessage:
    if FoldKind.image in kinds and is_capture_message(message):
        capture_id = message.additional_kwargs[CAPTURE_MESSAGE_KEY]
        if f"image:{capture_id}" in folded_keys and _has_image(message):
            return without_image(message)
        return message
    if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
        return message

    content = message.content
    for kind, (pattern, placeholder) in _TEXT_BLOCKS.items():
        if kind in kinds:
            key_prefix = f"{kind}:{message.tool_call_id}:"
            content = _fold_matches(content, pattern, placeholder, key_prefix, folded_keys)
    if content == message.content:
        return message
    return message.model_copy(update={"content": content})


def _fold_matches(
    content: str,
    pattern: re.Pattern[str],
    placeholder: Callable[[re.Match[str]], str],
    key_prefix: str,
    folded_keys: set[str],
) -> str:
    # 앞의 종류를 바꾼 뒤에도 이 종류의 순번은 그대로다. `placeholder` 는 어느 종류의 마커도
    # 쓰지 않으므로, 한 종류를 바꾸어도 다른 종류의 block 이 생기거나 없어지지 않는다.
    ordinals = itertools.count()

    def replace(match: re.Match[str]) -> str:
        if f"{key_prefix}{next(ordinals)}" in folded_keys:
            return placeholder(match)
        return match.group(0)

    return pattern.sub(replace, content)
