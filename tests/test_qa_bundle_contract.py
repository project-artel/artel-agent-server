"""통합한 TC 보고·사람 피드백·시연·진단 도구가 함께 전달되는 계약."""

import json

import pytest

from app.agents.qa.runner import _plan
from app.agents.qa.arch import QaArchSpec, resolve_arch, structure_of
from app.prompts import load_prompt, resolve_version
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from test_demonstrations import approved


@pytest.mark.parametrize("mode", ["ORDERED", "FREE"])
def test_tc_plan_keeps_demonstration_and_human_feedback_together(mode):
    case = QaCaseRef(id=42, expected="아이템 추가", verification_procedure=approved(),
        reviews=[{"ai_passed": False, "human_passed": True, "note": "지급 지연을 기다려야 함"}])
    scenario = QaScenario(execution_mode=mode, steps=[QaStep(action="구매", case_id=42, case=case)])
    text = _plan(scenario)
    payload = json.loads(text[text.index('{'):text.rindex('}') + 1])
    row = payload["test_cases"][0]
    assert row["verification_procedure"]["revision"] == 2
    assert row["human_review"][0]["note"] == "지급 지연을 기다려야 함"
    assert "this run's observations only" in text
    assert "start_test_case" in text and "report_test_case" in text
    assert "report_step(" not in text


def test_combined_prompt_keeps_tc_verdict_and_diagnostics_instructions():
    assert resolve_version("qa_run") == "v30"
    system = load_prompt("qa_run", "system").body
    assert "Steps are optional guidance" in system
    for name in ("start_test_case", "report_test_case", "ask_user", "get_errors", "get_performance"):
        assert name in system
    assert "expected and observed" in system
    assert "report_step" not in system
    vision = load_prompt("qa_run", "vision_directive").body
    assert "first time you enter each scene" in vision
    assert "empty areas" in vision and "developer console" in vision
    macro = load_prompt("qa_run", "skill_macro").body
    assert "[MISSING TEXT: STORY_003]" in macro


def test_combined_arch_has_tc_reports_and_both_diagnostic_tools():
    names, _, _ = structure_of(resolve_arch(QaArchSpec(), "openai/gpt-6-luna"))
    assert {"start_test_case", "report_test_case", "ask_user", "get_errors", "get_performance"} <= set(names)
    assert not {"report_step", "report_case"} & set(names)
