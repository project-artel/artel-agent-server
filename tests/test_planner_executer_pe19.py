"""pe19: 준비된 행동이 없으면 planner 가 한 번 다시 쓴다(`max_replans_per_step` 기본값 1).

로컬 try 43 step 4 의 모양을 그대로 둔다. 키 입력을 받는 것은 대화창(`ChatWindow`, `offers` 의 keys)이고, 안내 글자
(`AdvancePrompt`)는 입력을 받지 않는다. planner 가 준비 조건을 `actionable(find(name="AdvancePrompt"))` 로 쓰면 그 조건은 늘
거짓이다. 예전 기본값 0 에서는 유예 3초 뒤 그대로 판정으로 갔다.
"""

import asyncio

from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.qa.jev import JevClient
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import FakeJev, _config, _verdicts

KEYS = {"keys": [{"key": "key:any (down)"}]}
FIRST = '''```python
def press_on() -> None:
    """Press Space to move the dialogue on."""
    press_key("Space", 0.05)


def ready_press_on() -> bool:
    return actionable(find(name="AdvancePrompt"))


def done() -> bool:
    return scene() == "Map"
```
'''
SECOND = (
    '```changes\n{"base": 1, "replace": {"ready_press_on": "AdvancePrompt only shows the hint; ChatWindow takes the key"}}\n```\n'
    '```python\ndef ready_press_on() -> bool:\n    return actionable(find(name="ChatWindow"))\n```\n'
)
SCENARIO = QaScenario(
    title="Story",
    steps=[QaStep(action="Move the dialogue on.", case_id=5, case=QaCaseRef(id=5, expected="The map opens."))],
)


def _story() -> FakeGame:
    fake = FakeGame(
        "Story",
        {
            "Canvas[0]/ChatWindow[0]": {"path": "Canvas/ChatWindow", "offers": KEYS},
            "Canvas[0]/ChatWindow[0]/AdvancePrompt[0]": {"path": "Canvas/ChatWindow/AdvancePrompt", "text": "Click / any key · next"},
        },
    )

    def to_map(game: FakeGame) -> None:
        game.scene = "Map"
        game.objects = {"Root/Map": {"text": "Map"}}

    fake.reactions[("key_click", "Space")] = to_map
    return fake


def _run(**knobs):
    fake = _story()
    calls: list[str] = []

    async def ask(system: str, user: str) -> str:
        calls.append(user)
        return FIRST if len(calls) == 1 else SECOND

    async def judge(system: str, user: str):
        return {"passed": True, "reason": "ok", "state_detail": "", "game_insight": "", "checks": [], "precondition_held": None}

    config = _config(no_action_grace_seconds=0.2, analysis="off", coverage_check="off", **knobs)

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=ask, judge=judge, jev=JevClient(post=FakeJev(picks=("press_on",))))
        return await runner.run_with_deadline(fake.channel, SCENARIO)

    state, failure = asyncio.run(go())
    return fake, calls, state, failure


def test_a_ready_condition_on_an_object_that_takes_no_input_is_written_again_and_the_step_goes_on() -> None:
    fake, calls, state, failure = _run()

    assert failure is None and state.finished
    assert len(calls) == 2, "준비된 행동이 없어 planner 가 한 번 더 썼다"
    assert "REPLAN: no action was ready" in calls[1] and "(press_on)" in calls[1]
    assert "def ready_press_on" in calls[1] and "can do — keys" in calls[1], "지금 계획과 입력을 받는 객체가 함께 보인다"
    assert ("key_click", ["Space", 0.05]) in fake.actions
    (verdict,) = _verdicts(fake)
    assert verdict["played"]["stopped"] == "done"


def test_with_no_replans_the_step_stops_where_nothing_was_ready() -> None:
    """예전 기본값. try 42 step 9·try 43 step 4 가 이렇게 끝났다."""
    fake, calls, state, failure = _run(max_replans_per_step=0)

    assert len(calls) == 1
    assert ("key_click", ["Space", 0.05]) not in fake.actions
    (verdict,) = _verdicts(fake)
    assert verdict["played"]["stopped"].startswith("no action was ready")
