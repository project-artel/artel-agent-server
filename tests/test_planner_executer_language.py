"""ORDERED·FREE의 모델 요청과 최종 결과에 출력 언어를 전달한다."""

import asyncio
import hashlib

import pytest

from app.agents.planner_executer.evaluator import Evaluator, SceneEvaluator, stop_note, verdict_error
from app.agents.planner_executer.executer import StepPlay
from app.agents.planner_executer.macros import RegisteredMacros
from app.agents.planner_executer.planner import Planner
from app.agents.planner_executer.prompts import language_directive
from app.agents.planner_executer.runner import PlannerExecuterRunner, completion_message, planner_refused, unreached
from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer import runner as runner_module
from app.agents.qa.arch import QaArchSpec
from app.agents.scenario import OutputLanguage
from app.qa.run_config import resolve_run_config
from app.qa.jev import JevClient
from tests.test_planner_executer_free import MENU_CASE, Raw, _free, _plan, _resettable_menu
from tests.test_planner_executer_steps import MACROS, Asked, FakeJev, _judge, _menu, _scenario


@pytest.mark.parametrize("language", list(OutputLanguage))
def test_ORDERED와_FREE의_모든_모델_요청에_선택한_언어를_전달한다(language):
    config = resolve_run_config(language=language, arch=QaArchSpec(engine="planner_executer"))
    fake = _menu()
    ordered = Asked(MACROS)
    free = Raw(_plan([8]))
    systems = []

    async def judge(system, user):
        systems.append(system)
        return {"passed": True, "reason": "검증 완료", "verdicts": [{"case_id": 8, "passed": True, "reason": "검증 완료"}]}

    async def go():
        fake.push_reading()
        registry = RegisteredMacros(fake.channel)
        await Planner(config, fake.channel, config.arch.planner, registry, ask=ordered).write(_scenario(), 1)
        await Planner(config, fake.channel, config.arch.planner, registry, ask=free).plan_scene(_free(MENU_CASE), [1])
        play = StepPlay(start_screen="Menu", end_screen="Map", turns=[], stopped="done")
        await Evaluator(config, judge).judge(_scenario(), 1, play)
        await SceneEvaluator(config, judge).judge(_free(MENU_CASE), [(1, play)], "Menu")

    asyncio.run(go())
    systems.extend([ordered.calls[0][0], free.calls[0][0]])
    assert len(systems) == 4
    for system in systems:
        assert language_directive(language) in system
        assert "Preserve code identifiers, selectors, scene names, game text and comparison literals exactly." in system
    assert config.prompt_hashes["language"] == hashlib.sha256(language_directive(language).encode()).hexdigest()
    thoughts = [frame["payload"]["message"] for frame in fake.frames("LOG") if frame["payload"].get("category") == "THOUGHT"]
    assert len(thoughts) >= 2
    assert any(("작성하고 있습니다" if language == OutputLanguage.ko else "writing macros") in message for message in thoughts)
    assert any(("계획하고 있습니다" if language == OutputLanguage.ko else "planning") in message for message in thoughts)


def test_언어에_따라_prompt_hash가_달라진다():
    configs = [resolve_run_config(language=language, arch=QaArchSpec(engine="planner_executer")) for language in OutputLanguage]
    assert configs[0].prompt_hashes["language"] != configs[1].prompt_hashes["language"]
    assert configs[0].prompt_hashes["rules"] == configs[1].prompt_hashes["rules"]


@pytest.mark.parametrize("language", list(OutputLanguage))
def test_FREE_진행_설명과_최종_결과를_channel에_선택한_언어로_보낸다(language, monkeypatch):
    monkeypatch.setattr(runner_module, "RESET_READING_SECONDS", 0.01)
    config = resolve_run_config(language=language, arch=QaArchSpec(engine="planner_executer", planner=PlannerExecuterArchSpec(free_max_resets=1)))
    fake = _resettable_menu()
    answer = _plan([], {"8": "조건 미충족"})
    judge, _ = _judge()

    async def go():
        fake.push_reading()
        return await PlannerExecuterRunner(config, ask=Raw(answer, answer), judge=judge, jev=JevClient(post=FakeJev())).run_with_deadline(fake.channel, _free(MENU_CASE))

    state, error = asyncio.run(go())
    assert error is None and state.finished
    messages = {frame["payload"].get("kind"): frame["payload"]["message"] for frame in fake.frames("LOG")}
    for kind, ko, en in [("free_round", "남았습니다", "cases left"), ("reset", "다시 시작합니다", "Resetting"), ("free_end", "종료합니다", "ends after")]:
        assert (ko if language == OutputLanguage.ko else en) in messages[kind]
    status = fake.frames("STATUS")[-1]["payload"]
    assert ("통과했습니다" if language == OutputLanguage.ko else "steps passed") in status["message"]


@pytest.mark.parametrize("language, expected", [(OutputLanguage.ko, "통과했습니다"), (OutputLanguage.en, "steps passed")])
def test_고정된_검증_결과도_선택한_언어를_사용한다(language, expected):
    assert expected in completion_message(2, 3, language)
    assert "WAIT_TIMEOUT" in stop_note("WAIT_TIMEOUT", language)
    assert stop_note("done", language) == ""
    assert "EVALUATOR-ERROR" in verdict_error("JSON", language)
    assert "PLANNER-REFUSED" in planner_refused(2, "MACRO_NODE_REJECTED", language)
    result = unreached(2, 1, "GOAL_UNREACHED", "Map", language)
    assert "GOAL_UNREACHED" in result and "Map" in result
    assert ("도달하지 못했습니다" in result) == (language == OutputLanguage.ko)
