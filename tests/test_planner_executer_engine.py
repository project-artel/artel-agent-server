"""Choosing the engine: `QaArchSpec.engine` picks the runner, and the default run does not move."""

import asyncio
import hashlib

import pytest
from pydantic import ValidationError

from app.agents.planner_executer.arch import PLANNER_EXECUTER_LABEL, PlannerExecuterArchSpec
from app.agents.planner_executer.prompts import MACRO_GRAMMAR
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.agents.qa.arch import QA_ARCH_LABEL, QaArchError, QaArchSpec
from app.agents.qa.runner import QaRunner
from app.llm.decision import resolve_decision_model
from app.prompts import load_prompt
from app.prompts.loader import available_versions, resolve_version
from app.prompts.lock import read_lock
from app.qa.run_config import resolve_run_config
from app.qa.schemas import QaRunScenario, QaScenario, QaStep
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore

PLANNER_EXECUTER = QaArchSpec(engine="planner_executer")


def test_the_default_engine_is_the_tool_loop() -> None:
    config = resolve_run_config()

    assert config.arch.engine == "agent"
    assert config.arch.planner is None
    assert config.agent_arch == QA_ARCH_LABEL


def test_the_engine_axis_leaves_the_tool_loop_fingerprint_alone() -> None:
    """Naming the default engine is the same structure as naming nothing."""
    assert (
        resolve_run_config(arch=QaArchSpec(engine="agent")).agent_fingerprint
        == resolve_run_config().agent_fingerprint
    )


def test_planner_knobs_are_refused_on_the_tool_loop() -> None:
    with pytest.raises((QaArchError, ValidationError)):
        QaArchSpec(planner=PlannerExecuterArchSpec())


def test_the_planner_executer_opens_a_free_session() -> None:
    # FREE 도 돈다(ARTEL-1149). 같은 엔진이라 같은 label 과 digest 로 기록된다.
    free = resolve_run_config(arch=PLANNER_EXECUTER, execution_mode="FREE")
    ordered = resolve_run_config(arch=PLANNER_EXECUTER)

    assert free.agent_arch == ordered.agent_arch
    assert free.agent_fingerprint == ordered.agent_fingerprint


def test_the_planner_executer_run_is_filed_under_its_own_label_and_digest() -> None:
    agent = resolve_run_config()
    config = resolve_run_config(arch=PLANNER_EXECUTER)

    assert config.arch.engine == "planner_executer"
    assert config.arch.planner == PlannerExecuterArchSpec()
    assert config.agent_arch == PLANNER_EXECUTER_LABEL == "pe3-evaluator-analysis"
    assert config.agent_fingerprint != agent.agent_fingerprint
    assert config.decision_model == resolve_decision_model()
    assert config.citation_reporting is False and config.compaction_model is None
    assert {"macro.click", "macro.press_key"} <= set(config.tools)


def test_a_relabelled_planner_executer_run_keeps_its_digest() -> None:
    relabelled = QaArchSpec(
        engine="planner_executer", planner=PlannerExecuterArchSpec(label="pe-trial")
    )

    config = resolve_run_config(arch=relabelled)

    assert config.agent_arch == "pe-trial"
    assert config.agent_fingerprint == resolve_run_config(arch=PLANNER_EXECUTER).agent_fingerprint


def test_the_planner_prompt_is_recorded_with_the_macro_grammar_it_reads() -> None:
    config = resolve_run_config(arch=PLANNER_EXECUTER)

    assert config.prompt_version == "v4"
    assert set(config.prompt_hashes) == {
        "rules", "evaluator", "scene_rules", "scene_evaluator", "failure_analysis", "run_review", "macro_grammar"
    }
    locked = read_lock()["prompts"]
    for role in ("rules", "evaluator", "scene_rules", "scene_evaluator", "failure_analysis", "run_review"):
        assert locked[f"qa_planner/v4/{role}"] == config.prompt_hashes[role]
    assert config.prompt_hashes["macro_grammar"] == hashlib.sha256(MACRO_GRAMMAR.encode()).hexdigest()


def test_the_planner_defaults_to_the_newest_prompt_and_v1_can_still_be_pinned() -> None:
    """`qa_planner` has no `*_PROMPT_VERSION` setting, so the newest version is the default. The
    tool loop's `qa_run` default is not moved by it."""
    assert available_versions("qa_planner") == ("v1", "v2", "v3", "v4")
    assert resolve_version("qa_planner") == "v4"
    assert resolve_run_config().prompt_version == resolve_version("qa_run")

    # v1 has no `failure_analysis`, so a pinned run has to turn the analysis off to be built.
    without_analysis = QaArchSpec(engine="planner_executer", planner=PlannerExecuterArchSpec(analysis="off"))
    pinned = resolve_run_config(arch=without_analysis, prompt_version="v1")

    assert pinned.prompt_version == "v1"
    assert read_lock()["prompts"]["qa_planner/v1/rules"] == pinned.prompt_hashes["rules"]


def test_v2_carries_the_evaluator_over_unchanged_and_adds_developer_macros_to_the_rules() -> None:
    assert load_prompt("qa_planner", "evaluator", "v2").body == load_prompt("qa_planner", "evaluator", "v1").body
    rules = load_prompt("qa_planner", "rules", "v2").body
    assert "and nothing else" not in rules
    assert "- Reuse first." in rules
    assert "Your defs cannot call developer macros." in rules


def test_v3_carries_rules_and_evaluator_over_unchanged_and_adds_the_scene_prompts() -> None:
    # ORDERED step 은 v2 와 같은 글을 읽는다. FREE 의 씬 계획·씬 판정만 새 글이다(ARTEL-1149).
    for role in ("rules", "evaluator"):
        assert load_prompt("qa_planner", role, "v3").body == load_prompt("qa_planner", role, "v2").body
    scene_rules = load_prompt("qa_planner", "scene_rules", "v3").body
    assert "```plan" in scene_rules and "def done_<case id>() -> bool" in scene_rules
    assert '"verdicts"' in load_prompt("qa_planner", "scene_evaluator", "v3").body


def test_v4_adds_only_the_failure_analysis_and_run_review_prompts() -> None:
    # 판정 prompt 는 그대로다(ARTEL-1267): 통과 수가 움직이면 분석이 아니라 다른 것이 움직인 것이다.
    for role in ("rules", "scene_rules", "evaluator", "scene_evaluator"):
        assert load_prompt("qa_planner", role, "v4").body == load_prompt("qa_planner", role, "v3").body
    analysis = load_prompt("qa_planner", "failure_analysis", "v4").body
    assert analysis.startswith("You analyse ONE failed test step of a game after it was played.")
    for category in ("GAME_DEFECT", "UNREACHED", "INCONCLUSIVE", "AGENT_ERROR"):
        assert f"`{category}`" in analysis
    # 재현 절차는 번호에 묶인 문장이고 화면 글자를 그대로 쓴다. 언어는 런마다 채운다.
    assert "exactly as the input line quotes it" in analysis and "{language_directive}" in analysis
    assert load_prompt("qa_planner", "failure_analysis", "v4").placeholders == ("language_directive",)
    review = load_prompt("qa_planner", "run_review", "v4").body
    assert review.startswith("You review ONE QA run of a game after every step was judged.")
    assert '"patterns"' in review and '"insights"' in review


def test_analysis_on_is_refused_on_a_prompt_version_without_its_prompts() -> None:
    # 조용히 끄면 `analysis="on"` 으로 기록된 run 이 분석 없이 돈다 — `vision='on'` 과 같은 wrong-bucket 이다.
    with pytest.raises(QaArchError, match="failure_analysis"):
        resolve_run_config(arch=PLANNER_EXECUTER, prompt_version="v3")


def test_the_service_builds_the_runner_the_engine_names() -> None:
    scenario = QaScenario(title="S1", description="d", steps=[QaStep(action="Open the menu.")])

    async def run() -> None:
        service = QaExecutionService(store=InMemoryQaSessionStore())
        scenarios = [QaRunScenario(qa_try_id=101, test_scenario_id=1, scenario=scenario)]
        _, agent = await service.open(qa_run_id=9, game_instance_id=1, scenarios=scenarios)
        _, planner = await service.open(
            qa_run_id=9, game_instance_id=1, scenarios=scenarios, arch=PLANNER_EXECUTER
        )

        assert isinstance(service._runner_factory(config=agent), QaRunner)
        assert isinstance(service._runner_factory(config=planner), PlannerExecuterRunner)

    asyncio.run(run())
