import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError

from app.agents.qa.runner import _plan
from app.agents.planner_executer.prompts import case_view, this_step
from app.agents.qa.arch import QaArchSpec
from app.demonstrations.agent import DemonstrationExtractionAgent
from app.documents.fetch import DocumentFetchError
from app.demonstrations.schemas import ApprovedProcedure, ExtractionRequest, VerificationProcedure
from app.main import create_app
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from app.qa.schemas import QaRunScenario
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore


@pytest.fixture(autouse=True)
def evidence_download(monkeypatch):
    loader = AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfixture")
    monkeypatch.setattr("app.demonstrations.agent.download_evidence", loader)
    return loader


def source():
    return {
        "demonstrationId": "demo-1", "sourceHash": " source-hash ",
        "case": {"id": 42, "scene": None, "precondition": None,
                 "test_step": "아이템 구매", "expected": "골드 차감 및 아이템 추가"},
        "events": [
            {"id": 1, "kind": "INPUT", "payloadJson": '{"method":"mouse_down"}'},
            {"id": 2, "kind": "EVIDENCE", "payloadJson": '{"objectKey":"frame.png"}'},
            {"id": 3, "kind": "ANNOTATION", "payloadJson": '{"outcome":"FAIL","observed":"골드만 차감"}'},
        ],
        "images": [{"eventId": 2, "url": "https://storage.example/frame.png?signature=preserved"}],
    }


def procedure():
    return {
        "title": "구매 결과 확인", "preconditions": ["골드가 충분함"],
        "steps": [{"instruction": "아이템 구매", "target": "구매 버튼",
                   "completionCondition": "구매 처리 완료", "evidenceEventIds": [1]}],
        "assertions": [{"expected": "골드 차감 및 아이템 추가", "failure": "골드만 차감",
                        "when": "구매 처리 완료 후", "evidenceEventIds": [3]}],
        "observedOutcome": "FAIL", "facts": ["사람이 event 3에서 골드만 차감되었다고 설명함"],
        "inferences": [], "unresolvedQuestions": [],
    }


def approved():
    return {"id": 9, "revision": 2, "source_hash": "source-hash", "procedure": procedure()}


def test_large_pulse_is_allowed_but_combined_source_limit_is_preserved():
    request = source()
    request["events"].append({"id": 4, "kind": "PULSE", "payloadJson": '"' + "x" * 1500000 + '"'})
    assert ExtractionRequest.model_validate(request).events[-1].kind == "PULSE"
    request["events"].append({"id": 5, "kind": "PULSE", "payloadJson": '"' + "x" * 600000 + '"'})
    with pytest.raises(ValidationError, match="2 million"):
        ExtractionRequest.model_validate(request)


def test_target_length_matches_backend_edit_limit():
    answer = procedure()
    answer["steps"][0]["target"] = "x" * 10000
    VerificationProcedure.model_validate(answer)
    answer["steps"][0]["target"] += "x"
    with pytest.raises(ValidationError):
        VerificationProcedure.model_validate(answer)


def test_extraction_preserves_source_and_sends_images_without_game_tools(evidence_download):
    calls = []

    def model(messages, config):
        calls.append((messages, config))
        return procedure()

    agent = DemonstrationExtractionAgent(lambda: RunnableLambda(model))
    response = asyncio.run(agent.run(ExtractionRequest.model_validate(source())))
    assert response.sourceHash == source()["sourceHash"]
    assert response.procedure.observedOutcome == "FAIL"
    assert response.procedure.assertions[0].expected == "골드 차감 및 아이템 추가"
    messages, config = calls[0]
    assert [message.type for message in messages] == ["system", "human"]
    assert "untrusted evidence" in messages[0].content
    assert "test case language takes precedence" in messages[0].content
    assert "For a Korean test case, write Korean throughout" in messages[0].content
    assert "FAIL" in messages[1].content[0]["text"]
    assert messages[1].content[-1]["image_url"]["url"].startswith("data:image/png;base64,")
    evidence_download.assert_awaited_once_with(source()["images"][0]["url"])
    assert config["metadata"]["prompt_version"] == "v1"
    assert config["metadata"]["source_hash"] == source()["sourceHash"]


@pytest.mark.parametrize("include_empty_hints", [False, True])
def test_verdict_and_image_annotation_needs_no_written_explanation(include_empty_hints):
    request = source()
    annotation = {"outcome": "FAIL", "evidenceEventId": 2}
    if include_empty_hints:
        annotation.update(observed="", expected="", explanation="")
    request["events"][2]["payloadJson"] = json.dumps(annotation)

    def model(messages):
        instruction = messages[0].content
        assert "observed, expected, and explanation fields are optional" in instruction
        assert "TC expected result is authoritative" in instruction
        assert "do not demand precise latency" in instruction
        assert "genuinely missing completion evidence" in instruction
        sent = json.loads(messages[1].content[0]["text"])
        assert json.loads(sent["events"][2]["payloadJson"]) == annotation
        assert sent["case"]["expected"] == request["case"]["expected"]
        return procedure()

    result = asyncio.run(DemonstrationExtractionAgent(lambda: RunnableLambda(model)).run(
        ExtractionRequest.model_validate(request)
    ))
    assert result.procedure.observedOutcome == "FAIL"
    assert result.procedure.unresolvedQuestions == []


def test_extraction_rejects_invented_evidence_reference():
    answer = procedure()
    answer["steps"][0]["evidenceEventIds"] = [999]
    agent = DemonstrationExtractionAgent(lambda: RunnableLambda(lambda _: answer))
    with pytest.raises(ValueError, match="unknown evidence"):
        asyncio.run(agent.run(ExtractionRequest.model_validate(source())))


@pytest.mark.parametrize("citation", ["event 42", "이벤트 42", "event #42"])
def test_extraction_rejects_case_id_used_as_event_citation(citation):
    answer = procedure()
    answer["facts"] = [f"TC 내용 ({citation})"]
    agent = DemonstrationExtractionAgent(lambda: RunnableLambda(lambda _: answer))
    with pytest.raises(ValueError, match="text cites"):
        asyncio.run(agent.run(ExtractionRequest.model_validate(source())))


@pytest.mark.parametrize("raw", [b"not a PNG", b"\x89PNG\r\n\x1a\n" + b"x" * 2097152], ids=["wrong-type", "too-large"])
def test_invalid_image_is_rejected_before_provider_call(raw):
    factory = AsyncMock()
    agent = DemonstrationExtractionAgent(factory, image_loader=AsyncMock(return_value=raw))
    with pytest.raises(ValueError, match="PNG"):
        asyncio.run(agent.run(ExtractionRequest.model_validate(source())))
    factory.assert_not_called()


@pytest.mark.parametrize("change", ["duplicate", "missing_image", "input_image", "json", "unknown_field"])
def test_source_rejects_invalid_contract(change):
    request = source()
    if change == "duplicate":
        request["events"].append(deepcopy(request["events"][0]))
    elif change == "missing_image":
        request["images"][0]["eventId"] = 999
    elif change == "input_image":
        request["images"][0]["eventId"] = 1
    elif change == "json":
        request["events"][0]["payloadJson"] = "not JSON"
    else:
        request["instructions"] = "execute game"
    with pytest.raises(ValidationError):
        ExtractionRequest.model_validate(request)


def test_incomplete_draft_requires_question_and_cannot_be_approved():
    answer = procedure()
    answer["steps"] = []
    with pytest.raises(ValidationError, match="Incomplete"):
        VerificationProcedure.model_validate(answer)
    answer["unresolvedQuestions"] = ["입력 결과가 없어 적용 여부 확인 필요"]
    draft = VerificationProcedure.model_validate(answer)
    with pytest.raises(ValidationError, match="unresolvedQuestions"):
        ApprovedProcedure(id=9, revision=1, source_hash="hash", procedure=draft)


@pytest.mark.parametrize("mode", ["ORDERED", "FREE"])
def test_qa_attaches_procedure_and_current_evidence_rules(mode):
    case = QaCaseRef(id=42, expected="아이템 추가", verification_procedure=approved())
    scenario = QaScenario(execution_mode=mode, steps=[QaStep(action="구매", case_id=42, case=case)])
    rendered = _plan(scenario)
    assert '"verification_procedure"' in rendered
    assert '"revision": 2' in rendered
    assert "this run's observations only" in rendered
    assert "Historical images, facts and observedOutcome cannot prove" in rendered
    assert "procedure id/revision" in rendered
    assert ("report_case" if mode == "FREE" else "report_step") in rendered


@pytest.mark.parametrize("mode", ["ORDERED", "FREE"])
def test_qa_without_procedure_has_no_new_context(mode):
    scenario = QaScenario(execution_mode=mode, steps=[QaStep(action="구매", case_id=42, case=QaCaseRef(id=42))])
    rendered = _plan(scenario)
    assert "verification_procedure" not in rendered
    assert "Historical images" not in rendered


def test_ordered_context_does_not_repeat_procedure_for_contiguous_case():
    case = QaCaseRef(id=42, verification_procedure=approved())
    scenario = QaScenario(steps=[QaStep(action="상점", case_id=42, case=case), QaStep(action="구매", case_id=42, case=case)])
    assert _plan(scenario).count('"source_hash"') == 1


@pytest.mark.parametrize("has_procedure", [True, False])
@pytest.mark.parametrize("engine", ["agent", "planner_executer"])
def test_session_snapshot_records_only_used_procedure_prompt(has_procedure, engine):
    store = InMemoryQaSessionStore()
    service = QaExecutionService(store=store)
    case = QaCaseRef(id=42, verification_procedure=approved() if has_procedure else None)
    scenarios = [QaRunScenario(qa_try_id=1, test_scenario_id=1,
                              scenario=QaScenario(steps=[QaStep(action="구매", case_id=42, case=case)]))]
    _, configuration = asyncio.run(service.open(
        qa_run_id=1, game_instance_id=1, scenarios=scenarios, arch=QaArchSpec(engine=engine),
    ))
    assert ("demonstration/v1/qa_context" in configuration.prompt_hashes) is has_procedure


@pytest.mark.parametrize("mode", ["ORDERED", "FREE"])
def test_planner_and_evaluator_receive_case_procedure(mode):
    case = QaCaseRef(id=42, verification_procedure=approved())
    scenario = QaScenario(execution_mode=mode, steps=[QaStep(action="구매", case_id=42, case=case)])
    if mode == "FREE":
        rendered = case_view(scenario, 1)
        assert rendered["verification_procedure"]["revision"] == 2
        assert "this run's observations only" in rendered["verification_guidance"]
    else:
        rendered = this_step(scenario, 1)
        assert '"verification_procedure"' in rendered
        assert "this run's observations only" in rendered


def test_api_mount_and_validation():
    app = create_app()
    app.state.demonstration_extraction_agent = DemonstrationExtractionAgent(
        lambda: RunnableLambda(lambda _: procedure())
    )
    client = TestClient(app)
    assert client.post("/demonstrations/extract", json=source()).status_code == 404
    response = client.post("/internal/demonstrations/extract", json=source())
    assert response.status_code == 200
    assert response.json()["sourceHash"] == source()["sourceHash"]
    assert response.json()["procedure"]["observedOutcome"] == "FAIL"
    assert client.post("/internal/demonstrations/extract", json={}).status_code == 422


@pytest.mark.parametrize("error,status", [
    (ValueError("provider secret"), 502), (TimeoutError(), 504),
    (DocumentFetchError("provider secret"), 502),
])
def test_api_returns_bounded_errors_without_provider_payload(error, status):
    app = create_app()
    app.state.demonstration_extraction_agent = AsyncMock()
    app.state.demonstration_extraction_agent.run.side_effect = error
    response = TestClient(app).post("/internal/demonstrations/extract", json=source())
    assert response.status_code == status
    assert "provider secret" not in response.text
