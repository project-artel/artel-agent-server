"""QA 시작에 고정한 선언은 최신 조회나 시나리오 전환으로 바뀌지 않는다."""

import asyncio

import httpx
import pytest

from app.api.qa_sessions import QaContext
from app.agents.planner_executer.identities import GameIdentitySnapshot, fetch_game_identities
from app.agents.qa.arch import QaArchSpec
from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.qa.schemas import QaRunScenario, QaScenario
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore


DECLARATION = {
    "name": "StageData.stage", "kind": "ordinal", "base": 0, "meaning": "현재 stage",
    "changesWhen": "stage 진입 시", "identifies": "stage", "role": "current",
    "revisionId": "9007199254740993", "gameBuildId": "42", "version": 3, "source": "MCP",
    "createdBy": "17", "createdAt": "2026-11-03T00:00:00Z", "deleted": False,
}


@pytest.mark.parametrize("declarations", [[], [DECLARATION]])
def test_snapshot_survives_store_and_all_scenarios_without_latest_fetch(monkeypatch, declarations):
    async def run():
        async def forbidden_fetch(*args, **kwargs):
            raise AssertionError("확정한 snapshot이 있으면 최신 선언을 조회하지 않는다")

        async def no_context(**kwargs):
            return None

        monkeypatch.setattr("app.qa.service.fetch_game_identities", forbidden_fetch)
        monkeypatch.setattr("app.qa.service.fetch_scene_context", no_context)
        seen = []

        class Runner:
            async def run_with_deadline(self, channel, scenario):
                return None, None

        class Reset:
            async def between_scenarios(self, *args):
                pass

        def runner(*, config, identities):
            seen.append(identities)
            return Runner()

        store = InMemoryQaSessionStore()
        service = QaExecutionService(store=store, runner_factory=runner, reset_policy=Reset())
        arch = QaArchSpec(engine="planner_executer", planner=PlannerExecuterArchSpec(
            target_contract="on", developer_macros="off"
        ))
        session_id, _ = await service.open(
            qa_run_id=9, game_instance_id=1, project_id=2, game_build_id=42, arch=arch,
            scenarios=[QaRunScenario(qa_try_id=i, test_scenario_id=i, scenario=QaScenario(title="t"))
                       for i in [10, 11]],
            game_identities=[GameIdentitySnapshot.model_validate(item) for item in declarations],
        )
        record = await store.load(session_id)
        assert record is not None and record.game_identities is not None
        if declarations:
            item = record.game_identities[0]
            assert (item.revision_id, item.version, item.source, item.created_by) == (
                "9007199254740993", 3, "MCP", "17"
            )

        async def send(frame):
            pass

        await service.run(session_id, send)
        assert len(seen) == 2 and seen[0] == seen[1]
        assert len(seen[0]) == len(declarations)
        if declarations:
            assert seen[0][0].base == 0

    asyncio.run(run())


def test_context_distinguishes_missing_and_empty_snapshot():
    body = {"qa_try_id": 7, "game_instance_id": 1, "test_scenario_id": 3, "scenario": {"title": "t"}}
    assert QaContext.model_validate(body).game_identities is None
    assert QaContext.model_validate({**body, "game_identities": []}).game_identities == []


def test_api_passes_snapshot_into_the_persisted_session():
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.test_qa_prompt_version import open_request

    service = QaExecutionService(store=InMemoryQaSessionStore())
    app.state.qa_session_service = service
    try:
        body = open_request()
        body["context"]["game_identities"] = [DECLARATION]
        response = TestClient(app).post("/internal/qa-sessions", json=body)
        assert response.status_code == 200, response.text
        record = asyncio.run(service._load(response.json()["session_id"]))
        assert record.game_identities[0].revision_id == DECLARATION["revisionId"]
    finally:
        del app.state.qa_session_service


def test_missing_build_never_falls_back_to_project_latest():
    async def run():
        def forbidden(request):
            raise AssertionError("build가 없으면 최신 선언을 조회하지 않는다")
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            assert await fetch_game_identities(2, base_url="http://orch", client=client) == ()
    asyncio.run(run())
