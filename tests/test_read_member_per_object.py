"""`read_member` 의 값이 그 객체의 값인가. path 를 나눠 가진 holder 들이 서로를 덮지 않는다.

런 221: 두 번째 적 무리에서 쓰러져 꺼진 `RangedCat(Clone)` 과 새 `RangedCat(Clone)` 이 path 하나를 나눠 가졌고,
응답 순서상 나중에 온 쓰러진 적의 0 이 살아 있는 적의 10 을 덮어 "already defeated" 로 거절됐다.
"""

from app.agents.qa.macro.runner import ReadMemberAnswer, _apply_read_member
from tests.test_qa_pulse_contract import memory, obj, refusal, value

ALIVE = obj(path="RangedCat(Clone)", selector="RangedCat(Clone)[22]", id=2201)
DEFEATED = obj(path="RangedCat(Clone)", selector="RangedCat(Clone)[17]", id=1701)
ROCK = obj(path="MeleeRock(Clone)", selector="MeleeRock(Clone)[21]", id=2101)


def answer(*values: dict) -> ReadMemberAnswer:
    return ReadMemberAnswer(values=list(values))


def test_each_object_reads_its_own_value_whatever_order_the_holders_arrive_in() -> None:
    alive = {"path": "RangedCat(Clone)", "id": 2201, "selector": "RangedCat(Clone)[22]", "active": True, "value": 10}
    defeated = {"path": "RangedCat(Clone)", "id": 1701, "selector": "RangedCat(Clone)[17]", "active": False, "value": 0}
    for order in [(alive, defeated), (defeated, alive)]:
        m = memory([ALIVE, DEFEATED])
        _apply_read_member(m.pulse, "Combat.Enemies.Enemy::Hp", answer(*order))

        assert value(m, "Combat.Enemies.Enemy::Hp", "RangedCat(Clone)[22]") == 10
        assert value(m, "Combat.Enemies.Enemy::Hp", "RangedCat(Clone)[17]") == 0


def test_without_object_ids_a_shared_path_is_refused_rather_than_overwritten() -> None:
    """옛 SDK 는 id 를 보내지 않는다. 그때 같은 path 의 값 둘 중 하나를 고르지 않는다."""
    for order in [(10, 0), (0, 10)]:
        m = memory([ALIVE, DEFEATED])
        _apply_read_member(
            m.pulse,
            "Combat.Enemies.Enemy::Hp",
            answer(*({"path": "RangedCat(Clone)", "value": hp} for hp in order)),
        )

        failure = refusal(m, "Combat.Enemies.Enemy::Hp", "RangedCat(Clone)[22]")
        assert "2 objects share the path RangedCat(Clone)" in failure.reason


def test_without_object_ids_a_path_held_by_one_object_reads_as_before() -> None:
    m = memory([ALIVE, ROCK])
    _apply_read_member(
        m.pulse,
        "Combat.Enemies.Enemy::Hp",
        answer({"path": "RangedCat(Clone)", "value": 10}, {"path": "MeleeRock(Clone)", "value": 20}),
    )

    assert value(m, "Combat.Enemies.Enemy::Hp", "RangedCat(Clone)[22]") == 10
    assert value(m, "Combat.Enemies.Enemy::Hp", "MeleeRock(Clone)[21]") == 20


def test_the_run_221_answer_reads_the_living_enemies_alive() -> None:
    """런 221 마지막 turn 직전 응답(log 908788)에 SDK 가 이제 다는 id 를 붙인 것."""
    m = memory([ALIVE, DEFEATED, ROCK, obj(path="MeleeRock(Clone)", selector="MeleeRock(Clone)[20]", id=2001)])
    _apply_read_member(
        m.pulse,
        "Combat.Enemies.Enemy::Hp",
        answer(
            {"path": "RangedCat(Clone)", "id": 2201, "value": 10},
            {"path": "MeleeRock(Clone)", "id": 2101, "value": 20},
            {"path": "MeleeRock(Clone)", "id": 2001, "value": -1},
            {"path": "RangedCat(Clone)", "id": 1701, "value": 0},
        ),
    )

    assert value(m, "Combat.Enemies.Enemy::Hp", "RangedCat(Clone)[22]") > 0
    assert value(m, "Combat.Enemies.Enemy::Hp", "MeleeRock(Clone)[21]") > 0


def test_a_later_answer_replaces_the_whole_member_and_leaves_no_earlier_holder_value() -> None:
    m = memory([ALIVE, DEFEATED])
    _apply_read_member(
        m.pulse,
        "Combat.Enemies.Enemy::Hp",
        answer({"path": "RangedCat(Clone)", "id": 2201, "value": 10}, {"path": "RangedCat(Clone)", "id": 1701, "value": 0}),
    )
    _apply_read_member(
        m.pulse,
        "Combat.Enemies.Enemy::Hp",
        ReadMemberAnswer(values=[], unresolvable=[{"name": "Combat.Enemies.Enemy::Hp", "reason": "the getter threw"}]),
    )

    failure = refusal(m, "Combat.Enemies.Enemy::Hp", "RangedCat(Clone)[22]")
    assert "the getter threw" in failure.reason
