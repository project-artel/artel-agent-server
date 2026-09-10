"""화면 도달표(흐름 없는 저작 실험).

케이스에 붙는 `exits` 는 한 걸음만 답해서, 모델이 두 걸음 너머를 "길 없음"으로
읽는다 — 짝 행렬이 고쳐 주던 헛막힘 1,126칸이 그 종류였다. 지도 간선 전체를 접어
(`fold_scene_routes`) 게임의 모양 블록에 싣고, 게임을 켰을 때의 값도 함께 싣는다.

접기는 순수 함수다: 점수도 상수도 없고, 같은 지도면 바이트까지 같은 줄을 낸다
(프롬프트 캐시가 앞부분 일치로만 재활용되기 때문이다).
"""

import asyncio

from app.agents.scenario.cases import fold_scene_routes, render_game_shape
from app.agents.scenario.schemas import SceneEdge, TestCaseListItem
from tests.test_sessions import _service


def _edges() -> list[SceneEdge]:
    return [
        SceneEdge(from_scene="TitleScene", to_scene="StoryScene"),
        SceneEdge(from_scene="StoryScene", to_scene="Map_scene"),
        SceneEdge(from_scene="Map_scene", to_scene="TurnBattleScene", by="Return"),
        SceneEdge(from_scene="Map_scene", to_scene="TitleScene"),
        SceneEdge(from_scene="TurnBattleScene", to_scene="GameClearScene"),
        SceneEdge(from_scene="GameClearScene", to_scene="Map_scene"),
    ]


def _case() -> TestCaseListItem:
    return TestCaseListItem(
        id=1,
        scene="Map_scene",
        step="Return 키를 누른다",
        expected_value="전투 화면이 열린다",
        verification_status="UNVERIFIED",
    )


def test_folds_multi_step_routes_with_via_and_presses() -> None:
    lines = fold_scene_routes(_edges())

    # 한 걸음: 무엇을 눌러서 가는지가 그대로 실린다.
    assert "  Map_scene → TurnBattleScene: 1 step [Return]" in lines
    # 두 걸음 너머: `exits` 만으로는 "길 없음"으로 읽히던 자리가 경유지와 함께 풀린다.
    assert (
        "  Map_scene → StoryScene: 2 steps, via TitleScene [on its own · on its own]"
        in lines
    )


def test_prints_unreachable_pairs_instead_of_going_silent() -> None:
    # 없는 길을 침묵으로 두면 "안 적음"과 "없음"이 다시 섞인다 — 이 표가 없애려는
    # 바로 그 오독이다. 지도가 모르는 짝은 모른다고 적는다.
    one_way = [SceneEdge(from_scene="A", to_scene="B")]

    lines = fold_scene_routes(one_way)

    assert "  B → A: no route the map knows" in lines


def test_same_map_folds_to_the_same_bytes() -> None:
    edges = _edges()

    assert fold_scene_routes(edges) == fold_scene_routes(list(reversed(edges)))


def test_shape_block_carries_routes_and_boot_values() -> None:
    block = render_game_shape(
        [_case()],
        entry_scene="TitleScene",
        scene_edges=_edges(),
        starting_values={"StagePosition": "-1", "position": "0"},
    )

    assert "Routes between screens" in block
    assert "Map_scene → StoryScene: 2 steps" in block
    assert "When the game boots: StagePosition = -1, position = 0" in block


def test_shape_block_unchanged_when_nothing_extra_arrives() -> None:
    # 구버전 오케(안 보내는 쪽)와의 계약: 없는 것은 지어내지 않고 조용히 빠진다.
    block = render_game_shape([_case()], entry_scene="TitleScene")

    assert "Routes between screens" not in block
    assert "When the game boots" not in block


def test_open_freezes_scene_edges_and_starting_values_on_the_record() -> None:
    # flows 가 API 경계에서 소리 없이 떨어졌던 자리다 — 세션 기록까지 실려 얼어붙는지
    # 못박는다.
    service, _agent, store = _service()

    session_id = asyncio.run(
        service.open(
            {},
            {},
            "첫 전투를 봐 줘",
            scene_edges=_edges(),
            starting_values={"StagePosition": "-1"},
        )
    )
    record = asyncio.run(store.load(session_id))

    assert record is not None
    assert [edge.to_scene for edge in record.scene_edges[:1]] == ["StoryScene"]
    assert record.starting_values == {"StagePosition": "-1"}
