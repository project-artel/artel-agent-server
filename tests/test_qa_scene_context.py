"""The current scene arrives with what the project already knows about it.

Three things are pinned here, and they fail in different ways:

- **the rendering** — the block is the deliverable, so its text is asserted, not
  just its presence. Most of what this feature is worth lives in whether the
  agent reads the block as complete, which is a wording question.
- **the wiring** — a correct renderer nobody calls leaves the run exactly as
  blind as before, so a real `QaRunner.run` is driven and what the model actually
  received is inspected, the way `tests/test_qa_runner_context.py` does for the
  fold. Where the block rides is asserted here too: under the scene view a tool
  result carries and outside its markers, once per scene visit.
- **the failure path** — the lookup is advisory, so every way it can go wrong has
  to end with a running run and no block.
"""

import asyncio
from typing import Any

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.qa.compaction import render_progress_ledger
from app.agents.qa.context import FOLDED_VIEW_PREFIX
from app.api.qa_sessions import OpenQaSessionRequest
from app.agents.qa.runner import QaRunner
from app.agents.qa.tools import QaRunState
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType
from app.qa.scene import SCENE_VIEW_END, SCENE_VIEW_START_PREFIX
from app.qa.scene_context import (
    MAX_CAPABILITIES_IN_SCENE_CONTEXT,
    MAX_KNOWLEDGE_IN_SCENE_CONTEXT,
    SCENE_CONTEXT_END,
    SCENE_CONTEXT_START,
    SceneContext,
    fetch_scene_context,
)
from app.qa.schemas import QaCaseRef, QaRunScenario, QaScenario, QaStep
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore


def capability(**overrides: Any) -> dict:
    payload = {
        "capabilityId": "88",
        "capabilityKey": "cap-10",
        "summary": "공격 버튼으로 적을 공격한다",
        "givenText": "전투가 시작된 뒤",
        "interaction": "click",
        "inputKey": None,
        "controlPath": "Canvas/AttackButton",
        "controlLabel": "공격",
        "status": "runnable",
        "actionability": "runnable",
        "observability": "observable",
        "applicability": "applies",
        "verification": "unverified",
        "repeatUntilDone": False,
        "controlSelectorHint": "Canvas[2]/AttackButton[1]",
    }
    payload.update(overrides)
    return payload


def payload(**overrides: Any) -> dict:
    body = {
        "gameBuildId": "12",
        "contentMapId": "3",
        "capture": "editor",
        "scenes": [
            {
                "sceneName": "BattleScene",
                "knownToContentMap": True,
                "sceneSummary": "적과 싸우는 씬",
                "capabilities": [
                    capability(),
                    capability(
                        capabilityId="89",
                        capabilityKey=None,
                        summary="ESC 로 일시정지 메뉴를 연다",
                        givenText=None,
                        interaction="press_key",
                        inputKey="Escape",
                        controlPath=None,
                        controlLabel=None,
                        # 스키마가 실제로 내는 값이다(V45 의 유도 컬럼).
                        # 서버가 내지 않는 낱말을 픽스처에 쓰면 다음 사람이 그것을 어휘로 읽는다.
                        status="needs-probe",
                        verification="contradicted",
                        repeatUntilDone=True,
                        controlSelectorHint=None,
                    ),
                ],
                "knowledge": [
                    {"knowledgeId": "41", "summary": "전투 중 ESC 는 아무것도 하지 않는다"}
                ],
            },
            {
                "sceneName": "EmptyScene",
                "knownToContentMap": True,
                "capabilities": [],
                "knowledge": [],
            },
            {
                "sceneName": "GhostScene",
                "knownToContentMap": False,
                "capabilities": [],
                "knowledge": [{"knowledgeId": "77", "summary": "여기서는 저장이 되지 않는다"}],
            },
        ],
    }
    body.update(overrides)
    return body


def context() -> SceneContext:
    return SceneContext.model_validate(payload())


# --- the block ----------------------------------------------------------------


def test_a_scene_with_capabilities_and_knowledge_gets_both() -> None:
    """The block is the deliverable, so it is pinned whole.

    Every line is load-bearing: the boundary sentence, the capability line's
    shape, the knowledge line's id-and-summary, and the two parentheticals that
    say what to do with each list.
    """
    block = context().render("BattleScene")

    assert block == (
        f"{SCENE_CONTEXT_START}\n"
        "what is already known about this scene, and ONLY about this scene. "
        "Rules that hold across the game are not here — search_knowledge reaches those.\n"
        "the map describes it as: 적과 싸우는 씬\n"
        "\n"
        "the content map says this can be done here (2 known):\n"
        '  [cap-10] click "공격" (Canvas/AttackButton) — 공격 버튼으로 적을 공격한다 '
        "[runnable, unverified]  given: 전투가 시작된 뒤\n"
        "  press_key Escape — ESC 로 일시정지 메뉴를 연다 "
        "[needs-probe, contradicted, repeat until done]\n"
        "  (a path is where the map found the control, not something to aim at — "
        "take ids and coordinates from the scene view above)\n"
        "\n"
        "knowledge anchored to this scene, id and summary only:\n"
        "  [41] 전투 중 ESC 는 아무것도 하지 않는다\n"
        "  (search_knowledge on one of these brings back its full text, and its id is "
        "what report_step's used_knowledge_ids takes)\n"
        f"{SCENE_CONTEXT_END}"
    )


def test_the_block_says_it_is_not_the_whole_knowledge_base() -> None:
    """The boundary is stated by the block itself, not only by the prompt.

    A list in front of the agent is read as complete, and the prompt is the one
    part of the context that compaction can put behind a summary. So the sentence
    that says "anchored knowledge only, search for the rest" rides on every copy
    of the block, on every turn.
    """
    for scene in ("BattleScene", "EmptyScene", "GhostScene"):
        block = context().render(scene)
        assert "ONLY about this scene" in block
        assert "search_knowledge" in block


def test_a_scene_the_map_knows_with_nothing_on_it_says_exactly_that() -> None:
    """"Known and empty" and "unknown" are different answers and stay different.

    Collapsed into one, the agent reads a mapped scene with nothing on it as a
    scene nobody has ever mapped, and goes looking for a map that is already
    there and already empty.
    """
    block = context().render("EmptyScene")

    assert "the content map knows this scene and lists nothing that can be done here." in block
    assert "no knowledge is anchored to this scene." in block
    assert "never heard of this scene" not in block


def test_a_scene_the_map_never_heard_of_still_carries_its_knowledge() -> None:
    """An anchor may name a scene the content map has never seen (ARTEL-591).

    Anchors are stored without being checked against the map, so this is an
    ordinary state. The capabilities are genuinely absent; the knowledge is not,
    and it is the reason the scene is in the answer at all.
    """
    block = context().render("GhostScene")

    assert "the content map has never heard of this scene" in block
    assert "That is not a fault" in block
    assert "[77] 여기서는 저장이 되지 않는다" in block


def test_a_scene_the_lookup_says_nothing_about_gets_no_block() -> None:
    """No entry means no capabilities AND no anchors — the ordinary case.

    A line saying so, on every turn of every such scene, would be this block's
    largest single cost and its smallest contribution.
    """
    assert context().render("NeverHeardOfIt") is None
    assert context().render(None) is None


def test_a_near_miss_on_the_scene_name_is_not_a_match() -> None:
    """The scene name is the only key, so it is matched exactly.

    A game is free to ship `Battle` and `Battle 2`; a fuzzy match would hand the
    agent one screen's rules while it stands on another.
    """
    assert context().render("battlescene") is None
    assert context().render("BattleScene 2") is None


# --- the ceiling --------------------------------------------------------------


def test_a_long_capability_list_is_cut_and_says_so() -> None:
    """Silence would read as "that is all there is".

    The block is rewritten on every model call, so an unbounded list is paid once
    per turn for the whole run — the same reason `MAX_ACTIONS_IN_LIVE_VIEW` is ten
    rather than forty.
    """
    total = MAX_CAPABILITIES_IN_SCENE_CONTEXT + 5
    big = payload(
        scenes=[
            {
                "sceneName": "Busy",
                "knownToContentMap": True,
                "capabilities": [
                    capability(capabilityKey=f"cap-{index}") for index in range(total)
                ],
                "knowledge": [],
            }
        ]
    )
    block = SceneContext.model_validate(big).render("Busy")

    assert (
        f"showing {MAX_CAPABILITIES_IN_SCENE_CONTEXT} of {total} capabilities; 5 cut for space"
        in block
    )
    assert "[cap-0]" in block
    assert f"[cap-{MAX_CAPABILITIES_IN_SCENE_CONTEXT}]" not in block


def test_a_long_knowledge_list_is_cut_and_says_so() -> None:
    total = MAX_KNOWLEDGE_IN_SCENE_CONTEXT + 3
    big = payload(
        scenes=[
            {
                "sceneName": "Busy",
                "knownToContentMap": True,
                "capabilities": [],
                "knowledge": [
                    {"knowledgeId": str(index), "summary": f"사실 {index}"}
                    for index in range(total)
                ],
            }
        ]
    )
    block = SceneContext.model_validate(big).render("Busy")

    assert (
        f"showing {MAX_KNOWLEDGE_IN_SCENE_CONTEXT} of {total} entries; 3 cut for space"
        in block
    )
    assert "[0] 사실 0" in block
    assert f"[{MAX_KNOWLEDGE_IN_SCENE_CONTEXT}] 사실" not in block


def test_a_summary_that_runs_long_is_clipped_visibly() -> None:
    long = payload(
        scenes=[
            {
                "sceneName": "Wordy",
                "knownToContentMap": True,
                "capabilities": [],
                "knowledge": [{"knowledgeId": "1", "summary": "가" * 500}],
            }
        ]
    )
    block = SceneContext.model_validate(long).render("Wordy")

    assert "…" in block
    assert "가" * 500 not in block


# --- what comes off the wire --------------------------------------------------


def test_a_build_with_no_content_map_is_a_normal_answer() -> None:
    """`contentMapId: null` means nobody has uploaded evidence yet, not an error."""
    empty = SceneContext.model_validate(
        {"gameBuildId": "12", "contentMapId": None, "capture": None, "scenes": []}
    )

    assert empty.content_map_id is None
    assert empty.render("Anything") is None


def test_a_field_orchestration_adds_later_does_not_break_the_lookup() -> None:
    """A payload that grew a field must not cost the run its whole block."""
    grown = payload()
    grown["somethingNew"] = 1
    grown["scenes"][0]["capabilities"][0]["screenId"] = "9"

    assert SceneContext.model_validate(grown).render("BattleScene") is not None


def _stub_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")


def test_the_lookup_asks_the_endpoint_ARTEL_611_opened() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=payload())

    async def go() -> SceneContext | None:
        async with _stub_client(handler) as client:
            return await fetch_scene_context(
                project_id=5,
                game_build_id=12,
                qa_try_id=7,
                base_url="http://orchestration:8081",
                client=client,
            )

    result = asyncio.run(go())

    assert seen["url"] == (
        "http://orchestration:8081/internal/projects/5/game-builds/12/scene-context?qaTryId=7"
    )
    assert result is not None
    assert result.render("BattleScene") is not None


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(lambda request: httpx.Response(500), id="server error"),
        pytest.param(lambda request: httpx.Response(404), id="not found"),
        pytest.param(lambda request: httpx.Response(200, text="not json"), id="unreadable"),
        pytest.param(
            lambda request: httpx.Response(200, json={"scenes": "not a list"}),
            id="wrong shape",
        ),
    ],
)
def test_a_lookup_that_fails_answers_none_rather_than_raising(handler) -> None:
    """Every way this can go wrong ends the same way: no block, and a live run.

    A run that cannot start because an advisory lookup failed is worse than a run
    without the advice.
    """

    async def go() -> SceneContext | None:
        async with _stub_client(handler) as client:
            return await fetch_scene_context(
                project_id=5, game_build_id=12, base_url="http://x", client=client
            )

    assert asyncio.run(go()) is None


def test_a_lookup_that_cannot_reach_orchestration_answers_none() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing listening", request=request)

    async def go() -> SceneContext | None:
        async with _stub_client(refuse) as client:
            return await fetch_scene_context(
                project_id=5, game_build_id=12, base_url="http://x", client=client
            )

    assert asyncio.run(go()) is None


def test_no_ids_means_no_lookup_at_all() -> None:
    """Orchestration does not send these yet, so this is today's live path.

    It lands where a failed lookup lands, deliberately: the feature ships dark
    rather than shipping a run that refuses to open.
    """

    def explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no lookup should have been attempted")

    async def go() -> list[SceneContext | None]:
        async with _stub_client(explode) as client:
            return [
                await fetch_scene_context(
                    project_id=None, game_build_id=12, base_url="http://x", client=client
                ),
                await fetch_scene_context(
                    project_id=5, game_build_id=None, base_url="http://x", client=client
                ),
                await fetch_scene_context(
                    project_id=5, game_build_id=12, base_url=None, client=client
                ),
            ]

    assert asyncio.run(go()) == [None, None, None]


# --- the wiring ---------------------------------------------------------------


class ScriptedModel(BaseChatModel):
    """Returns one scripted tool call per turn, and records what it was given."""

    turns: list[dict[str, Any]]
    received: list[list[BaseMessage]] = []
    calls: int = 0
    # Run before each turn is answered, so a test can move the game between two
    # model calls. `observe_scene` sends no frame of its own since ARTEL-516, so
    # a scene change cannot be driven off the socket here.
    before_turn: Any = None

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.received.append(list(messages))
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        if self.before_turn is not None:
            self.before_turn(self.calls)
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=turn.get("content", ""),
                        tool_calls=turn.get("tool_calls", []),
                    )
                )
            ]
        )


def make_channel() -> QaRunChannel:
    """A channel that answers every action the way the game would.

    프레임은 게임이 스스로 올린다 — 붙자마자 한 장 올리고, 액션이 나갈 때마다 그것이
    만든 화면을 다시 올린다.
    """
    current = {"scene": "BattleScene"}

    def push(scene: str | None = None) -> None:
        if scene is not None:
            current["scene"] = scene
        channel.on_game_state(
            {
                "type": "GAME_STATE",
                "payload": {
                    "scene": current["scene"],
                    "interactables": [{"id": 1, "name": "Button", "type": "button"}],
                    "observables": {},
                },
            }
        )

    async def send(frame: dict) -> None:
        if frame["type"] != MessageType.ACTION.value:
            return
        push()
        channel.on_action_result(
            {
                "correlationId": frame["messageId"],
                "payload": {
                    "results": [
                        {"id": action["id"], "success": True}
                        for action in frame["payload"]["actions"]
                    ]
                },
            }
        )

    channel = QaRunChannel(qa_try_id=1, send=send)
    channel.push_scene = push  # type: ignore[attr-defined]
    return channel


def scenario() -> QaScenario:
    return QaScenario(
        title="씬 확인",
        description="씬을 확인한다.",
        steps=[
            QaStep(
                action="관찰",
                case_id=1,
                case=QaCaseRef(id=1, test_step="관찰", expected="화면이 보인다"),
            )
        ],
    )


def drive(
    monkeypatch: pytest.MonkeyPatch, scenes: list[str], scene_context: SceneContext | None
) -> tuple[ScriptedModel, QaRunChannel]:
    model = ScriptedModel(
        turns=[
            {"tool_calls": [{"name": "observe_scene", "args": {"step": 1, "thought": "본다"}, "id": "1"}]},
            {"tool_calls": [{"name": "observe_scene", "args": {"step": 1, "thought": "또 본다"}, "id": "2"}]},
            {
                "tool_calls": [
                    {
                        "name": "report_step",
                        "args": {"step": 1, "passed": True, "message": "봤다", "thought": "판정"},
                        "id": "3",
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish_run",
                        "args": {"passed": True, "summary": "끝", "thought": "종료"},
                        "id": "4",
                    }
                ]
            },
            {"content": "done"},
        ]
    )
    monkeypatch.setattr(
        "app.agents.qa.runner.build_chat_model",
        lambda _model, reasoning=None, **_: model,
    )
    channel = make_channel()
    # 서비스가 시나리오 시작 전에 하는 그대로. 런너를 거치지 않는다 — 화면을 그리는
    # 자리가 `SceneMemory` 하나뿐이라, 블록도 거기 얹힌다.
    channel.scene.scene_context = scene_context
    # One scene per model call, the last repeating: the run stands where `scenes`
    # says it stands when each turn is answered.
    model.before_turn = lambda call: channel.push_scene(scenes[min(call - 1, len(scenes) - 1)])
    model.before_turn(1)
    state = QaRunState(total_steps=1)

    asyncio.run(QaRunner().run(channel, scenario(), state))

    assert state.finished
    return model, channel


def scene_views(messages: list[BaseMessage]) -> list[str]:
    """화면이 모델에게 닿는 유일한 자리 — 도구 결과. 라이브 꼬리는 ARTEL-621 이 없앴다.

    `fold` 된 것도 센다. `fold_stale_scenes` 는 씬 뷰 마커 사이를 자리표로 바꾸므로,
    그런 메시지에는 시작 마커가 남지 않는다 — 그런데 블록은 그 마커 밖에 있어 그대로
    남고, 남는다는 것이 여기서 확인하려는 것 중 하나다.
    """
    return [
        str(m.content)
        for m in messages
        if isinstance(m, ToolMessage)
        and (
            SCENE_VIEW_START_PREFIX in str(m.content)
            or FOLDED_VIEW_PREFIX in str(m.content)
        )
    ]


def test_the_block_rides_under_the_scene_view_the_model_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """씬 뷰 아래, 그리고 그 마커 **밖**이다.

    위가 게임이 지금 하고 있는 것이고 이것은 그것을 어디서 하고 있는지에 대한 문서다.
    마커 밖인 것은 `fold_stale_scenes` 가 그 마커 쌍 사이를 통째로 자리표로 바꾸기
    때문이다 — 안에 넣으면 씬 뷰 하나만 남기는 `fold` 에 블록도 함께 사라진다.
    """
    model, _channel = drive(monkeypatch, ["BattleScene"], context())

    # 두 번째 호출이 받은 것. 씬 뷰가 아직 하나뿐이라 `fold` 가 손대지 않은 상태다.
    views = scene_views(model.received[1])
    assert len(views) == 1
    first = views[0]
    assert "scene: BattleScene" in first
    assert SCENE_CONTEXT_START in first
    assert SCENE_CONTEXT_END in first
    assert "[41] 전투 중 ESC 는 아무것도 하지 않는다" in first
    # 마커 밖: 씬 뷰가 닫힌 **뒤**에 온다.
    assert first.index(SCENE_VIEW_END) < first.index(SCENE_CONTEXT_START)


def test_the_block_is_drawn_once_per_scene_visit_not_once_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """도구 결과는 대화에 쌓인다. 매번 그리면 한 씬에 머문 턴 수만큼 같은 문단이 남는다.

    종전에는 매 모델 호출 뒤에 붙었다 사라지는 꼬리라 이 질문이 없었다. 그 꼬리를
    ARTEL-621 이 없앴고, 남는 자리에 그리게 된 이상 한 번이 맞는 횟수다.
    """
    model, _channel = drive(monkeypatch, ["BattleScene"], context())

    views = scene_views(model.received[-1])
    assert len(views) >= 2
    assert [SCENE_CONTEXT_START in view for view in views] == [True] + [False] * (
        len(views) - 1
    )


def test_the_block_survives_the_fold_that_swallows_the_view_it_sits_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fold` 되는 것은 화면이고, 블록은 그 화면이 무엇인지에 대한 설명이다.

    `fold_stale_scenes` 는 가장 새 씬 뷰 하나만 전문으로 남긴다. 블록을 실은 것은 그
    씬에 처음 들어선 도구 결과이므로, 마커 안에 있었다면 두 번째 씬 뷰가 도착하는
    순간 사라졌을 것이다.
    """
    model, _channel = drive(monkeypatch, ["BattleScene"], context())

    sent = model.received[-1]
    folded = [
        str(m.content)
        for m in sent
        if isinstance(m, ToolMessage) and FOLDED_VIEW_PREFIX in str(m.content)
    ]
    assert folded, "가장 새 씬 뷰 말고는 `fold` 돼 있어야 한다"
    assert any(SCENE_CONTEXT_START in view for view in folded)
    assert any("[41] 전투 중 ESC 는 아무것도 하지 않는다" in view for view in folded)


def test_the_block_follows_the_scene_when_the_scene_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """씬이 바뀌면 그 씬 조각이 새로 그려진다. 씬 이름을 캐싱하지 않는 것이 그 자체로
    전환 처리다.

    두 도구 결과를 씬 이름이 아니라 **블록 내용**으로 가른다. 앞의 것은 `fold` 돼 있고,
    씬 이름은 `fold` 되는 마커 안에 있어 남지 않는다 — 블록은 마커 밖이라 남는다.
    """
    model, _channel = drive(monkeypatch, ["BattleScene", "GhostScene"], context())

    views = scene_views(model.received[-1])
    assert len(views) >= 2
    first, second = views[0], views[1]

    assert "전투 중 ESC 는 아무것도 하지 않는다" in first
    assert "여기서는 저장이 되지 않는다" not in first

    assert "여기서는 저장이 되지 않는다" in second
    assert "전투 중 ESC 는 아무것도 하지 않는다" not in second

    # 살아 있는 뷰는 **맨 뒤**다. ARTEL-635 로 `report_step` 도 화면을 들고 오므로 그것이
    # 마지막 뷰가 되고, 앞의 것은 접힌다(`DEFAULT_KEEP_SCENES = 1`). 이 테스트가 확인하는
    # 것은 그대로다 — 씬이 바뀌면 그 씬의 블록이 새로 그려지고, 블록은 접혀도 남는다.
    assert "scene: GhostScene" in views[-1]


def test_a_run_without_a_lookup_still_runs_and_reads_the_scene(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure path, end to end: no block, and everything else unchanged."""
    model, _channel = drive(monkeypatch, ["BattleScene"], None)

    views = scene_views(model.received[-1])
    assert views
    assert all(SCENE_CONTEXT_START not in view for view in views)
    assert any("scene: BattleScene" in view for view in views)


def test_a_scene_the_lookup_does_not_cover_leaves_the_view_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model, _channel = drive(monkeypatch, ["SomeOtherScene"], context())

    views = scene_views(model.received[-1])
    assert views
    assert all(SCENE_CONTEXT_START not in view for view in views)


def test_the_compaction_ledger_carries_the_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """화면과 같은 이유로, 그리고 이쪽이 더 급하다.

    화면은 다음 도구 결과가 다시 그리지만, 블록은 씬에 들어설 때 한 번만 그려진다.
    그것을 실은 도구 결과가 요약으로 대체되면 그 씬에 머무는 동안 다시 오지 않는다
    (ARTEL-622 가 화면에 대해 세운 것과 같은 보장).
    """
    _model, channel = drive(monkeypatch, ["BattleScene"], context())

    ledger = render_progress_ledger(QaRunState(total_steps=1), channel)

    assert "The screen as it stands right now:" in ledger
    assert SCENE_CONTEXT_START in ledger
    assert "[41] 전투 중 ESC 는 아무것도 하지 않는다" in ledger


def test_the_ledger_does_not_say_the_block_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """씬이 막 바뀐 자리에서 압축이 걸리면 원장의 `render` 자신이 블록을 그린다."""
    _model, channel = drive(monkeypatch, ["BattleScene"], context())
    # 씬이 막 바뀐 상태로 되돌린다: 다음 렌더가 블록을 그린다.
    channel.scene.scene_context_drawn_for = None

    ledger = render_progress_ledger(QaRunState(total_steps=1), channel)

    assert ledger.count(SCENE_CONTEXT_START) == 1


# --- the session path ---------------------------------------------------------


class SilentResetPolicy:
    """Skips the between-scenario reset, which nothing here is about.

    The real policy sends a frame and waits out its timeout against a `send` that
    goes nowhere, which costs this file half a minute for a wait no assertion
    reads.
    """

    async def between_scenarios(self, channel, completed_index, total) -> None:
        return None


class RecordingRunner:
    """Records the scene context each scenario was started with.

    Off the channel rather than off a runner argument: the lookup is handed to
    `SceneMemory`, which is the one place a screen is drawn from.
    """

    def __init__(self, seen: list) -> None:
        self._seen = seen

    async def run_with_deadline(self, channel, scenario):
        self._seen.append((channel.qa_try_id, channel.scene.scene_context))
        return None, None


def test_the_lookup_happens_once_per_scenario_before_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once at the start of a run, not per turn and not per observation.

    Per scenario rather than per session because the knowledge scope is a
    `qa_try`'s: one lookup reused across a session's scenarios would show the
    second scenario what the first one's try could see.
    """
    asked: list[tuple] = []

    async def fake_fetch(project_id, game_build_id, qa_try_id=None, **_):
        asked.append((project_id, game_build_id, qa_try_id))
        return context()

    monkeypatch.setattr("app.qa.service.fetch_scene_context", fake_fetch)

    async def go() -> list:
        seen: list = []
        service = QaExecutionService(
            store=InMemoryQaSessionStore(),
            runner_factory=lambda *, config: RecordingRunner(seen),
            reset_policy=SilentResetPolicy(),
        )
        session_id, _ = await service.open(
            qa_run_id=9,
            game_instance_id=1,
            scenarios=[
                QaRunScenario(qa_try_id=101, test_scenario_id=1, scenario=scenario()),
                QaRunScenario(qa_try_id=102, test_scenario_id=2, scenario=scenario()),
            ],
            project_id=5,
            game_build_id=12,
        )

        async def send(_frame: dict) -> None:
            return None

        await service.run(session_id, send)
        return seen

    seen = asyncio.run(go())

    assert asked == [(5, 12, 101), (5, 12, 102)]
    assert [try_id for try_id, _ in seen] == [101, 102]
    assert all(ctx is not None for _, ctx in seen)


def test_a_session_opened_without_the_ids_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Today's live path: Orchestration's session open carries neither id yet.

    It has to degrade to a run with no block rather than to a run that will not
    open, which is also exactly what a failed lookup does.
    """
    async def go() -> list:
        seen: list = []
        service = QaExecutionService(
            store=InMemoryQaSessionStore(),
            runner_factory=lambda *, config: RecordingRunner(seen),
        )
        session_id, _ = await service.open(
            qa_run_id=9,
            game_instance_id=1,
            scenarios=[
                QaRunScenario(qa_try_id=101, test_scenario_id=1, scenario=scenario())
            ],
        )

        async def send(_frame: dict) -> None:
            return None

        await service.run(session_id, send)
        return seen

    assert asyncio.run(go()) == [(101, None)]


def test_the_open_request_carries_the_ids_through_to_the_record() -> None:
    request = OpenQaSessionRequest.model_validate(
        {
            "context": {
                "qa_try_id": 7,
                "game_instance_id": 1,
                "test_scenario_id": 1,
                "scenario": scenario().model_dump(),
                "project_id": 5,
                "game_build_id": 12,
            }
        }
    )

    assert request.context.project_id == 5
    assert request.context.game_build_id == 12

    # And a caller that does not send them — every caller today — still parses.
    without = OpenQaSessionRequest.model_validate(
        {
            "context": {
                "qa_try_id": 7,
                "game_instance_id": 1,
                "test_scenario_id": 1,
                "scenario": scenario().model_dump(),
            }
        }
    )
    assert without.context.project_id is None
    assert without.context.game_build_id is None
