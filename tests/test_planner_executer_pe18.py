"""pe18: evaluator 가 개발자가 선언한 fact·rule·identity 를 런 시작부터 안다.

- 선언은 evaluator·실패 분석 system 글 끝에 붙는다.
- 판정 근거의 값 중 fact 의 source 에서 읽은 것에 `[fact <name>]` 이 붙는다.
"""

import asyncio

from app.agents.evaluator import Evaluator, FailureAnalyzer
from app.agents.evaluator.verdict import evidence_text
from app.agents.planner_executer.executer import StepPlay
from app.agents.planner_executer.facts import NO_FACTS, NO_MEANINGS, build_game_facts, game_meanings
from app.agents.planner_executer.identities import GameIdentity
from tests.test_planner_executer_steps import _config, _scenario

FACTS = build_game_facts(
    [
        {"name": "stages_unlocked_up_to", "source": {"scope": "statics", "member": "MapMove.StagePosition"},
         "type": "integer", "means": "Highest stage open on the map"},
        {"name": "player_hp", "source": {"scope": "objects", "member": "Player.Hp"}, "type": "integer", "means": "Word's HP"},
    ],
    [{"name": "first_clear_unlocks_next_stage", "says": "A first clear opens the next stage.", "facts": ["stages_unlocked_up_to"]}],
    origin="local",
)
IDENTITY = GameIdentity(
    "MapMove.StagePosition", "integer", 0, "The furthest stage unlocked.", "when a stage is first cleared", "stage", "progress"
)
PLAY = StepPlay(
    "done",
    turns=[{"step": 1, "turn": 0, "scene": "MapScene", "done": True}],
    evidence=[
        {"at": "start", "values": {"static MapMove.StagePosition": 1, "Word[13] Player.Hp": 120, "Canvas[1] text": "x"}},
        {"at": "end", "values": {"static MapMove.StagePosition": 1, "Word[13] Player.Hp": 120, "Canvas[1] text": "x"}},
    ],
)


def test_the_meanings_carry_the_facts_the_rules_and_the_identities() -> None:
    meanings = game_meanings(FACTS, (IDENTITY,))

    assert "- stages_unlocked_up_to = `static MapMove.StagePosition` (integer): Highest stage open on the map" in meanings.text
    assert "- player_hp = `Player.Hp` (integer): Word's HP" in meanings.text
    assert "first_clear_unlocks_next_stage [stages_unlocked_up_to]: A first clear opens the next stage." in meanings.text
    assert "- MapMove.StagePosition (progress stage; integer, counting from 0): The furthest stage unlocked." in meanings.text


def test_no_declaration_gives_no_meanings() -> None:
    assert game_meanings(NO_FACTS, ()) == NO_MEANINGS


def test_a_value_read_from_a_fact_s_source_carries_the_fact_s_name() -> None:
    text = evidence_text(PLAY, game_meanings(FACTS))

    assert "  static MapMove.StagePosition [fact stages_unlocked_up_to]: 1 (unchanged from start to end)" in text
    assert "  Word[13] Player.Hp [fact player_hp]: 120" in text
    assert "  Canvas[1] text: " in text and "Canvas[1] text [fact" not in text


def test_the_evaluator_knows_the_declarations_before_the_step() -> None:
    """로컬 try 37·38 step 14: 근거에 `static MapMove.StagePosition: 1` 만 있고 그것이 열린 스테이지라는 걸 evaluator 가 몰랐다."""
    seen: list[tuple[str, str]] = []

    async def judge(system: str, user: str):
        seen.append((system, user))
        return {"passed": True, "reason": "ok", "state_detail": "", "game_insight": ""}

    asyncio.run(Evaluator(_config(), judge, game_meanings(FACTS, (IDENTITY,))).judge(_scenario(), 1, PLAY))

    system, user = seen[0]
    assert "stages_unlocked_up_to = `static MapMove.StagePosition`" in system
    assert "MapMove.StagePosition (progress stage" in system
    assert "[fact stages_unlocked_up_to]" in user


def test_the_evaluator_without_declarations_reads_as_before() -> None:
    seen: list[tuple[str, str]] = []

    async def judge(system: str, user: str):
        seen.append((system, user))
        return {"passed": True, "reason": "ok", "state_detail": "", "game_insight": ""}

    asyncio.run(Evaluator(_config(), judge).judge(_scenario(), 1, PLAY))

    system, user = seen[0]
    assert "Game facts" not in system and "[fact " not in user


def test_the_failure_analysis_reads_the_same_declarations() -> None:
    seen: list[tuple[str, str]] = []

    async def judge(system: str, user: str):
        seen.append((system, user))
        return {"category": "GAME_BUG", "summary": "x", "evidence": [], "suspected_cause": "x"}

    asyncio.run(FailureAnalyzer(_config(), judge, game_meanings(FACTS)).analyze(_scenario(), 1, PLAY, "failed"))

    system, user = seen[0]
    assert "stages_unlocked_up_to = `static MapMove.StagePosition`" in system
    assert "[fact stages_unlocked_up_to]" in user
