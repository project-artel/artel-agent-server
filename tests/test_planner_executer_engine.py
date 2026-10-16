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


def test_the_planner_executer_runs_ordered_scenarios_only() -> None:
    with pytest.raises(QaArchError, match="ORDERED scenarios only"):
        resolve_run_config(arch=PLANNER_EXECUTER, execution_mode="FREE")


def test_the_planner_executer_run_is_filed_under_its_own_label_and_digest() -> None:
    agent = resolve_run_config()
    config = resolve_run_config(arch=PLANNER_EXECUTER)

    assert config.arch.engine == "planner_executer"
    assert config.arch.planner == PlannerExecuterArchSpec()
    assert config.agent_arch == PLANNER_EXECUTER_LABEL == "pe1-skeleton"
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

    assert config.prompt_version == "v2"
    assert set(config.prompt_hashes) == {"rules", "evaluator", "macro_grammar"}
    locked = read_lock()["prompts"]
    assert locked["qa_planner/v2/rules"] == config.prompt_hashes["rules"]
    assert locked["qa_planner/v2/evaluator"] == config.prompt_hashes["evaluator"]
    assert config.prompt_hashes["macro_grammar"] == hashlib.sha256(MACRO_GRAMMAR.encode()).hexdigest()


def test_the_planner_defaults_to_the_newest_prompt_and_v1_can_still_be_pinned() -> None:
    """`qa_planner` has no `*_PROMPT_VERSION` setting, so the newest version is the default. The
    tool loop's `qa_run` default is not moved by it."""
    assert available_versions("qa_planner") == ("v1", "v2")
    assert resolve_version("qa_planner") == "v2"
    assert resolve_run_config().prompt_version == resolve_version("qa_run")

    pinned = resolve_run_config(arch=PLANNER_EXECUTER, prompt_version="v1")

    assert pinned.prompt_version == "v1"
    assert read_lock()["prompts"]["qa_planner/v1/rules"] == pinned.prompt_hashes["rules"]


def test_v2_carries_the_evaluator_over_unchanged_and_adds_developer_macros_to_the_rules() -> None:
    assert load_prompt("qa_planner", "evaluator", "v2").body == load_prompt("qa_planner", "evaluator", "v1").body
    rules = load_prompt("qa_planner", "rules", "v2").body
    assert "and nothing else" not in rules
    assert "- Reuse first." in rules
    assert "Your defs cannot call developer macros." in rules


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
