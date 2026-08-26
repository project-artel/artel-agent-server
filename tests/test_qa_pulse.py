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


def test_꺼진_객체는_그리지_않는다():
    """화면에 없고 누를 수도 없는 것을 조준 후보로 권하지 않는다.

    `GAME_STATE` 도 활성 GameObject 만 보냈다(`SceneScanner`). 판독이 꺼진 것까지 그리면
    걷어내려는 그것보다 못해진다.
    """
    memory = fold(reading(active=[obj(selector="A[1]")], deactive=[obj(selector="B[1]")]))

    view = memory.render()
    assert "A[1]" in view
    assert "B[1]" not in view


def test_꺼진_객체도_들고_있는다():
    """그리지 않는 것과 버리는 것은 다르다.

    판독이 "그건 꺼졌다" 고 말한 것 자체가 사실이고, 버리면 다시 켜질 때 그 사이의 값을
    잃어 전량 판독을 기다리게 된다.
    """
    memory = fold(reading(deactive=[obj(selector="B[1]")]))

    held = next(iter(memory.held.values()))
    assert held.live is False
    assert held.members  # 값은 그대로 있다


def test_다시_켜지면_돌아온다():
    """판독이 그 객체를 active 통에 넣어 보내므로 저절로 돌아온다. 되살리는 코드가 따로
    있어야 하는 것이 아니다 — 어느 통에 들어가는가가 곧 그 진술이다."""
    memory = fold(
        reading(deactive=[obj(selector="B[1]")]),
        reading(reading=2, whole=False, active=[obj(selector="B[1]")]),
    )

    assert "B[1]" in memory.render()


def test_풀에서_꺼내_쓰는_씬에서_프롬프트가_자라지_않는다():
    """카드 스무 장짜리 풀에서 손에 든 셋만 활성이면 프롬프트에 드는 것도 셋이다.

    풀은 객체를 재사용하므로 selector 가 그대로다 — `held` 는 풀 크기에서 수렴하고,
    렌더는 활성 수만큼이다. 이 테스트가 없으면 "꺼진 것도 그린다" 로 되돌아갔을 때
    긴 전투에서만, 그것도 프롬프트 길이로만 드러난다.
    """
    pool = [f"Card(Clone)[{i}]" for i in range(1, 21)]
    hand, rest = pool[:3], pool[3:]
    memory = fold(
        reading(
            whole=True,
            active=[obj(selector=s) for s in hand],
            deactive=[obj(selector=s) for s in rest],
        )
    )
    def drawn(mem) -> int:
        """렌더에 이름이 오른 카드 수. 판독 로그 절은 자기 상한이 따로 있어 총 줄 수로는
        객체 증가를 가릴 수 없다."""
        return sum(1 for line in mem.render().splitlines() if "Card(Clone)[" in line)

    assert drawn(memory) == len(hand)

    # 한 장 내고 한 장 뽑기를 스무 번. 풀이 재사용되므로 새 키가 생기지 않는다.
    for turn in range(20):
        out, inn = hand.pop(0), rest.pop(0)
        hand.append(inn)
        rest.append(out)
        memory.apply(
            PulseReading.model_validate(
                reading(
                    reading=turn + 2,
                    whole=False,
                    active=[obj(selector=inn)],
                    deactive=[obj(selector=out)],
                )
            )
        )

    assert len(memory.held) == len(pool)   # 풀 크기에서 수렴한다
    assert drawn(memory) == len(hand)      # 손에 든 수만큼만 그린다


# --- 창을 타는 렌더와 상세 조회 (ARTEL-541) -----------------------------------


def _reading(n: int, *members, whole: bool = False, selector: str = "Enemy[1]") -> dict:
    """판독 하나. `members` 는 (이름, 값) 쌍이다."""
    return {
        "schema": 2,
        "reading": n,
        "scene": "Battle",
        "whole": whole,
        "active": [
            {
                "scene": "Battle",
                "id": -101,
                "selector": selector,
                "rect": {"x": 10, "y": 20, "w": 30, "h": 40},
                "offers": {"clicks": [{"event": "onClick", "method": "Enemy.Hit"}]},
                "members": [
                    {"on": "Enemy", "member": name, "value": value, "asked": True}
                    for name, value in members
                ],
            }
        ],
        "deactive": [],
        "changed": [f"Enemy::{name}" for name, _ in members],
    }


def test_a_value_that_did_not_move_again_is_not_drawn_again():
    """판독은 이미 델타다. 지난번에 그린 값을 매 턴 다시 적을 이유가 없다.

    이것이 이 변경의 전부다 — 실측에서 매 턴 5,148 토큰이던 것이 758 이 됐다. 객체 40개짜리
    샘플 게임의 전투에서 그랬고, 비용이 씬 크기에 비례하던 것을 변화량에 비례하게 만든다.
    """
    memory = PulseMemory()
    memory.apply(PulseReading.model_validate(_reading(1, ("Hp", 100), whole=True)))

    first = memory.render()
    assert "Hp = 100" in first

    # 아무것도 안 왔다. 값은 그대로지만 독자는 이미 읽었다.
    second = memory.render()
    assert "Hp = 100" not in second

    memory.apply(PulseReading.model_validate(_reading(2, ("Hp", 80))))
    third = memory.render()
    assert "Hp = 80" in third, "움직인 값은 다시 그린다"


def test_what_you_can_act_on_is_drawn_every_turn():
    """조작 가능한 것은 창과 무관하게 매번 그린다.

    **이 성질을 잃으면 줄인 것이 아니라 눈을 가린 것이다.** GAME_STATE 시절 에이전트가 화면을
    물어보느라 34턴을 쓰다 죽었고, 상시로 보이게 하니 4턴에 끝났다. 무엇을 누를 수 있는지는
    그 "묻지 않아도 보이는" 것의 절반이다.
    """
    memory = PulseMemory()
    memory.apply(PulseReading.model_validate(_reading(1, ("Hp", 100), whole=True)))
    memory.render()

    again = memory.render()
    assert "Enemy[1]" in again
    assert "id=-101" in again, "조준값이 남는다"
    assert "can do" in again, "무엇을 할 수 있는지가 남는다"
    assert "Hp" not in again, "값은 안 남는다 — 그것은 안 움직였다"


def test_a_run_of_identical_readings_folds_into_one_line():
    """한 멤버만 계속 움직이는 판독은 한 줄로 접는다.

    실측에서 전투 중 변화의 98%가 `SlimeAnimator::spriteRenderer` 하나였다. 애니메이션이
    스프라이트를 갈아 끼우는 것이지 QA 가 판정할 값이 아닌데, 그것이 판독 로그 열 줄을
    독차지해 프롬프트의 31%를 먹었다.
    """
    memory = PulseMemory()
    for n in range(1, 7):
        memory.apply(PulseReading.model_validate(_reading(n, ("sprite", f"frame{n}"))))

    out = memory.render()
    assert "readings, only" in out
    assert "Enemy::sprite" in out
    # 접혔으니 판독 번호가 줄줄이 나오지 않는다.
    assert out.count("(delta") == 0


def test_inspect_answers_with_everything_it_holds():
    """상시 블록이 안 싣는 값을 물으면 답한다.

    줄이는 것과 잃는 것의 차이가 여기 있다. 안 움직인 값을 매 턴 적지 않되, 스텝이 그 값에
    걸리면 가져올 수 있어야 한다.
    """
    memory = PulseMemory()
    memory.apply(
        PulseReading.model_validate(_reading(1, ("Hp", 100), ("MaxHp", 100), whole=True))
    )
    memory.render()  # 다 그렸다. 이제 창에는 아무것도 없다

    assert "Hp = 100" not in (memory.render() or "")
    found = memory.inspect("Enemy")
    assert "Hp = 100" in found
    assert "MaxHp = 100" in found


def test_inspect_takes_a_partial_address():
    """블록에 적히는 주소는 길고 대괄호 안 숫자는 바뀔 수 있다. 정확히 옮겨 적기를
    요구하면 그것 자체가 왕복을 만든다."""
    memory = PulseMemory()
    memory.apply(
        PulseReading.model_validate(
            _reading(1, ("Hp", 7), whole=True, selector="RangedCat(Clone)[17]")
        )
    )
    assert "Hp = 7" in memory.inspect("RangedCat")


def test_inspect_says_so_when_nothing_matches():
    """빈손을 조용히 돌려주면 에이전트가 그것을 '값이 없다'로 읽는다."""
    memory = PulseMemory()
    memory.apply(PulseReading.model_validate(_reading(1, ("Hp", 7), whole=True)))
    assert "No object matching" in memory.inspect("Nothing")


# --- 키가 무엇을 하는지 (ARTEL-539) -------------------------------------------


def _with_keys(keys) -> dict:
    return {
        "schema": 2,
        "reading": 1,
        "scene": "Map_scene",
        "whole": True,
        "active": [
            {
                "scene": "Map_scene",
                "id": -1,
                "selector": "MapScene[1]",
                "offers": {"keys": keys},
                "members": [],
            }
        ],
        "deactive": [],
        "changed": [],
    }


def test_a_key_says_what_it_does():
    """이름만으로는 다섯 키 중 무엇을 눌러야 할지 못 고른다.

    stage 에서 Map 씬의 QA 가 화살표와 Return 을 대등하게 받고 위/아래를 눌러 보다 전투에
    진입하지 못했다. 근거 문서는 `Return` 이 씬을 바꾼다는 것을 알고 있었고, 그 앎이 채널에서
    버려지고 있었다.
    """
    memory = PulseMemory()
    memory.apply(
        PulseReading.model_validate(
            _with_keys(
                [
                    {"key": "key:UpArrow (down)", "does": ["sets MapMove.position"]},
                    {
                        "key": "key:Return (down)",
                        "does": ["sets StageDataSingleton.stagePosition", "→ TurnBattleScene"],
                    },
                ]
            )
        )
    )

    out = memory.render()
    assert "key:Return (down) → sets StageDataSingleton.stagePosition; → TurnBattleScene" in out
    assert "key:UpArrow (down) → sets MapMove.position" in out


def test_a_key_whose_effect_is_unknown_says_so():
    """비어 있는 것을 '아무 일도 안 한다' 로 읽히게 두지 않는다.

    분석이 못 읽은 것과 정말 아무 일도 안 하는 것은 에이전트의 다음 수가 다르다 — 앞의 것은
    눌러 볼 값이 있고 뒤의 것은 없다.
    """
    memory = PulseMemory()
    memory.apply(PulseReading.model_validate(_with_keys([{"key": "key:Space (down)"}])))
    assert "key:Space (down) (effect unknown)" in (memory.render() or "")


def test_an_old_sdk_still_gets_its_keys_drawn():
    """구버전은 키를 문자열로 보낸다. 그 빌드도 무엇을 누를 수 있는지는 말할 수 있어야 한다."""
    memory = PulseMemory()
    memory.apply(PulseReading.model_validate(_with_keys(["key:Space (down)"])))
    out = memory.render() or ""
    assert "keys: key:Space (down)" in out
    assert "effect unknown" not in out, "모양이 다를 뿐 모르는 것이 아니다"


# --- SDK 가 접어 보내는 모양 (ARTEL-540 / ARTEL-552) ---------------------------


def _folded(scene: str = "Battle") -> dict:
    """새 모양: `on` 을 컴포넌트마다 한 번, 객체의 `scene` 은 생략, `type` 없음."""
    return {
        "schema": 2,
        "reading": 1,
        "scene": scene,
        "whole": True,
        "active": [
            {
                "id": -101,
                "selector": "Enemy[1]",
                "rect": {"x": 1, "y": 2, "w": 3, "h": 4},
                "by": [
                    {
                        "on": "Combat.Enemies.SwordEnemy",
                        "m": [
                            {"member": "Hp", "value": 20},
                            {"member": "MaxHp", "value": 20},
                        ],
                    },
                    {"on": "Combat.SlimeAnimator", "m": [{"member": "sprite", "value": "a"}]},
                ],
            }
        ],
        "deactive": [],
        "changed": [],
    }


def test_members_folded_by_component_are_spread_again():
    """`on` 을 멤버마다 되풀이하지 않는 것은 전송의 사정이다.

    한 문서에서 `on` 316개 중 295개가 같은 값이었다. 접는 것은 SDK 가 하고, 읽는 쪽은 펴서
    종전과 같은 것을 본다 — 병합도 렌더도 접힌 모양을 알 이유가 없다.
    """
    memory = PulseMemory()
    memory.apply(PulseReading.model_validate(_folded()))

    out = memory.render() or ""
    assert "SwordEnemy.Hp = 20" in out
    assert "SwordEnemy.MaxHp = 20" in out
    assert "SlimeAnimator.sprite = 'a'" in out


def test_an_object_without_a_scene_takes_the_readings():
    """객체의 `scene` 이 없으면 판독의 것이다. 다른 씬의 객체만 제 이름을 댄다.

    키가 `{scene}/{selector}` 라 이것을 안 채우면 같은 객체가 씬마다 다른 키로 앉는다.
    """
    memory = PulseMemory()
    memory.apply(PulseReading.model_validate(_folded(scene="Battle")))

    keys = list(memory.held)
    assert keys == ["Battle/Enemy[1]"], keys


def test_the_old_flat_shape_still_reads():
    """구버전 SDK 는 멤버를 평평하게 보낸다. 그 빌드가 붙어도 읽힌다."""
    memory = PulseMemory()
    memory.apply(
        PulseReading.model_validate(
            {
                "schema": 2,
                "reading": 1,
                "scene": "Battle",
                "whole": True,
                "active": [
                    {
                        "scene": "Battle",
                        "id": -101,
                        "selector": "Enemy[1]",
                        "members": [{"on": "SwordEnemy", "member": "Hp", "value": 20}],
                    }
                ],
                "deactive": [],
                "changed": [],
            }
        )
    )
    assert "SwordEnemy.Hp = 20" in (memory.render() or "")
