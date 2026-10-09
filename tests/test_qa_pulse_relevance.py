"""pulse member 의 gameplay 여부를 결정 모델로 판정하는 judge 검사 (ARTEL-958). 네트워크 없음."""

import asyncio

# app.qa 를 먼저 import 하면 app.agents 와 순환한다(기존 문제). app.agents 를 먼저 읽어 피한다.
import app.agents  # noqa: F401

from app.qa import relevance as rel
from app.qa.pulse import PulseMemory, PulseReading
from app.qa.relevance import PulseRelevanceJudge


def _member(on: str, member: str, value=1) -> dict:
    return {"on": on, "member": member, "value": value, "asked": True}


def _obj(selector: str, members: list[dict], **over) -> dict:
    base = {
        "scene": "TurnBattleScene",
        "id": 1,
        "path": selector,
        "selector": selector,
        "members": members,
    }
    base.update(over)
    return base


def _memory(*objects: dict, reading: int = 1, whole: bool = True) -> PulseMemory:
    memory = PulseMemory()
    _apply(memory, *objects, reading=reading, whole=whole)
    return memory


def _apply(memory, *objects, reading=1, whole=False):
    doc = {
        "schema": 2,
        "reading": reading,
        "frame": 100,
        "scene": "TurnBattleScene",
        "whole": whole,
        "statics": [],
        "active": list(objects),
        "deactive": [],
        "changed": [],
        "watching": 1,
        "unresolved": 0,
        "unwatchable": 0,
    }
    memory.apply(PulseReading.model_validate(doc))


class _Fake:
    def __init__(self, probability=0.9, fail=False, gate: asyncio.Event | None = None):
        self.bodies: list[dict] = []
        self.probability = probability
        self.fail = fail
        self.gate = gate

    async def __call__(self, body: dict) -> dict:
        self.bodies.append(body)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("boom")
        return {
            "answers": {
                name: {"type": "noul", "noul": self.probability} for name in body["questions"]
            },
            "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0.001},
        }


def _run(coro):
    return asyncio.run(coro)


def _types(count: int) -> list[dict]:
    return [_obj(f"O{i}", [_member(f"Game.T{i}", "hp")], id=i) for i in range(count)]


def test_같은_member_type_은_질문_하나다():
    memory = _memory(
        _obj("A", [_member("Game.Card", "cost")], id=1),
        _obj("B", [_member("Game.Card", "cost")], id=2),
    )
    fake = _Fake()

    async def go():
        judge = PulseRelevanceJudge(fake)
        judge.notice(memory)
        await judge.drain()

    _run(go())

    assert len(fake.bodies) == 1
    assert list(fake.bodies[0]["questions"]) == ["m0"]


def test_요청_본문_모양과_확률_기록():
    memory = _memory(_obj("Enemy", [_member("Battle.Enemy", "Hp", 7)]))
    fake = _Fake(probability=0.96)

    async def go():
        judge = PulseRelevanceJudge(fake)
        judge.notice(memory)
        await judge.drain()

    _run(go())

    body = fake.bodies[0]
    assert body["model"] == rel.RELEVANCE_MODEL
    assert body["state"] == {"scene": "TurnBattleScene"}
    question = body["questions"]["m0"]
    assert question["type"] == "noul"
    assert question["criteria"] == {"true": "gameplay state", "false": "cosmetic or internal noise"}
    assert "Enemy.Hp (current value 7)" in question["instructions"]
    assert memory.relevance == {"Battle.Enemy::Hp": 0.96}


def test_이미_판정한_key_는_다시_묻지_않는다():
    memory = _memory(_obj("A", [_member("Game.Card", "cost")]))
    fake = _Fake()

    async def go():
        judge = PulseRelevanceJudge(fake)
        judge.notice(memory)
        await judge.drain()
        judge.notice(memory)
        await judge.drain()
        assert judge.samples(memory) == []

    _run(go())
    assert len(fake.bodies) == 1


def test_실패하면_비워_두고_MAX_ATTEMPTS_번에서_멈춘다():
    memory = _memory(_obj("A", [_member("Game.Card", "cost")]))
    fake = _Fake(fail=True)

    async def go():
        judge = PulseRelevanceJudge(fake)
        for _ in range(rel.MAX_ATTEMPTS + 3):
            judge.notice(memory)
            await judge.drain()

    _run(go())

    assert memory.relevance == {}
    assert len(fake.bodies) == rel.MAX_ATTEMPTS


def test_41_type_은_두_번_호출된다():
    memory = _memory(*_types(41))
    fake = _Fake()

    async def go():
        judge = PulseRelevanceJudge(fake)
        judge.notice(memory)
        await judge.drain()

    _run(go())

    assert [len(body["questions"]) for body in fake.bodies] == [40, 1]
    assert len(memory.relevance) == 41


def test_loop_없이_notice_하면_아무것도_하지_않는다():
    memory = _memory(_obj("A", [_member("Game.Card", "cost")]))
    fake = _Fake()
    judge = PulseRelevanceJudge(fake)

    judge.notice(memory)

    assert fake.bodies == []
    assert judge.samples(memory) != []


def test_진행_중에_도착한_member_는_다음_round_에_판정된다():
    memory = _memory(_obj("A", [_member("Game.Card", "cost")]))

    async def go():
        gate = asyncio.Event()
        fake = _Fake(gate=gate)
        judge = PulseRelevanceJudge(fake)
        judge.notice(memory)
        await asyncio.sleep(0)
        _apply(memory, _obj("B", [_member("Game.Door", "open")], id=2), reading=2)
        judge.notice(memory)
        gate.set()
        await judge.drain()
        return fake

    fake = _run(go())

    assert len(fake.bodies) == 2
    assert set(memory.relevance) == {"Game.Card::cost", "Game.Door::open"}


def test_살아_있는_object_를_대표로_쓴다():
    memory = _memory(
        _obj("Dead", [_member("Game.Card", "cost")], id=1),
        _obj("Live", [_member("Game.Card", "cost")], id=2),
    )
    memory.held[next(k for k, h in memory.held.items() if h.selector == "Dead")].live = False

    samples = PulseRelevanceJudge(_Fake()).samples(memory)

    assert [s.object for s in samples] == ["Live"]


def test_channel_이_pulse_를_받으면_판정이_돌고_view_가_cosmetic_을_숨긴다():
    """channel → judge → `PulseMemory.relevance` → `render` 가 한 줄로 이어지는지 본다."""
    from app.qa.channel import QaRunChannel

    async def send(_frame: dict) -> None:
        return None

    async def scenario() -> str:
        channel = QaRunChannel(qa_try_id=1, send=send)

        async def decide(body: dict) -> dict:
            answers = {}
            for name, question in body["questions"].items():
                cosmetic = "SlimeAnimator" in question["instructions"]
                answers[name] = {"type": "noul", "noul": 0.1 if cosmetic else 0.95}
            return {"answers": answers, "usage": {"cost": 0.0}}

        judge = PulseRelevanceJudge(decide=decide)
        channel.pulse_relevance = judge
        enemy = _obj(
            "Enemy[0]",
            [_member("Game.Enemy", "Hp", 30), _member("Game.SlimeAnimator", "sprite", "s1")],
        )
        channel.on_pulse({"payload": {"schema": 2, "reading": 1, "frame": 1, "whole": True,
                                      "scene": "TurnBattleScene", "active": [enemy]}})
        await judge.drain()
        return channel.scene.pulse.render(since=0)

    view = _run(scenario())
    assert "Enemy.Hp = 30" in view
    assert "SlimeAnimator.sprite" not in view
    assert "hidden as cosmetic: 1 values on 1 objects" in view


def test_judge_가_없는_channel_은_아무것도_숨기지_않는다():
    from app.qa.channel import QaRunChannel

    async def send(_frame: dict) -> None:
        return None

    channel = QaRunChannel(qa_try_id=1, send=send)
    enemy = _obj("Enemy[0]", [_member("Game.SlimeAnimator", "sprite", "s1")])
    channel.on_pulse({"payload": {"schema": 2, "reading": 1, "frame": 1, "whole": True,
                                  "scene": "TurnBattleScene", "active": [enemy]}})

    assert channel.scene.pulse.relevance == {}
    assert "SlimeAnimator.sprite" in channel.scene.pulse.render(since=0)
