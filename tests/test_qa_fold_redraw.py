"""`fold` 직후 다시 그린 view 가 다음 `fold` 를 바로 부르지 않고, `fold` 뒤에도 그 앞까지 `cache` 에서 읽힌다.

로컬 런 116(`v53`, `claude/claude-sonnet-5-5`)에서 `fold` 뒤 첫 view 가 값을 전부 다시 그려 약 115,000 token
이 됐고, 다음 view 가 오자 혼자 기준을 넘어 다시 `fold` 됐다. 그 `fold` 가 또 다시 그리기를 불러 두 호출마다
되풀이됐고, 경계가 시스템 끝과 대화 끝에만 있어 `fold` 마다 시스템 뒤 대화 전체를 다시 썼다.

여기서 보는 것:
- 기준에서 뺀 block 은 혼자서는 `fold` 를 부르지 않고, 다른 block 이 기준을 넘으면 함께 `fold` 된다
- runner 가 `fold` 뒤 처음 나타난 view 를 다시 그린 view 로 알아보고 기준에서 뺀다
- 다음 batch 가 고쳐 쓰는 자리 바로 앞 메시지에 `stable` 표시가 붙고, Claude 요청에서 그 자리에 경계가 선다
"""

import asyncio
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents.qa.arch import QaArchSpec
from app.agents.qa.case_context import (
    TC_LEDGER_ID,
    detail_block,
    supersede_case_context,
)
from app.agents.qa.context import FoldKind, fold_context
from app.agents.qa.runner import _fold_context_for
from app.agents.qa.tools import QaRunState
from app.llm.cache_boundary import STABLE_KEY, per_call, stable
from app.llm.claude_api import (
    ClaudeApiChat,
    _with_stable_breakpoint,
    _with_stored_breakpoint,
)
from app.qa.channel import QaRunChannel
from app.qa.pulse import (
    PULSE_VIEW_END,
    PULSE_VIEW_START,
    PULSE_WHOLE_SCENE,
    PulseMemory,
    PulseReading,
)

SCENE_VIEW_ONLY = frozenset({FoldKind.scene_view})
THRESHOLD = 1_000
CACHED = {"type": "ephemeral"}


def view(number: int, size: int, whole: bool = False) -> ToolMessage:
    """`pulse` view 하나를 실은 도구 결과. view 의 글자 수는 대략 `size` 다."""
    body = "x" * size
    head = f"reading {number} · scene S" + (PULSE_WHOLE_SCENE if whole else "")
    content = f"clicked.\n\n{PULSE_VIEW_START}\n{head}\n{body}\n{PULSE_VIEW_END}"
    return ToolMessage(content=content, tool_call_id=str(number))


def whole(number: int) -> ToolMessage:
    return view(number, 5_000, whole=True)


def delta(number: int) -> ToolMessage:
    return view(number, 300)


def redraw(number: int) -> ToolMessage:
    return view(number, 5_000)


async def _swallow(frame: dict) -> None:
    return None


def call(middleware, messages):
    received = []

    async def handler(request):
        received.append(request.messages)
        return SimpleNamespace(result=[])

    request = SimpleNamespace(messages=messages, override=lambda messages: SimpleNamespace(messages=messages))
    asyncio.run(middleware.awrap_model_call(request, handler))
    return received[0]


def folded(messages) -> list[str]:
    return [message.tool_call_id for message in messages if isinstance(message, ToolMessage) and PULSE_VIEW_START not in message.content]


# --- fold_context 의 `unscored` ---------------------------------------------------


def test_기준에서_뺀_block_은_혼자서는_fold_를_부르지_않는다() -> None:
    messages = [redraw(1), delta(2)]

    fold = fold_context(messages, SCENE_VIEW_ONLY, threshold_chars=THRESHOLD, unscored=frozenset({"scene_view:1:0"}))

    assert fold.folded_keys == frozenset()
    assert fold.messages == messages


def test_다른_block_이_기준을_넘으면_기준에서_뺀_block_도_함께_fold_된다() -> None:
    messages = [redraw(1), *[delta(n) for n in range(2, 7)]]

    fold = fold_context(messages, SCENE_VIEW_ONLY, threshold_chars=THRESHOLD, unscored=frozenset({"scene_view:1:0"}))

    assert "scene_view:1:0" in fold.folded_keys
    assert folded(fold.messages) == ["1", "2", "3", "4", "5"]


def test_unfolded_from_은_fold_되지_않은_후보가_처음_나오는_메시지다() -> None:
    messages = [HumanMessage(content="start"), delta(1), delta(2), delta(3), delta(4), delta(5)]

    before = fold_context(messages, SCENE_VIEW_ONLY, threshold_chars=10 * THRESHOLD)
    after = fold_context(messages, SCENE_VIEW_ONLY, threshold_chars=THRESHOLD)

    assert before.unfolded_from == 1
    # 가장 새 view 하나만 남는다.
    assert after.unfolded_from == 5


# --- runner: 다시 그린 view 를 알아보고 기준에서 뺀다 ---------------------------------


def test_fold_뒤_다시_그린_view_는_다음_view_가_와도_다시_fold_되지_않는다() -> None:
    channel = QaRunChannel(qa_try_id=1, send=_swallow)
    state = QaRunState(total_steps=1)
    middleware = _fold_context_for(SCENE_VIEW_ONLY, THRESHOLD, state, channel, redraw="unscored")
    batch = [delta(n) for n in range(1, 6)]

    call(middleware, batch)
    assert len(state.folded_blocks) == 4

    # `fold` 뒤 첫 view 는 값을 전부 다시 그려 혼자 기준을 넘는다.
    call(middleware, [*batch, redraw(6)])
    assert state.redraw_view == "scene_view:6:0"
    call(middleware, [*batch, redraw(6), delta(7)])
    assert len(state.folded_blocks) == 4

    # 그 뒤의 델타가 기준을 넘으면 다시 그린 view 와 함께 `fold` 된다.
    grown = [*batch, redraw(6), *[delta(n) for n in range(7, 12)]]
    sent = call(middleware, grown)
    assert folded(sent) == [str(n) for n in range(1, 11)]
    assert state.redraw_view is None


def test_다시_그린_view_를_세던_때는_다음_view_에서_바로_다시_fold_했다() -> None:
    """고치기 전 동작. `unscored` 없이 같은 목록을 보면 다시 그린 view 하나로 기준을 넘는다."""
    messages = [*[delta(n) for n in range(1, 6)], redraw(6), delta(7)]
    before = frozenset(f"scene_view:{n}:0" for n in range(1, 5))

    fold = fold_context(messages, SCENE_VIEW_ONLY, before, threshold_chars=THRESHOLD)

    assert "scene_view:6:0" in fold.folded_keys


# --- `stable` 표시와 Claude 요청의 경계 -------------------------------------------


def test_다음_batch_가_고쳐_쓰는_자리_바로_앞_메시지에_stable_표시가_붙는다() -> None:
    channel = QaRunChannel(qa_try_id=1, send=_swallow)
    state = QaRunState(total_steps=1)
    middleware = _fold_context_for(SCENE_VIEW_ONLY, THRESHOLD, state, channel, cache_boundary=True)
    messages = [
        HumanMessage(content="start"),
        AIMessage(content="", tool_calls=[{"name": "click", "args": {}, "id": "1"}]),
        delta(1),
        AIMessage(content="", tool_calls=[{"name": "click", "args": {}, "id": "2"}]),
        delta(2),
    ]

    sent = call(middleware, messages)

    # 고쳐 쓸 첫 후보는 `delta(1)` 이고, 그 앞의 답 메시지는 건너뛴다.
    assert [index for index, message in enumerate(sent) if message.additional_kwargs.get(STABLE_KEY)] == [0]


def test_Claude_요청에서_stable_메시지에_경계가_서고_표시는_실리지_않는다() -> None:
    messages = [
        SystemMessage(content="rules"),
        stable(HumanMessage(content="start")),
        AIMessage(content="", tool_calls=[{"name": "click", "args": {}, "id": "1"}]),
        ToolMessage(content="scene view", tool_call_id="1"),
        per_call(HumanMessage(content="latest pulse")),
    ]
    model = ClaudeApiChat(model="claude-sonnet-5-5", api_key="sk-ant-test")

    payload = model._get_request_payload(_with_stored_breakpoint(_with_stable_breakpoint(messages)))

    first = payload["messages"][0]
    assert first["role"] == "user"
    assert first["content"][-1]["cache_control"] == CACHED
    last = payload["messages"][-1]
    assert last["content"][0]["cache_control"] == CACHED
    assert STABLE_KEY not in json.dumps(payload)
    # 시스템 · `stable` · 저장된 대화 끝으로 셋. top-level 을 더해도 API 한도 넷을 넘지 않는다.
    assert json.dumps(payload).count('"cache_control"') <= 4


def test_stable_과_저장된_대화_끝이_같은_메시지면_경계는_하나다() -> None:
    messages = [
        HumanMessage(content="start"),
        AIMessage(content="", tool_calls=[{"name": "click", "args": {}, "id": "1"}]),
        stable(ToolMessage(content="scene view", tool_call_id="1")),
        per_call(HumanMessage(content="latest pulse")),
    ]

    out = _with_stored_breakpoint(_with_stable_breakpoint(messages))

    assert out[2].content == [{"type": "text", "text": "scene view", "cache_control": CACHED}]


# --- `scaled` 와 기본값 --------------------------------------------------------------


def test_scaled_는_기준을_가장_큰_view_보다_크게_올려_다시_그린_view_로는_fold_하지_않는다() -> None:
    channel = QaRunChannel(qa_try_id=1, send=_swallow)
    state = QaRunState(total_steps=1)
    middleware = _fold_context_for(SCENE_VIEW_ONLY, THRESHOLD, state, channel, redraw="scaled", redraw_scale=1.5)
    messages = [redraw(1), delta(2)]

    call(middleware, messages)
    assert state.folded_blocks == frozenset()

    # 델타가 쌓여 다시 그린 view 의 1.5 배를 넘으면 `fold` 한다.
    grown = [redraw(1), *[delta(n) for n in range(2, 12)]]
    sent = call(middleware, grown)
    assert folded(sent) == [str(n) for n in range(1, 11)]


def test_counted_는_다시_그린_view_를_세고_stable_표시를_두지_않는다() -> None:
    """v58 까지의 동작. 다시 그린 view 하나가 다음 view 가 오자마자 `fold` 된다."""
    channel = QaRunChannel(qa_try_id=1, send=_swallow)
    state = QaRunState(total_steps=1)
    middleware = _fold_context_for(SCENE_VIEW_ONLY, THRESHOLD, state, channel, redraw="counted", cache_boundary=False)
    messages = [HumanMessage(content="start"), redraw(1), delta(2)]

    sent = call(middleware, messages)

    assert folded(sent) == ["1"]
    assert not any(message.additional_kwargs.get(STABLE_KEY) for message in sent)


def test_기본값은_anchored_와_cache_경계다() -> None:
    spec = QaArchSpec()
    assert (spec.fold_redraw, spec.fold_cache_boundary) == ("anchored", True)

    channel = QaRunChannel(qa_try_id=1, send=_swallow)
    state = QaRunState(total_steps=1)
    middleware = _fold_context_for(SCENE_VIEW_ONLY, THRESHOLD, state, channel)

    sent = call(middleware, [HumanMessage(content="start"), whole(1), delta(2)])

    assert folded(sent) == []
    # 기준 view 는 다음 batch 가 안 고치므로, 경계는 그 뒤에 선다.
    assert [index for index, message in enumerate(sent) if message.additional_kwargs.get(STABLE_KEY)] == [1]


def test_fold_가_다시_그리기를_부르는_되풀이가_scaled_에서는_생기지_않는다() -> None:
    """런 134 의 모양. `counted` 는 두 호출마다 다시 그린 view 를 `fold` 하고, 그 `fold` 가 다음 view 를 다시
    그리게 한다. `scaled` 는 델타가 쌓여 기준을 넘을 때 한 번 `fold` 하고, 그 뒤 다시 그린 view 로는 넘지 않는다."""

    def run(redraw_mode: str) -> int:
        channel = QaRunChannel(qa_try_id=1, send=_swallow)
        state = QaRunState(total_steps=1)
        middleware = _fold_context_for(SCENE_VIEW_ONLY, THRESHOLD, state, channel, redraw=redraw_mode)
        folds = 0
        redrew = False
        messages = [HumanMessage(content="start"), redraw(1)]
        for number in range(2, 12):
            before = len(state.folded_blocks)
            # `fold` 했으면 다음 view 는 값을 전부 다시 그린다. 아니면 델타다.
            messages.append(redraw(number) if redrew else delta(number))
            call(middleware, messages)
            redrew = len(state.folded_blocks) > before
            folds += redrew
        return folds

    assert run("counted") >= 5
    assert run("scaled") <= 1


# --- 지난 TC ledger 는 `fold` batch 와 같은 호출에서 접는다 ---------------------------


def ledger(number: int) -> HumanMessage:
    return HumanMessage(id=f"{TC_LEDGER_ID}-{number}", content=f"AUTHORITATIVE TC LEDGER {number}")


def detail(number: int, case_id: int) -> ToolMessage:
    return ToolMessage(id=f"detail-{number}", content=detail_block(case_id, "spec"), tool_call_id=f"d{number}")


def test_지난_ledger_는_fold_batch_에서도_접지_않는다() -> None:
    """첫 ledger 가 대화 맨 앞에 있어서, 접으면 batch 마다 시스템 뒤 전부를 다시 쓴다(로컬 런 137)."""
    frozen: set[tuple[str, int]] = set()
    messages = [ledger(1), delta(1), ledger(2), delta(2)]

    sent, pending = supersede_case_context(messages, None, frozen, batch=True)

    assert sent == messages
    assert pending is None


def test_지난_TC_상세는_fold_batch_에서_접고_다음_호출에도_같은_내용으로_보낸다() -> None:
    frozen: set[tuple[str, int]] = set()
    messages = [detail(1, 7), detail(2, 8), detail(3, 9)]

    quiet, pending = supersede_case_context(messages, 9, frozen, batch=False)
    sent, _ = supersede_case_context(messages, 9, frozen, batch=True)
    # 지금 TC 가 바뀌어도 접은 내용은 그때 그대로다.
    later, _ = supersede_case_context(messages, 8, frozen, batch=False)

    assert quiet == messages
    assert pending == 0
    assert "details folded" in sent[0].content
    assert later[0].content == sent[0].content


def test_TC_상세를_접은_뒤_새_anchor가_생기면_이전_view도_접힌다() -> None:
    state = QaRunState(total_steps=1)
    channel = QaRunChannel(qa_try_id=1, send=_swallow)
    middleware = _fold_context_for(SCENE_VIEW_ONLY, THRESHOLD, state, channel, redraw="anchored", case_context=True)
    anchor = whole(1)
    anchor = anchor.model_copy(update={"id": "start-7", "content": detail_block(7, "spec") + "\n" + anchor.content})
    messages = [anchor, detail(2, 8), *[delta(n) for n in range(3, 9)]]

    first = call(middleware, messages)
    assert "details folded" in first[0].content
    assert PULSE_VIEW_START in first[0].content

    messages.append(whole(9))
    for _ in range(2):
        sent = call(middleware, messages)
        assert "details folded" in sent[0].content
        assert PULSE_VIEW_START not in sent[0].content
        assert PULSE_VIEW_START in sent[-1].content


def test_case_context_가_꺼진_middleware_는_ledger_를_덧붙이기만_한다() -> None:
    from app.agents.qa.case_context import QaCaseContextMiddleware

    channel = QaRunChannel(qa_try_id=1, send=_swallow)
    state = QaRunState(total_steps=1)
    middleware = QaCaseContextMiddleware(state, channel, supersede=False)
    messages = [ledger(n) for n in range(1, 6)]

    update = asyncio.run(middleware.abefore_model({"messages": messages}, None))

    assert len(update["messages"]) == 1
    assert update["messages"][0].id.startswith(TC_LEDGER_ID)


def test_fold_batch_는_ledger_를_남기고_경계는_첫_델타_앞에_선다() -> None:
    channel = QaRunChannel(qa_try_id=1, send=_swallow)
    state = QaRunState(total_steps=1)
    middleware = _fold_context_for(SCENE_VIEW_ONLY, THRESHOLD, state, channel, redraw="counted", case_context=True)
    messages = [HumanMessage(content="start"), ledger(1), *[delta(n) for n in range(1, 6)], ledger(2), delta(6)]

    sent = call(middleware, messages)

    assert sent[1].content == "AUTHORITATIVE TC LEDGER 1"
    assert folded(sent) == ["1", "2", "3", "4", "5"]
    assert [index for index, message in enumerate(sent) if message.additional_kwargs.get(STABLE_KEY)] == [7]


def test_그림으로_끝나는_메시지는_그림_앞_block_에_경계가_선다() -> None:
    """langchain-anthropic 이 `image_url` 을 `image` 로 바꾸며 경계를 버린다(로컬 런 137)."""
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
    messages = [
        SystemMessage(content="rules"),
        HumanMessage(content=[{"type": "text", "text": "Captured the screen."}, image]),
        per_call(HumanMessage(content="latest pulse")),
    ]
    model = ClaudeApiChat(model="claude-sonnet-5-5", api_key="sk-ant-test")

    payload = model._get_request_payload(_with_stored_breakpoint(messages))

    caption = payload["messages"][0]["content"][0]
    assert caption["type"] == "text"
    assert caption["cache_control"] == CACHED


def test_계획_상세는_다음_batch_가_고쳐_쓰는_자리로_세지_않는다() -> None:
    """첫 메시지의 계획 상세는 접지 않는다. 세면 경계가 늘 0 번 앞이 되어 한 번도 서지 않는다(로컬 런 138)."""
    frozen: set[tuple[str, int]] = set()
    messages = [
        HumanMessage(id="start", content="start\n" + detail_block("plan", "the plan")),
        delta(1),
        detail(2, 7),
    ]

    _, pending = supersede_case_context(messages, 7, frozen, batch=False)

    assert pending == 2


def test_계획_상세가_있어도_fold_자리_앞에_cache_경계가_선다() -> None:
    channel = QaRunChannel(qa_try_id=1, send=_swallow)
    state = QaRunState(total_steps=1)
    middleware = _fold_context_for(SCENE_VIEW_ONLY, THRESHOLD, state, channel, case_context=True)
    messages = [HumanMessage(id="start", content="start\n" + detail_block("plan", "the plan")), whole(1), delta(2), delta(3)]

    sent = call(middleware, messages)

    assert [index for index, message in enumerate(sent) if message.additional_kwargs.get(STABLE_KEY)] == [1]


def test_fold_한_호출은_지난_호출의_경계_자리에_다시_경계를_둔다() -> None:
    """Anthropic 은 경계에서 20 block 안쪽만 찾는다. `fold` 뒤 경계를 옮기면 지난 `cache` 에 닿지 않는다(로컬 런 161)."""
    channel = QaRunChannel(qa_try_id=1, send=_swallow)
    state = QaRunState(total_steps=1)
    middleware = _fold_context_for(SCENE_VIEW_ONLY, THRESHOLD, state, channel)
    marked = lambda sent: [message.id for message in sent if message.additional_kwargs.get(STABLE_KEY)]
    messages = [HumanMessage(id="start", content="start"), whole(1).model_copy(update={"id": "v1"})]
    for number in range(2, 4):
        messages.append(delta(number).model_copy(update={"id": f"v{number}"}))
    before = marked(call(middleware, messages))

    messages += [delta(number).model_copy(update={"id": f"v{number}"}) for number in range(4, 8)]
    folding = call(middleware, messages)
    after = call(middleware, [*messages, delta(8).model_copy(update={"id": "v8"})])

    assert folded(folding)
    assert marked(folding) == before
    assert marked(after) != before


# --- `quiet_unchanged`: 안 바뀐 조작 줄과 화면 글자는 다시 그리지 않는다 ------------------------


def _button(object_id: int, selector: str, text: str | None = None, hp: int | None = None) -> dict:
    members = [] if hp is None else [{"on": "Unit", "member": "Hp", "value": hp}]
    obj = {"id": object_id, "selector": selector, "members": members, "offers": {"clicks": [{"on": "Button", "method": "OnClick"}]}}
    if text is not None:
        obj["text"] = text
    return obj


def _quiet_pulse() -> PulseMemory:
    pulse = PulseMemory(quiet_unchanged=True)
    pulse.apply(PulseReading(reading=1, scene="Battle", whole=True, active=[_button(1, "Play[0]", "Play"), _button(2, "Stop[1]", "Stop")]))
    pulse.since_action(None)
    return pulse


def test_안_바뀐_조작_줄과_글자는_다음_view_에서_한_줄로_줄인다() -> None:
    pulse = _quiet_pulse()
    pulse.apply(PulseReading(reading=2, scene="Battle", active=[_button(1, "Play[0]", hp=3)]))

    view = pulse.since_action(None)

    assert "Play[0]" in view
    assert "Stop[1]" not in view
    assert "1 more objects can be acted on as listed in an earlier view" in view
    assert "unchanged labels" in view


def test_끄면_조작_줄을_매번_그린다() -> None:
    pulse = _quiet_pulse()
    pulse.quiet_unchanged = False
    pulse.apply(PulseReading(reading=2, scene="Battle", active=[_button(1, "Play[0]", hp=3)]))

    assert "Stop[1]" in pulse.since_action(None)


def test_fold_뒤에는_기준_view_이후_바뀐_조작_줄만_다시_그린다() -> None:
    pulse = _quiet_pulse()
    pulse.apply(PulseReading(reading=2, scene="Battle", active=[_button(3, "Pause[2]")]))
    pulse.since_action(None)
    pulse.apply(PulseReading(reading=3, scene="Battle", active=[_button(1, "Play[0]", hp=3)]))
    pulse.since_action(None)

    pulse.redraw_values_since(1)
    view = pulse.since_action(None)

    assert "Pause[2]" in view
    assert "Stop[1]" not in view


def test_씬_전체_view_는_조작_줄을_전부_그린다() -> None:
    pulse = _quiet_pulse()

    view = pulse.current_scene()

    assert "Play[0]" in view and "Stop[1]" in view


# --- `compact`: 판독 목록 묶기 · statics 는 바뀐 것만 ---------------------------------------


def _timer(reading: int, value: float) -> PulseReading:
    """판독마다 같은 세 값이 움직인다(타이머 · 회전 · 파티클). 한 종류만 움직이는 경우는 원래부터 묶였다."""
    members = [{"on": "Sky", "member": name, "value": value} for name in ("timer", "accTime", "spin")]
    return PulseReading(
        reading=reading,
        scene="Battle",
        active=[{"id": 9, "selector": "Sky[0]", "members": members}],
        changed=[f"Battle/Env[0]/Sky[0]|Sky::{name}" for name in ("timer", "accTime", "spin")],
    )


def test_같은_것이_움직인_판독은_한_줄로_묶고_이름을_줄인다() -> None:
    pulse = PulseMemory(compact=True)
    pulse.apply(PulseReading(reading=1, scene="Battle", whole=True, active=[]))
    pulse.since_action(None)
    for reading in range(2, 6):
        pulse.apply(_timer(reading, reading * 0.1))

    view = pulse.since_action(None)

    assert "2–5 (4 readings, each delta — Sky[0]|Sky::timer, Sky[0]|Sky::accTime, Sky[0]|Sky::spin)" in view
    assert "Battle/Env[0]/Sky[0]|Sky::timer" not in view


def test_statics_는_바뀐_것만_그리고_fold_뒤에는_다시_다_그린다() -> None:
    pulse = PulseMemory(compact=True)
    lock = {"declaring": "Core.InteractionLock", "member": "IsLocked", "value": True}
    other = {"declaring": "Core.Settings", "member": "Volume", "value": 3}
    pulse.apply(PulseReading(reading=1, scene="Battle", whole=True, statics=[lock, other]))
    pulse.since_action(None)
    pulse.apply(PulseReading(reading=2, scene="Battle", statics=[{**other, "value": 4}], changed=["Core.Settings::Volume"]))

    quiet = pulse.since_action(None)
    pulse.redraw_values_since(1)
    pulse.apply(PulseReading(reading=3, scene="Battle"))
    after_fold = pulse.since_action(None)

    assert "Volume = 4" in quiet and "IsLocked" not in quiet
    assert "1 unchanged since the earlier view" in quiet
    assert "Volume = 4" in after_fold
