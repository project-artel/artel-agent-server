"""cosmetic 으로 판정된 member 를 `pulse` view 에서 숨긴다 (ARTEL-958).

판정은 다른 곳에서 `PulseMemory.relevance` 에 쓴다. 여기서는 그 값을 읽는 쪽만 본다.
"""

import app.agents  # noqa: F401  (import order: avoids a circular import through app.qa)
from app.qa.pulse import (
    COSMETIC_BELOW,
    PULSE_VIEW_END,
    PulseMemory,
    PulseMember,
    PulseReading,
    changed_type_key,
)

SPRITE = "Combat.SlimeAnimator::spriteRenderer"
HEALTH = "Combat.Slime::health"


def reading(**over) -> dict:
    base = {
        "schema": 2,
        "reading": 1,
        "frame": 100,
        "scene": "Battle",
        "whole": True,
        "statics": [],
        "active": [],
        "deactive": [],
        "changed": [],
        "watching": 3,
        "unresolved": 0,
        "unwatchable": 0,
    }
    base.update(over)
    return base


def obj(selector="Slime[1]", members=None, **over) -> dict:
    base = {
        "scene": "Battle",
        "id": 1,
        "path": "Slime",
        "selector": selector,
        "members": members
        if members is not None
        else [
            {"on": "Combat.SlimeAnimator", "member": "spriteRenderer", "value": "frame3"},
            {"on": "Combat.Slime", "member": "health", "value": 7},
        ],
    }
    base.update(over)
    return base


def fold(*docs: dict, relevance: dict | None = None) -> PulseMemory:
    memory = PulseMemory()
    memory.relevance = dict(relevance or {})
    for doc in docs:
        memory.apply(PulseReading.model_validate(doc))
    return memory


def test_changed_type_key_ordinal_0_prefix_없음():
    assert changed_type_key("Battle/Slime[1]|Combat.SlimeAnimator::spriteRenderer") == SPRITE


def test_changed_type_key_among_접두사를_뗀다():
    assert changed_type_key("Battle/Slime[1]|2#Combat.SlimeAnimator::spriteRenderer") == SPRITE


def test_changed_type_key_객체_수준_키는_member_가_아니다():
    for key in ("Battle/Slime[1]|text", "|world", "|offers", "|active", "|tag"):
        assert changed_type_key(key) is None


def test_changed_type_key_static_은_그대로():
    assert changed_type_key("Core.InteractionLock::IsLocked") == "Core.InteractionLock::IsLocked"


def test_member_type_key_는_among_과_무관하다():
    a = PulseMember(on="A", member="b", among=0)
    b = PulseMember(on="A", member="b", among=3)
    assert a.type_key == b.type_key == "A::b"


def test_is_cosmetic_경계():
    memory = PulseMemory()
    assert COSMETIC_BELOW == 0.3
    assert not memory.is_cosmetic("A::b")  # 판정 없음
    assert not memory.is_cosmetic(None)
    memory.relevance = {"low": 0.29, "edge": 0.3, "high": 0.9}
    assert memory.is_cosmetic("low")
    assert not memory.is_cosmetic("edge")
    assert not memory.is_cosmetic("high")


def test_cosmetic_member_만_숨기고_같은_객체의_게임_상태는_남긴다():
    memory = fold(reading(active=[obj()]), relevance={SPRITE: 0.05, HEALTH: 0.95})
    view = memory.render()

    assert "SlimeAnimator.spriteRenderer" not in view
    assert "Slime.health = 7" in view
    assert "hidden as cosmetic: 1 values on 1 objects" in view
    assert view.index("hidden as cosmetic") < view.index(PULSE_VIEW_END)


def test_숨긴_개수는_객체와_member_를_따로_센다():
    memory = fold(
        reading(active=[obj(selector="A"), obj(selector="B", id=2), obj(selector="C", id=3)]),
        relevance={SPRITE: 0.0},
    )
    assert "hidden as cosmetic: 3 values on 3 objects" in memory.render()


def test_cosmetic_만_든_객체는_건너뛰고_shown_을_옮기지_않는다():
    memory = fold(
        reading(active=[obj(members=[{"on": "Combat.SlimeAnimator", "member": "spriteRenderer", "value": 1}])]),
        relevance={SPRITE: 0.0},
    )
    view = memory.render()
    assert "Slime[1]" not in view
    assert "hidden as cosmetic: 1 values on 1 objects" in view
    assert next(iter(memory.held.values())).shown == {}


def test_조작할_수_있는_객체는_cosmetic_만_들어도_그린다():
    memory = fold(
        reading(
            active=[
                obj(
                    members=[{"on": "Combat.SlimeAnimator", "member": "spriteRenderer", "value": 1}],
                    offers={"clicks": [{"event": "onClick", "method": "Enemy.Hit"}]},
                )
            ]
        ),
        relevance={SPRITE: 0.0},
    )
    view = memory.render()
    assert "Slime[1]" in view
    assert "can do" in view
    assert "SlimeAnimator.spriteRenderer" not in view


def test_판정이_뒤집히면_숨겼던_값이_다시_나온다():
    memory = fold(reading(active=[obj()]), relevance={SPRITE: 0.0})
    memory.render(advance=False)
    memory.relevance[SPRITE] = 0.9
    assert "SlimeAnimator.spriteRenderer" in memory.render(since=0)


def test_판독_로그는_cosmetic_을_이름_대지_않고_센다():
    memory = fold(
        reading(
            whole=False,
            changed=[
                "Battle/Slime[1]|Combat.SlimeAnimator::spriteRenderer",
                "Battle/Slime[2]|2#Combat.SlimeAnimator::spriteRenderer",
                "Battle/Slime[1]|Combat.Slime::health",
                "Battle/Slime[1]|text",
            ],
        ),
        relevance={SPRITE: 0.0},
    )
    entry = memory.log[-1]
    assert entry.moved == 4
    assert entry.cosmetic == 2
    assert entry.changed == ["Battle/Slime[1]|Combat.Slime::health", "Battle/Slime[1]|text"]
    assert memory._log_line(entry) == (
        "delta — Battle/Slime[1]|Combat.Slime::health, Battle/Slime[1]|text, +2 cosmetic"
    )
    # moves 는 종전대로 전부 센다.
    assert memory.moves["Battle/Slime[1]|Combat.SlimeAnimator::spriteRenderer"] == 1


def test_판독_로그_전부_cosmetic_이면_그렇게_말한다():
    memory = fold(
        reading(
            whole=False,
            changed=[
                "Battle/Slime[1]|Combat.SlimeAnimator::spriteRenderer",
                "Battle/Slime[2]|Combat.SlimeAnimator::spriteRenderer",
            ],
        ),
        relevance={SPRITE: 0.0},
    )
    assert memory._log_line(memory.log[-1]) == "delta — only cosmetic values moved (2)"


def test_접힘의_더_있다는_cosmetic_을_세지_않는다():
    keys = [f"Battle/S[{i}]|Combat.Slime::health" for i in range(10)]
    cosmetic = ["Battle/S[1]|Combat.SlimeAnimator::spriteRenderer"] * 3
    memory = fold(reading(whole=False, changed=keys + cosmetic), relevance={SPRITE: 0.0})
    line = memory._log_line(memory.log[-1])
    assert line.endswith(", +2 more, +3 cosmetic")


def _rich_docs() -> list[dict]:
    return [
        reading(
            statics=[{"declaring": "Core.InteractionLock", "member": "IsLocked", "value": False}],
            active=[
                obj(selector="A", text="Round 1"),
                obj(
                    selector="B",
                    id=2,
                    offers={"clicks": [{"event": "onClick", "method": "Enemy.Hit"}]},
                ),
            ],
        ),
        reading(
            reading=2,
            whole=False,
            changed=["Battle/A|Combat.Slime::health", "Battle/A|Combat.SlimeAnimator::spriteRenderer"],
            active=[
                obj(
                    selector="A",
                    members=[
                        {"on": "Combat.Slime", "member": "health", "value": 5},
                        {"on": "Combat.SlimeAnimator", "member": "spriteRenderer", "value": "f9"},
                    ],
                )
            ],
        ),
        reading(
            reading=3,
            whole=False,
            changed=["|offers", "Core.InteractionLock::IsLocked"],
            statics=[{"declaring": "Core.InteractionLock", "member": "IsLocked", "value": True}],
            deactive=[obj(selector="B", id=2, members=[])],
        ),
    ]


def test_relevance_가_비어_있으면_출력이_종전과_같다():
    # 숨기는 코드가 들어오기 전 (`9dc3f41`) 의 `pulse.py` 로 같은 입력을 돌려 얻은 결과다.
    memory = fold(*_rich_docs())
    views = [memory.render(since=0), memory.inspect("A"), memory.current_scene(), memory.since_action(1)]
    assert [memory._log_line(e) for e in memory.log] == GOLDEN_LOG
    assert views == GOLDEN_VIEWS


def test_relevance_가_비어_있어도_judged_표가_없다():
    memory = fold(*_rich_docs())
    assert "judged cosmetic" not in memory.inspect("A")
    assert "hidden as cosmetic" not in memory.render(since=0)


def test_inspect_는_cosmetic_도_보이고_표를_단다():
    memory = fold(reading(active=[obj()]), relevance={SPRITE: 0.0, HEALTH: 0.9})
    text = memory.inspect("Slime")
    assert "SlimeAnimator.spriteRenderer = 'frame3'  (judged cosmetic)" in text
    assert "Slime.health = 7\n" in text + "\n"


GOLDEN_LOG = ['whole — 0 values reported', 'delta — Battle/A|Combat.Slime::health, Battle/A|Combat.SlimeAnimator::spriteRenderer', 'delta — |offers, Core.InteractionLock::IsLocked']
GOLDEN_VIEWS = ["<<pulse>>\nreading 3 · frame 100 · scene Battle\n\nreadings since you last looked (last 10 kept):\n  1 (whole — 0 values reported)\n  2 (delta — Battle/A|Combat.Slime::health, Battle/A|Combat.SlimeAnimator::spriteRenderer)\n  3 (delta — |offers, Core.InteractionLock::IsLocked)\n\nswitched off: B\nhere but switched off: B\n\nthe screen reads:\n  'Round 1'   A  [id=1]  (changed)\n\nstatics:\n  InteractionLock.IsLocked = True  (changed)\nA  [id=1]:\n  Slime.health = 5\n  SlimeAnimator.spriteRenderer = 'f9'\n<<end pulse>>", "A  [id=1]:\n  says 'Round 1'\n  Slime.health = 5\n  SlimeAnimator.spriteRenderer = 'f9'", "<<pulse>>\nreading 3 · frame 100 · scene Battle\n\nreadings since you last looked (last 10 kept):\n  1 (whole — 0 values reported)\n  2 (delta — Battle/A|Combat.Slime::health, Battle/A|Combat.SlimeAnimator::spriteRenderer)\n  3 (delta — |offers, Core.InteractionLock::IsLocked)\n\nswitched off: B\nhere but switched off: B\n\nthe screen reads:\n  'Round 1'   A  [id=1]\n\nstatics:\n  InteractionLock.IsLocked = True\nA  [id=1]:\n  Slime.health = 5\n  SlimeAnimator.spriteRenderer = 'f9'\n<<end pulse>>", "<<pulse>>\nreading 3 · frame 100 · scene Battle\n\nreadings since you last looked (last 10 kept):\n  1 (whole — 0 values reported)\n  2 (delta — Battle/A|Combat.Slime::health, Battle/A|Combat.SlimeAnimator::spriteRenderer)\n  3 (delta — |offers, Core.InteractionLock::IsLocked)\n\nswitched off: B\nhere but switched off: B\n\nthe screen reads:\n  'Round 1'   A  [id=1]\n\nstatics:\n  InteractionLock.IsLocked = True\nA  [id=1]:\n  Slime.health = 5\n  SlimeAnimator.spriteRenderer = 'f9'\n<<end pulse>>"]
