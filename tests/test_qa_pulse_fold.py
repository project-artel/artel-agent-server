"""`fold_stale_scenes` 가 지난 `pulse` view 를 batch 로 `fold` 하고, 그 뒤 첫 view 가 값을 전부 다시 그린다.

실제 런은 GAME_STATE 없이 `pulse` 만 받으므로 모델이 읽는 view 는 전부 `pulse` view 다. 그것이
`fold` 되지 않아 L1 런 마지막 호출의 입력 87–90% 를 차지했다(`QA_ARCH_LABEL` v17 의 설명).

여기서 보는 것:
- 전문 view 가 `DEFAULT_MAX_FULL_VIEWS` 개일 때는 아무것도 안 고치고, 하나 더 오면 가장 새것만 남긴다
- `placeholder` 가 어느 `reading` 의 어느 `scene` 이었는지 한 줄로 말한다
- view 앞의 도구 몸통과 뒤의 `<<scene context>>` 블록은 그대로다
- `fold` 가 새 batch 를 지우면 다음 `pulse` view 가 델타가 빠뜨렸을 값까지 다시 그린다
"""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from app.agents.qa.context import (
    DEFAULT_MAX_FULL_VIEWS,
    FOLDED_PULSE_VIEW_PREFIX,
    FOLDED_VIEW_PREFIX,
    fold_scenes,
    fold_stale_scenes,
)
from app.agents.qa.runner import _context_shape, _fold_scene_views_for
from app.agents.qa.tools import QaRunState
from app.qa.channel import QaRunChannel
from app.qa.envelope import GameState, Interactable
from app.qa.pulse import PULSE_VIEW_END, PULSE_VIEW_START, PulseMemory, PulseReading
from app.qa.scene import SceneMemory
from app.qa.scene_context import SCENE_CONTEXT_END, SCENE_CONTEXT_START

TOOL_BODY = "replay_step_3 ran to the end. 12 action(s) reached the game."
SCENE_CONTEXT = f"{SCENE_CONTEXT_START}\nTurnBattleScene: 전투 화면\n{SCENE_CONTEXT_END}"


def enemy_reading(number: int, hp: int, *, whole: bool = False) -> PulseReading:
    return PulseReading.model_validate(
        {
            "schema": 2,
            "reading": number,
            "frame": number * 10,
            "scene": "TurnBattleScene",
            "whole": whole,
            "active": [
                {
                    "scene": "TurnBattleScene",
                    "id": 7,
                    "selector": "Enemy[1]",
                    "members": [{"on": "Enemy", "member": "Hp", "value": hp, "asked": True}],
                }
            ],
            "changed": ["TurnBattleScene/Enemy[1]|Enemy::Hp#0"],
        }
    )


def pulse_view(number: int) -> str:
    """`PulseMemory.render` 가 실제로 내는 `pulse` view, 머리 줄이 `reading {number}` 인 것."""
    memory = PulseMemory()
    memory.apply(enemy_reading(number, 100, whole=True))
    view = memory.render(since=0)
    assert view is not None
    return view


def tool_message(number: int) -> ToolMessage:
    """도구 몸통, `pulse` view, `<<scene context>>` 블록 순서로 실린 도구 결과."""
    content = f"{TOOL_BODY}\n\n{pulse_view(number)}\n\n{SCENE_CONTEXT}"
    return ToolMessage(content=content, tool_call_id=str(number))


def full_readings(messages) -> list[int]:
    return [
        index + 1
        for index, message in enumerate(messages)
        if f"{PULSE_VIEW_START}\nreading {index + 1} " in message.content
    ]


def test_the_rendered_view_has_both_markers() -> None:
    """`fold` 는 두 마커 사이만 바꾼다. 끝 마커가 없으면 뒤의 블록까지 먹는다."""
    view = pulse_view(5)

    assert view.startswith(f"{PULSE_VIEW_START}\nreading 5 · frame 50 · scene TurnBattleScene")
    assert view.endswith(PULSE_VIEW_END)


def test_nothing_is_folded_at_the_batch_size() -> None:
    messages = [tool_message(i) for i in range(1, DEFAULT_MAX_FULL_VIEWS + 1)]

    fold = fold_scenes(messages)

    assert fold.views_folded == 0
    assert all(after is before for after, before in zip(fold.messages, messages))


def test_one_past_the_batch_size_leaves_only_the_newest_in_full() -> None:
    count = DEFAULT_MAX_FULL_VIEWS + 1
    messages = [tool_message(i) for i in range(1, count + 1)]

    fold = fold_scenes(messages)

    assert full_readings(fold.messages) == [count]
    assert fold.views_folded == count - 1


def test_the_placeholder_names_the_reading_and_the_scene_in_one_line() -> None:
    messages = [tool_message(i) for i in range(1, DEFAULT_MAX_FULL_VIEWS + 2)]

    folded = fold_stale_scenes(messages)[0].content

    placeholder = folded.split("\n\n")[1]
    assert placeholder.startswith(
        f"{FOLDED_PULSE_VIEW_PREFIX}reading 1 · scene TurnBattleScene folded"
    )
    assert "\n" not in placeholder
    assert "frame" not in placeholder
    assert "Enemy[1]" not in folded


def test_the_tool_body_and_the_scene_context_block_survive() -> None:
    messages = [tool_message(i) for i in range(1, DEFAULT_MAX_FULL_VIEWS + 2)]

    folded = fold_stale_scenes(messages)[0].content

    assert folded.startswith(f"{TOOL_BODY}\n\n{FOLDED_PULSE_VIEW_PREFIX}")
    assert folded.endswith(f"\n\n{SCENE_CONTEXT}")


def test_folding_twice_changes_nothing_further() -> None:
    messages = [tool_message(i) for i in range(1, DEFAULT_MAX_FULL_VIEWS + 4)]

    once = fold_stale_scenes(messages)
    twice = fold_stale_scenes(once)

    assert [m.content for m in once] == [m.content for m in twice]


def test_a_tool_result_without_a_view_is_the_same_object() -> None:
    plain = ToolMessage(content="The game did not answer.", tool_call_id="plain")
    messages = [plain, *(tool_message(i) for i in range(1, DEFAULT_MAX_FULL_VIEWS + 2))]

    folded = fold_stale_scenes(messages)

    assert folded[0] is plain


def test_a_pulse_view_inside_a_scene_view_counts_once() -> None:
    """GAME_STATE 가 오는 빌드에서는 `pulse` view 가 scene view 마커 안에 들어간다."""
    memory = SceneMemory()
    memory.pulse.apply(enemy_reading(1, 100, whole=True))
    memory.apply(
        GameState(
            scene="TurnBattleScene",
            interactables=[Interactable(id=1, name="Attack", type="button")],
            observables={},
        )
    )
    nested = memory.render(0)
    assert PULSE_VIEW_START in nested

    messages = [ToolMessage(content=nested, tool_call_id="nested")]
    messages += [tool_message(i) for i in range(2, DEFAULT_MAX_FULL_VIEWS + 2)]

    fold = fold_scenes(messages)

    assert fold.views_folded == DEFAULT_MAX_FULL_VIEWS
    assert fold.messages[0].content.startswith(FOLDED_VIEW_PREFIX)
    assert FOLDED_PULSE_VIEW_PREFIX not in fold.messages[0].content


def test_context_shape_counts_pulse_views_and_their_placeholders() -> None:
    messages = [tool_message(i) for i in range(1, DEFAULT_MAX_FULL_VIEWS + 2)]

    shape = _context_shape(fold_stale_scenes(messages))

    assert f"views folded={DEFAULT_MAX_FULL_VIEWS} kept=1" in shape


def test_a_redraw_brings_back_a_value_a_delta_would_leave_out() -> None:
    memory = PulseMemory()
    memory.apply(enemy_reading(1, 100, whole=True))
    assert "Enemy.Hp = 100" in memory.render()
    # 안 움직인 값은 델타가 다시 안 그린다. `fold` 가 앞의 view 를 지우면 이 값이 사라진다.
    assert "Enemy.Hp = 100" not in memory.render()

    memory.redraw_all_values_next()

    redrawn = memory.render()
    assert "Enemy.Hp = 100" in redrawn
    assert "(changed earlier)" in redrawn
    # 한 번 그리면 끝이다. 매 view 가 전부 다시 그리면 `fold` 가 줄인 것이 도로 붙는다.
    assert "Enemy.Hp = 100" not in memory.render()


async def _swallow(frame: dict) -> None:
    return None


def run_fold_middleware(middleware, messages):
    received = []

    async def handler(request):
        received.append(request.messages)
        return SimpleNamespace(result=[])

    request = SimpleNamespace(
        messages=messages,
        override=lambda messages: SimpleNamespace(messages=messages),
    )
    asyncio.run(middleware.awrap_model_call(request, handler))
    return received[0]


def held_channel() -> QaRunChannel:
    """`Enemy.Hp = 100` 을 이미 한 번 그려서, 다음 델타에는 그 값이 안 나오는 채널."""
    channel = QaRunChannel(qa_try_id=1, send=_swallow)
    channel.scene.pulse.apply(enemy_reading(1, 100, whole=True))
    channel.scene.pulse.since_action(None)
    return channel


def test_a_new_batch_makes_the_next_pulse_view_redraw_everything() -> None:
    channel = held_channel()
    state = QaRunState(total_steps=1)
    middleware = _fold_scene_views_for(state, channel)
    messages = [tool_message(i) for i in range(1, DEFAULT_MAX_FULL_VIEWS + 2)]

    sent = run_fold_middleware(middleware, messages)

    assert full_readings(sent) == [DEFAULT_MAX_FULL_VIEWS + 1]
    assert state.views_folded == DEFAULT_MAX_FULL_VIEWS
    assert "Enemy.Hp = 100" in channel.scene.pulse.since_action(None)


def test_no_redraw_without_a_new_batch() -> None:
    channel = held_channel()
    state = QaRunState(total_steps=1)
    middleware = _fold_scene_views_for(state, channel)

    # batch 크기까지는 `fold` 가 없다.
    run_fold_middleware(middleware, [tool_message(i) for i in range(1, DEFAULT_MAX_FULL_VIEWS + 1)])
    assert "Enemy.Hp = 100" not in channel.scene.pulse.since_action(None)

    # 첫 batch 가 `fold` 되고 그 다음 view 가 값을 다시 그렸다.
    batch = [tool_message(i) for i in range(1, DEFAULT_MAX_FULL_VIEWS + 2)]
    run_fold_middleware(middleware, batch)
    assert "Enemy.Hp = 100" in channel.scene.pulse.since_action(None)

    # 같은 batch 를 다시 보는 호출은 새 `fold` 가 아니다.
    run_fold_middleware(middleware, [*batch, tool_message(DEFAULT_MAX_FULL_VIEWS + 2)])
    assert "Enemy.Hp = 100" not in channel.scene.pulse.since_action(None)
