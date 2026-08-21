"""판독 채널을 받아 상태의 출처로 삼는다 (ARTEL-401).

병합 규칙의 출처는 SDK 의 판독 뷰어(`artel-sdk/tools/watch-readings.py`)다. 이 채널을 읽는
쪽이 둘뿐이고, 둘이 델타를 다르게 해석하면 어느 한쪽이 틀린 것보다 나쁘다.
"""

from app.qa.envelope import MessageType
from app.qa.pulse import PulseMemory, PulseReading
from app.qa.scene import SceneMemory
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore


def reading(**over) -> dict:
    base = {
        "schema": 2,
        "reading": 1,
        "frame": 100,
        "scene": "TurnBattleScene",
        "whole": True,
        "statics": [],
        "active": [],
        "deactive": [],
        "changed": [],
        "watching": 3,
        "unresolved": 0,
        "unwatchable": 1126,
    }
    base.update(over)
    return base


def obj(selector="Canvas[2]/continue[1]", **over) -> dict:
    base = {
        "scene": "TurnBattleScene",
        "id": 26168,
        "path": "Canvas/continue",
        "selector": selector,
        "members": [
            {"on": "Battle.Turns.TurnBattleSystem", "member": "turn", "value": 2, "asked": True}
        ],
    }
    base.update(over)
    return base


def fold(*docs: dict) -> PulseMemory:
    memory = PulseMemory()
    for doc in docs:
        memory.apply(PulseReading.model_validate(doc))
    return memory


def test_전량_판독이_들고_있던_것을_갈아치운다():
    memory = fold(
        reading(active=[obj(selector="A")]),
        reading(reading=2, whole=True, active=[obj(selector="B")]),
    )

    assert [held.selector for held in memory.held.values()] == ["B"]
    assert memory.wholes == 2


def test_델타는_직전_상태_위에_얹힌다():
    memory = fold(
        reading(active=[obj(selector="A"), obj(selector="B")]),
        # B 만 움직였다고 말하는 델타. A 는 말하지 않으므로 그대로 남아야 한다.
        # `on` 을 첫 판독과 같게 두는 것이 요점이다 — 키가 `on::member#among` 이라
        # 다르게 쓰면 덮어쓰기가 아니라 다른 멤버가 하나 더 생긴다.
        reading(
            reading=2,
            whole=False,
            active=[
                obj(
                    selector="B",
                    members=[
                        {
                            "on": "Battle.Turns.TurnBattleSystem",
                            "member": "turn",
                            "value": 9,
                        }
                    ],
                )
            ],
            changed=["B|turn"],
        ),
    )

    assert sorted(held.selector for held in memory.held.values()) == ["A", "B"]
    b = next(h for h in memory.held.values() if h.selector == "B")
    assert [m.value for m in b.members.values()] == [9]
    # A 는 델타가 말하지 않았으므로 값이 그대로다.
    a = next(h for h in memory.held.values() if h.selector == "A")
    assert [m.value for m in a.members.values()] == [2]


def test_아무_말도_없는_객체는_있던_목록에_그대로_남는다():
    memory = fold(
        reading(deactive=[obj(selector="Off")]),
        reading(reading=2, whole=False, active=[obj(selector="On")]),
    )

    off = next(h for h in memory.held.values() if h.selector == "Off")
    on = next(h for h in memory.held.values() if h.selector == "On")
    assert off.live is False
    assert on.live is True


def test_같은_객체가_목록을_옮기면_그것이_따라간다():
    memory = fold(
        reading(active=[obj(selector="X")]),
        reading(reading=2, whole=False, deactive=[obj(selector="X")]),
    )

    assert next(iter(memory.held.values())).live is False


def test_창_안_변화_횟수가_쌓인다():
    memory = fold(
        reading(changed=["StageManager.turn"]),
        reading(reading=2, whole=False, changed=["StageManager.turn", "CardManager.hand"]),
        reading(reading=3, whole=False, changed=["StageManager.turn"]),
    )

    assert memory.moves["StageManager.turn"] == 3
    assert memory.moves["CardManager.hand"] == 1


def test_statics_는_객체_아래로_섞이지_않는다():
    memory = fold(
        reading(
            statics=[{"declaring": "Combat.Stage.StageDataSingleton", "member": "stage", "value": 3}],
            active=[obj()],
        )
    )

    assert list(memory.statics) == ["Combat.Stage.StageDataSingleton::stage"]
    held = next(iter(memory.held.values()))
    assert [m.member for m in held.members.values()] == ["turn"]


def test_한_객체의_같은_타입_컴포넌트_둘이_서로를_덮지_않는다():
    memory = fold(
        reading(
            active=[
                obj(
                    members=[
                        {"on": "DropZone", "member": "held", "among": 0, "value": 1},
                        {"on": "DropZone", "member": "held", "among": 1, "value": 2},
                    ]
                )
            ]
        )
    )

    held = next(iter(memory.held.values()))
    assert sorted(m.value for m in held.members.values()) == [1, 2]


def test_값을_해석하지_않고_그대로_들고_있는다():
    shapes = [
        {"on": "T", "member": "a", "value": {"path": "Canvas/x", "world": {"x": 1.5, "y": 2.0}}},
        {"on": "T", "member": "b", "value": {"sprite": "Sprite_Start"}},
        {"on": "T", "member": "c", "value": None},
        {"on": "T", "member": "d", "value": "문자열"},
    ]
    memory = fold(reading(active=[obj(members=shapes)]))

    held = next(iter(memory.held.values()))
    got = {m.member: m.value for m in held.members.values()}
    assert got["a"] == {"path": "Canvas/x", "world": {"x": 1.5, "y": 2.0}}
    assert got["b"] == {"sprite": "Sprite_Start"}
    assert got["c"] is None
    assert got["d"] == "문자열"


def test_판독이_없으면_아무것도_실리지_않는다():
    """구버전 SDK 호환 — 이것이 깨지면 프롬프트가 통째로 달라진다."""
    memory = SceneMemory()
    assert memory.pulse.render() is None
    assert memory.render_now() is None
    assert memory.render(0) == "No scene has been received yet."


def test_판독만_와도_보인다():
    memory = SceneMemory()
    memory.pulse.apply(PulseReading.model_validate(reading(active=[obj()])))

    now = memory.render_now()
    assert now is not None
    assert "TurnBattleSystem.turn" in now
    assert "could not read: 1126" in now


def test_모르는_필드가_와도_읽는다():
    """더하기만 하는 변경에 이 쪽이 먼저 깨지면 안 된다."""
    doc = reading(active=[obj(members=[{"on": "T", "member": "m", "value": 1, "새필드": "x"}])])
    doc["앞으로생길것"] = 1

    memory = fold(doc)

    assert memory.readings == 1


def test_service_가_PULSE_를_채널로_보낸다():
    service = QaExecutionService(store=InMemoryQaSessionStore())

    class Recorder:
        def __init__(self):
            self.got = None

        def on_pulse(self, raw):
            self.got = raw

    recorder = Recorder()
    service._channels["s"] = recorder

    assert service.deliver("s", {"type": MessageType.PULSE, "payload": reading()}) is True
    assert recorder.got is not None
    assert recorder.got["payload"]["scene"] == "TurnBattleScene"


def test_모르는_타입은_여전히_거절된다():
    """대조군.

    `deliver` 가 False 를 돌려주면 WS 핸들러가 `Unsupported inbound frame` 오류를 되쏜다
    (`app/api/qa_sessions.py`). 실제 스택으로 판독을 흘렸을 때 그 오류가 **없었다**는 것이
    "받아들여졌다"의 근거인데, 아무 타입이나 다 True 를 돌려준다면 그 근거가 성립하지
    않는다. 이 테스트가 그 전제를 지킨다.
    """
    service = QaExecutionService(store=InMemoryQaSessionStore())
    service._channels["s"] = object()

    assert service.deliver("s", {"type": "NOT_A_TYPE", "payload": {}}) is False
