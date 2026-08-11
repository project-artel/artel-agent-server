"""QA_Run(TR) 단위 실행: 런의 시나리오들을 순서대로, 사이에 초기화하며 돌린다(ARTEL-258)."""

import asyncio

from app.api.qa_sessions import QaContext
from app.qa.schemas import QaRunScenario, QaScenario
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore


class RecordingRunner:
    def __init__(self, seen: list[tuple[int, str]]) -> None:
        self._seen = seen

    async def run_with_deadline(self, channel, scenario):
        # 각 시나리오가 자기 qa_try_id 채널로, 순서대로 실행되는지 기록.
        self._seen.append((channel.qa_try_id, scenario.title))
        return None, None  # 깨끗한 종료(실패/취소 아님)


class RecordingResetPolicy:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []

    async def between_scenarios(self, channel, completed_index, total) -> None:
        self.calls.append((channel.qa_try_id, completed_index, total))


def _body(title: str) -> QaScenario:
    return QaScenario(title=title, description="d")


def test_run_executes_scenarios_in_order_and_resets_between() -> None:
    async def run() -> None:
        seen: list[tuple[int, str]] = []
        reset = RecordingResetPolicy()
        service = QaExecutionService(
            store=InMemoryQaSessionStore(),
            runner_factory=lambda *, config: RecordingRunner(seen),
            reset_policy=reset,
        )
        scenarios = [
            QaRunScenario(qa_try_id=101, test_scenario_id=1, scenario=_body("S1")),
            QaRunScenario(qa_try_id=102, test_scenario_id=2, scenario=_body("S2")),
            QaRunScenario(qa_try_id=103, test_scenario_id=3, scenario=_body("S3")),
        ]
        session_id, _ = await service.open(
            qa_run_id=9, game_instance_id=1, scenarios=scenarios
        )

        async def send(_frame: dict) -> None:
            return None

        await service.run(session_id, send)

        # 모든 시나리오가 순서대로, 각자 자기 qa_try_id 채널로 실행됐다.
        assert seen == [(101, "S1"), (102, "S2"), (103, "S3")]
        # 리셋은 시나리오 사이에만(첫 시나리오 전엔 X) — 다가올 시나리오의 채널(qa_try)로.
        assert reset.calls == [(102, 1, 3), (103, 2, 3)]

    asyncio.run(run())


def test_ensure_returns_first_scenario_try() -> None:
    async def run() -> None:
        service = QaExecutionService(store=InMemoryQaSessionStore())
        session_id, _ = await service.open(
            qa_run_id=9,
            game_instance_id=1,
            scenarios=[
                QaRunScenario(qa_try_id=101, test_scenario_id=1, scenario=_body("S1")),
                QaRunScenario(qa_try_id=102, test_scenario_id=2, scenario=_body("S2")),
            ],
        )
        # 연결 레벨 프레임은 첫 시나리오의 try로 stamp된다.
        assert await service.ensure(session_id) == 101

    asyncio.run(run())


def test_legacy_single_context_normalizes_to_one_scenario_run() -> None:
    # Orche가 run-scoped로 바뀌기 전 구 단일 시나리오 요청도 1-시나리오 런으로 받는다.
    ctx = QaContext.model_validate(
        {
            "qa_try_id": 7,
            "game_instance_id": 1,
            "test_scenario_id": 3,
            "scenario": {"title": "t", "description": "d"},
        }
    )
    assert ctx.qa_run_id == 7  # 전용 run이 없으면 try id로 대신
    assert len(ctx.scenarios) == 1
    assert ctx.scenarios[0].qa_try_id == 7
    assert ctx.scenarios[0].test_scenario_id == 3
