"""판독 채널을 받아 상태의 출처로 삼는다 (ARTEL-401).

병합 규칙의 출처는 SDK 의 판독 뷰어(`artel-sdk/tools/watch-readings.py`)다. 이 채널을 읽는
쪽이 둘뿐이고, 둘이 델타를 다르게 해석하면 어느 한쪽이 틀린 것보다 나쁘다.
"""

from app.qa.envelope import MessageType
from app.qa.pulse import MAX_CHANGED_NAMED, MAX_READING_LOG, PulseMemory, PulseReading
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


def test_판독이_들어온_순서대로_남는다():
    memory = fold(
        reading(reading=1, changed=["a"]),
        reading(reading=2, whole=False, changed=["b"]),
        reading(reading=3, whole=False, changed=[]),
    )

    assert [entry.reading for entry in memory.log] == [1, 2, 3]
    assert [entry.whole for entry in memory.log] == [True, False, False]
    assert memory.log[1].changed == ["b"]
    assert memory.log[2].changed == []
    assert memory.log[2].moved == 0
    assert memory.trimmed is False


def test_열_개를_넘으면_앞에서_버린다():
    """FIFO. 접힌 상태가 아니라 순서만 버린다 — 값은 이미 상태에 들어가 있다."""
    memory = fold(*[reading(reading=n, whole=(n == 1)) for n in range(1, 15)])

    assert len(memory.log) == MAX_READING_LOG
    # 최신 열 개가 순서대로 남는다.
    assert [entry.reading for entry in memory.log] == list(range(5, 15))
    assert memory.trimmed is True
    # 버린 것이 있다는 사실이 화면에 남아야 한다.
    assert "earlier readings dropped" in memory.render()


def test_한_판독은_변화가_몇_개든_항목_하나다():
    """상한은 판독 수에 걸린다. 한 문서가 여러 칸을 먹지 않는다."""
    memory = fold(reading(changed=[f"K{i}" for i in range(50)]))

    assert len(memory.log) == 1
    assert memory.log[0].moved == 50


def test_줄이_이름_대는_키에_상한이_있다():
    """전량 판독은 감시 중인 전부를 changed 에 담는다(실측 watching 111).

    상한이 없으면 로그 한 줄이 화면을 덮는다.
    """
    memory = fold(reading(reading=9, whole=False, changed=[f"Enemy{i}.hp" for i in range(23)]))

    entry = memory.log[0]
    assert entry.moved == 23
    assert len(entry.changed) == MAX_CHANGED_NAMED
    line = memory.render()
    assert "+15 more" in line
    assert "Enemy22.hp" not in line


def test_전량_판독은_키를_이름_대지_않는다():
    """직전이 없어 전부가 움직인 것으로 오므로, 그 목록은 '무엇을 보고 있나'에 답한다."""
    memory = fold(reading(reading=1, whole=True, changed=[f"K{i}" for i in range(111)]))

    line = memory.render()
    assert "whole — 111 values reported" in line
    assert "K0" not in line


def test_로그가_값을_두_번_들지_않는다():
    """같은 사실이 어긋날 자리를 둘 두지 않는다 — 값은 접힌 상태에만 있다."""
    memory = fold(reading(active=[obj()], changed=["TurnBattleSystem.turn"]))

    entry = memory.log[0]
    assert entry.changed == ["TurnBattleSystem.turn"]
    assert not hasattr(entry, "value")
    assert not hasattr(entry, "active")


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


# ── 조준값과 가능한 조작 (ARTEL-512) ─────────────────────────────────────────
#
# 판독은 이것들을 처음부터 싣고 있었다. 접는 과정에서 잃거나 그리지 않았을 뿐이라,
# 여기서 지키는 것은 "도착한 것이 프롬프트까지 간다" 하나다.


def test_조준값이_접히는_과정에서_살아남는다():
    """`id` 가 없으면 독자는 무엇이 바뀌었는지 알면서 그것을 건드릴 방법이 없다.

    판독이 `id` 를 매 기록에 싣는 이유가 그것이고(`LiveState.Object` 주석), 접을 때
    버리면 그 의도가 마지막 한 칸에서 무너진다.
    """
    memory = fold(reading(active=[obj(id=26168)]))

    held = next(iter(memory.held.values()))
    assert held.id == 26168
    assert "id=26168" in memory.render()


def test_조준값이_델타에서도_유지된다():
    """델타는 안 바뀐 것을 다시 말하지 않는다. `id` 는 결코 바뀌지 않으므로 델타에
    없을 수 있고, 그때 이전 값을 잃으면 눌 수 있던 것이 눌 수 없게 된다."""
    memory = fold(
        reading(active=[obj(id=26168)]),
        reading(reading=2, whole=False, active=[obj(id=None)]),
    )

    assert next(iter(memory.held.values())).id == 26168


def test_화면_사각형이_실린다():
    """좌표는 조준의 대체 수단이다. 컨트롤로 만들어지지 않은 것을 겨누는 유일한 방법이
    rect 이고, 판독이 그것을 싣는데 모델에 자리가 없어 사라지고 있었다."""
    memory = fold(reading(active=[obj(rect={"x": 860, "y": 600, "w": 200, "h": 60})]))

    assert "at 860,600 200x60" in memory.render()


def test_가능한_조작이_실린다():
    """`offers` 는 스캔이 볼 수 없는 둘을 준다 — 어떤 키가 뜻을 가지는가, 어떤 객체가
    포인터에 답하는가. 배선된 메서드 이름까지 함께 쓰는 것은, 누른 뒤 아무것도 움직이지
    않았을 때 무엇이 불렸어야 하는지를 아는 독자만 그것을 결함으로 부를 수 있기 때문이다.
    """
    memory = fold(
        reading(
            active=[
                obj(
                    offers={
                        "clicks": [
                            {"on": "TitleSceneManager", "event": "m_OnClick", "method": "StartGame"}
                        ],
                        "keys": ["space"],
                        "pointers": ["left"],
                    }
                )
            ]
        )
    )

    view = memory.render()
    assert "click → TitleSceneManager.StartGame" in view
    assert "keys: space" in view
    assert "pointer: left" in view


def test_멤버가_없어도_누를_것이면_그린다():
    """판독이 유일한 출처일 때, 감시 대상 멤버가 없고 조준값과 `offers` 만 들고 오는
    객체가 대다수다. 그것을 건너뛰면 누를 것이 화면에서 통째로 사라진다 — 실측에서
    에이전트가 아홉 턴을 조준값 찾는 데 쓰고 결국 화면 캡처로 좌표를 눈으로 찾았다.
    """
    memory = fold(
        reading(
            active=[
                obj(
                    members=[],
                    offers={"clicks": [{"on": "M", "event": "m_OnClick", "method": "Go"}]},
                )
            ]
        )
    )

    assert "Canvas[2]/continue[1]" in memory.render()


def test_아무것도_말하지_않는_객체는_여전히_빠진다():
    """상한이 있어야 한다. 조준값도 조작도 멤버도 없는 객체는 판독이 그것에 대해 할 말이
    없는 객체이고, 그것까지 그리면 화면이 배경으로 덮인다."""
    memory = fold(reading(active=[obj(id=None, members=[], offers=None)]))

    assert memory.render() is not None
    assert "Canvas[2]/continue[1]" not in memory.render()
