"""A flow that asks a value to climb without ever moving it (ARTEL-648).

Shapes taken from the measured runs, so a regression reads as the run that produced it.
"""

from app.agents.scenario.ordering import unreachable_climbs
from app.agents.scenario.schemas import (
    AuthoredStep,
    CaseGuard,
    ScenarioPlan,
    TestCaseListItem,
    ValueMove,
)

BATTLE = ValueMove(
    scene="TurnBattleScene",
    by="+1",
    how=None,  # no button: the player has to win
    when="wave >= BattleWaveController.battleScript.GetBattleWaveDatas().Count",
)


def map_case(case_id: int, stage: int, position: int) -> TestCaseListItem:
    """A Map_scene walk case, as run 208 had them."""
    return TestCaseListItem(
        id=case_id,
        scene="Map_scene",
        step="`RightArrow` 키를 누른다",
        expected_value="다음 지점으로 이동한다",
        verification_status="DRAFT",
        state_before=[
            CaseGuard(
                variable="StagePosition", operator=">=", value=str(stage),
                raised_in=["TurnBattleScene"], moves=[BATTLE],
            ),
            CaseGuard(
                variable="position", operator="==", value=str(position),
                raised_in=["Map_scene"],
                moves=[ValueMove(scene="Map_scene", by="+1", how="key:RightArrow")],
            ),
        ],
    )


CASES = [map_case(1636, 1, 0), map_case(1637, 2, 1), map_case(1639, 3, 2)]


def steps(*items) -> list[AuthoredStep]:
    return [
        AuthoredStep(action=text, case_id=case_id) for case_id, text in items
    ]


def test_climb_without_the_screen_that_raises_it_is_caught():
    """런 187·203·208 의 그 모양 — 전투를 한 번도 안 끼운 채 스테이지를 훑는다."""
    plan = ScenarioPlan(
        title="지도를 훑는다", description="",
        steps=steps((1636, "오른쪽으로"), (1637, "오른쪽으로"), (1639, "오른쪽으로")),
    )

    found = unreachable_climbs([plan], CASES)

    assert list(found) == [0]
    assert [c.variable for c in found[0]] == ["StagePosition", "StagePosition"]
    assert found[0][0].had == 1 and found[0][0].wants == 2
    assert found[0][0].where == ("TurnBattleScene",)


def test_a_step_through_the_raising_screen_pays_for_the_climb():
    """사이에 그 화면을 지나면 시나리오가 스스로 만든 것이다."""
    battle = TestCaseListItem(
        id=1654, scene="Map_scene", step="`Return` 키를 누른다",
        expected_value="전투 화면으로 전환된다", verification_status="DRAFT",
        state_after={"scene": "TurnBattleScene"},
    )
    plan = ScenarioPlan(
        title="싸우며 나아간다", description="",
        steps=steps(
            (1636, "오른쪽으로"), (1654, "전투에 들어간다"),
            (1637, "오른쪽으로"), (1654, "전투에 들어간다"),
            (1639, "오른쪽으로"),
        ),
    )

    assert unreachable_climbs([plan], CASES + [battle]) == {}


def test_a_flow_that_merely_starts_high_is_not_a_climb():
    """시작 조건은 오름이 아니다 — 첫 스텝의 요구는 안내가 말할 자리다."""
    plan = ScenarioPlan(
        title="보스 앞에서 시작한다", description="",
        steps=steps((1639, "오른쪽으로")),
    )

    assert unreachable_climbs([plan], CASES) == {}


def test_a_value_that_moves_on_this_very_screen_is_not_a_climb():
    """`position` 은 그 화면에서 방향키로 오른다. 사이에 넣을 것이 없다."""
    walk = [
        TestCaseListItem(
            id=700 + n, scene="Map_scene", step="`RightArrow` 키를 누른다",
            expected_value="이동한다", verification_status="DRAFT",
            state_before=[
                CaseGuard(
                    variable="position", operator="==", value=str(n),
                    raised_in=["Map_scene"],
                    moves=[ValueMove(scene="Map_scene", by="+1", how="key:RightArrow")],
                )
            ],
        )
        for n in range(3)
    ]
    plan = ScenarioPlan(
        title="걸어간다", description="",
        steps=steps((700, "오른쪽"), (701, "오른쪽"), (702, "오른쪽")),
    )

    assert unreachable_climbs([plan], walk) == {}


def test_nothing_to_check_without_a_case_list():
    """목록이 없으면 판단하지 않는다 — 없는 것을 틀렸다고 하지 않는다."""
    plan = ScenarioPlan(title="t", description="", steps=steps((1636, "오른쪽")))

    assert unreachable_climbs([plan], []) == {}
