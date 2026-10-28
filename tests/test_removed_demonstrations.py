"""삭제된 시연 계약이 이전 snapshot으로 다시 노출되지 않는지 확인한다."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.agents.planner_executer.prompts import candidate_list, case_view, this_step
from app.agents.qa.arch import QaArchSpec
from app.agents.qa.runner import _plan
from app.main import create_app
from app.prompts import known_agents
from app.qa.schemas import QaCaseRef, QaRunScenario, QaScenario, QaStep
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore


def legacy_scenario(mode):
    return QaScenario.model_validate({
        "execution_mode": mode,
        "steps": [{"action": "구매", "case_id": 42, "case": {
            "id": 42, "expected": "아이템 추가",
            "verification_procedure": {"id": 9, "procedure": {"steps": ["STALE_DEMONSTRATION_ONLY"]}},
            "reviews": [{"ai_passed": False, "human_passed": True, "note": "지급 지연 확인"}],
        }}],
    })


@pytest.mark.parametrize("mode", ["ORDERED", "FREE"])
def test_이전_snapshot의_시연_절차는_역직렬화와_두_engine_context에서_제외(mode):
    scenario = legacy_scenario(mode)
    assert "verification_procedure" not in scenario.model_dump_json()
    assert "verification_procedure" not in QaCaseRef.model_fields
    contexts = [_plan(scenario), this_step(scenario, 1), json.dumps(case_view(scenario, 1)), candidate_list(scenario, [1])]
    for context in contexts:
        assert "verification_procedure" not in context
        assert "verification_guidance" not in context
        assert "STALE_DEMONSTRATION_ONLY" not in context
    assert "지급 지연 확인" in _plan(scenario)
    assert "start_test_case" in _plan(scenario)
    assert "report_test_case" in _plan(scenario)


@pytest.mark.parametrize("engine", ["agent", "planner_executer"])
def test_이전_snapshot을_열어도_삭제된_시연_prompt_hash를_기록하지_않음(engine):
    service = QaExecutionService(store=InMemoryQaSessionStore())
    _, configuration = asyncio.run(service.open(
        qa_run_id=1, game_instance_id=1,
        scenarios=[QaRunScenario(qa_try_id=1, test_scenario_id=1, scenario=legacy_scenario("ORDERED"))],
        arch=QaArchSpec(engine=engine),
    ))
    assert not any(key.startswith("demonstration/") for key in configuration.prompt_hashes)


def test_시연_추출_API와_prompt를_제거():
    app = create_app()
    with TestClient(app) as client:
        assert client.post("/internal/demonstrations/extract", json={}).status_code == 404
        assert client.post("/demonstrations/extract", json={}).status_code == 404
        schema = client.get("/openapi.json").json()
        assert not any("demonstrations" in path for path in schema["paths"])
    assert "demonstration" not in known_agents()


class FakeRedis:
    def __init__(self, entries, conflicts=0):
        self.entries = entries
        self.conflicts = conflicts
        self.ttl = {key: 77 for key in entries}
        self.writes = 0

    async def scan_iter(self, match, count):
        assert match == "qa:*"
        for key in list(self.entries):
            if key.startswith("qa:"):
                yield key

    async def get(self, key):
        return self.entries.get(key)

    async def eval(self, script, key_count, key, expected, replacement):
        assert "KEEPTTL" in script and key_count == 1
        if self.conflicts:
            self.conflicts -= 1
            value = json.loads(self.entries[key])
            value["chat"] = [{"message": "concurrent answer"}]
            self.entries[key] = json.dumps(value)
            return 0
        if self.entries.get(key) != expected:
            return 0
        self.entries[key] = replacement
        self.writes += 1
        return 1


def test_Redis_정리는_시연_필드만_삭제하고_QA_세션과_TTL을_보존():
    from app.qa.retired_context import purge_demonstration_context

    scenario = {"scenario": {"steps": [{"case": {"id": 42, "verification_procedure": {"steps": ["old"]}}}]}}
    raw = json.dumps({"scenarios": [scenario], "run_config": {"prompt_hashes": {
        "demonstration/v1/qa_context": "old", "qa_run/v30/system": "keep"}}, "qa_run_id": 3, "chat": []})
    redis = FakeRedis({"qa:old": raw, "qa:clean": '{"qa_run_id":4}', "session:other": raw}, conflicts=1)
    asyncio.run(purge_demonstration_context(redis))
    cleaned = json.loads(redis.entries["qa:old"])
    assert cleaned["scenarios"][0]["scenario"]["steps"][0]["case"] == {"id": 42}
    assert cleaned["qa_run_id"] == 3
    assert cleaned["chat"] == [{"message": "concurrent answer"}]
    assert cleaned["run_config"]["prompt_hashes"] == {"qa_run/v30/system": "keep"}
    assert redis.ttl == {"qa:old": 77, "qa:clean": 77, "session:other": 77}
    assert redis.entries["session:other"] == raw
    assert redis.entries["qa:clean"] == '{"qa_run_id":4}'
    asyncio.run(purge_demonstration_context(redis))
    assert redis.writes == 1
    asyncio.run(purge_demonstration_context(FakeRedis({})))


def test_Redis_정리_동시_충돌을_조용히_무시하지_않고_재시도_가능하게_실패():
    from app.qa.retired_context import purge_demonstration_context

    raw = json.dumps({"scenarios": [{"scenario": {"steps": [{"case": {"verification_procedure": {}}}]}}]})
    redis = FakeRedis({"qa:old": raw}, conflicts=3)
    with pytest.raises(RuntimeError, match="concurrent updates"):
        asyncio.run(purge_demonstration_context(redis))
    asyncio.run(purge_demonstration_context(redis))
    assert "verification_procedure" not in redis.entries["qa:old"]
