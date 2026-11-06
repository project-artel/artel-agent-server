"""개발자가 선언한 fact·rule: 선언을 읽고, planner 가 고른 fact 만 정확한 이름으로 읽어 Jev 에 싣는다.

불변식: 이름을 추측해 맞추지 않는다, 읽지 못한 값은 0·false 가 아니라 "unknown" 이다, 여러 holder 의 값은 고르지 않는다,
planner 요청에는 source 이름이 실리지 않는다, 범위 밖의 fact holder 는 그 fact member 만 보인다.
"""

import asyncio
import json

import pytest

from app.agents.planner_executer.developer_macros import DeveloperMacro, load_developer_macros
from app.agents.planner_executer.facts import NO_FACTS, load_game_facts, planner_text, read_facts_block
from app.agents.planner_executer.macros import RegisteredMacros
from app.agents.planner_executer.planner import Planner, split_facts_block
from app.agents.qa.macro.errors import MacroRejection
from app.qa.state_set import StateSnapshot, StateView, declared_values, render_jev_state
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import _config, _scenario

FACTS = [
    {"name": "cards_in_hand", "source": {"scope": "objects", "member": "Hand.cards.Count"}, "type": "integer",
     "unit": "cards", "means": "Cards the player holds."},
    {"name": "spells_in_flight", "source": {"scope": "statics", "member": "Spell.active"}, "type": "integer",
     "means": "Spells still flying."},
    {"name": "player_hp", "source": {"scope": "objects", "member": "Player.hp"}, "type": "integer", "means": "Player HP."},
]
RULES = [{"name": "cast_uses_cards", "says": "A spell uses two cards from the hand.", "facts": ["cards_in_hand"]}]


def _declare(tmp_path, facts=FACTS, rules=RULES):
    (tmp_path / "facts.json").write_text(json.dumps(facts))
    (tmp_path / "rules.json").write_text(json.dumps(rules))
    return load_game_facts(str(tmp_path))


def _snapshot(*, hand_status="KNOWN", hand_count=0, extra_hp_holder=False, static_value=0) -> StateSnapshot:
    hp = [{"entityId": "s:1", "scope": "scene:Battle#1", "path": "Word", "selector": "Word[0]", "active": True,
           "status": "KNOWN", "members": {"Game.Player::hp#0": {"status": "KNOWN", "value": 90}}}]
    if extra_hp_holder:
        hp.append({"entityId": "s:4", "scope": "scene:Battle#1", "path": "Twin", "selector": "Twin[0]", "active": True,
                   "status": "KNOWN", "members": {"Game.Player::hp#0": {"status": "KNOWN", "value": 90}}})
    hand = {"status": hand_status, "value": hand_count} if hand_status == "KNOWN" else {"status": hand_status, "reason": "not sampled"}
    return StateSnapshot.model_validate({
        "stateVersion": 1, "sync": "SYNCED", "scene": "Battle",
        "entities": [
            *hp,
            {"entityId": "s:2", "scope": "scene:Battle#1", "path": "Cards/Hand", "selector": "Cards[1]/Hand[0]",
             "active": True, "status": "KNOWN",
             "members": {"Game.Hand::cards.Count#0": hand, "Game.Hand::dragging#0": {"status": "KNOWN", "value": False}}},
            {"entityId": "s:3", "scope": "scene:Battle#1", "path": "Enemy", "selector": "Enemy[0]", "active": True,
             "status": "KNOWN", "members": {"Game.Enemy::hp#0": {"status": "KNOWN", "value": 5}}},
        ],
        "statics": [{"memberId": "Game.Spell::active", "observation": {"status": "KNOWN", "value": static_value}}],
    })


def test_declarations_are_read_and_the_ones_breaking_the_rules_are_left_out(tmp_path) -> None:
    bad = [*FACTS, {"name": "Bad Name", "source": {"scope": "objects", "member": "X.y"}, "type": "integer", "means": "x"},
           {"name": "player_hp", "source": {"scope": "objects", "member": "X.y"}, "type": "integer", "means": "twice"},
           {"name": "no_scope", "source": {"member": "X.y"}, "type": "integer", "means": "x"}]
    rules = [*RULES, {"name": "dangling", "says": "Uses mana.", "facts": ["mana"]}]
    declared = _declare(tmp_path, bad, rules)
    assert [fact.name for fact in declared.facts] == ["cards_in_hand", "player_hp", "spells_in_flight"]  # 이름순
    assert [rule.name for rule in declared.rules] == ["cast_uses_cards"]
    assert declared.revision and declared.revision == _declare(tmp_path, bad, rules).revision
    assert load_game_facts(None) == NO_FACTS and load_game_facts(str(tmp_path / "missing")) == NO_FACTS


def test_the_planner_reads_names_meanings_and_rules_but_no_source_names(tmp_path) -> None:
    text = planner_text(_declare(tmp_path))
    assert "- cards_in_hand (integer): Cards the player holds. (cards)" in text
    assert "- cast_uses_cards [cards_in_hand]: A spell uses two cards from the hand." in text
    assert "```facts" in text and '"unknown"' in text
    assert "Hand.cards" not in text and "Spell.active" not in text
    assert planner_text(NO_FACTS) == ""


def test_the_facts_block_takes_declared_names_only(tmp_path) -> None:
    declared = _declare(tmp_path)
    rest, text = split_facts_block("```guidance\nEnd the turn when facts.cards_in_hand is 0.\n```\n```facts\ncards_in_hand\n- player_hp\ncards_in_hand\n```\n")
    assert "facts" not in rest.replace("facts.cards_in_hand", "")
    assert read_facts_block(text, declared) == ("cards_in_hand", "player_hp")
    assert read_facts_block(None, declared) == ()
    with pytest.raises(MacroRejection, match="hand_size"):
        read_facts_block("hand_size\n", declared)


def test_values_are_read_by_the_exact_rendered_name_and_unknown_is_never_zero() -> None:
    sources = [("cards_in_hand", "objects", "Hand.cards.Count"), ("spells_in_flight", "statics", "Spell.active"),
               ("player_hp", "objects", "Player.hp"), ("guessed", "objects", "Hand.cards")]
    values, reasons = declared_values(_snapshot(hand_count=0, static_value=2), sources)
    assert values == {"cards_in_hand": 0, "spells_in_flight": 2, "player_hp": 90, "guessed": "unknown"}
    assert reasons == {"guessed": "not reported"}

    unread, why = declared_values(_snapshot(hand_status="UNREAD"), sources[:1])
    assert unread == {"cards_in_hand": "unknown"} and why["cards_in_hand"].startswith("not read")
    twins, why = declared_values(_snapshot(extra_hp_holder=True), [sources[2]])
    assert twins == {"player_hp": "unknown"} and why == {"player_hp": "2 holders"}


def test_a_chosen_fact_is_shown_outside_the_planned_scope_with_only_its_member() -> None:
    view = StateView(paths=("Enemy",), members=("Enemy.hp",), planned=True,
                     facts=(("objects", "Hand.cards.Count"), ("statics", "Spell.active")))
    state, _ = render_jev_state(_snapshot(hand_count=2, static_value=1), view=view)
    by_path = {entry["path"]: entry["members"] for entry in state["objects"]}
    assert by_path == {"Enemy[0]": {"Enemy.hp": 5}, "Cards[1]/Hand[0]": {"Hand.cards.Count": 2}}
    assert state["statics"] == {"Spell.active": 1}
    assert state["hidden"]["objects"] == {"Word": 1}

    without, _ = render_jev_state(_snapshot(), view=StateView(paths=("Enemy",), members=("Enemy.hp",), planned=True))
    assert [entry["path"] for entry in without["objects"]] == ["Enemy[0]"]
    assert without["hidden"]["objects"] == {"Cards": 1, "Word": 1}


STRIKE = '''def strike(enemy: object) -> None:
    """Strike one living enemy so it loses health."""
    click(enemy)


def ready_strike(enemy: object) -> bool:
    return member(enemy, "Game.Enemy::hp") > 0


def effect_strike(enemy: object) -> bool:
    return member(enemy, "Enemy.hit") == True
'''
CONTRACT = {"params": {"enemy": {"holds": "Game.Enemy::hp"}}, "effect": {"mode": "transition", "within": 0.5}}
ANSWER = (
    '```python\ndef done() -> bool:\n    return scene() == "Done"\n```\n'
    "```developer\nstrike\n```\n"
    "```guidance\nStrike the enemy. When facts.cards_in_hand is 0, stop.\n```\n"
    "```facts\ncards_in_hand\n```\n"
)


def _write(tmp_path, answer: str, **knobs):
    config = _config(actions="guidance", **knobs)
    declared = _declare(tmp_path)
    sent: list[str] = []

    async def ask(system: str, user: str) -> str:
        sent.append(user)
        return answer

    async def go():
        fake = FakeGame("Battle", {})
        fake.push_reading()
        developer = await load_developer_macros((DeveloperMacro("strike", STRIKE, "1.0", None, False, CONTRACT),))
        planner = Planner(config, fake.channel, config.arch.planner, RegisteredMacros(fake.channel), ask=ask,
                          developer=developer, facts=declared)
        return await planner.write(_scenario(), 1)

    return asyncio.run(go()), sent


def test_the_planner_chooses_facts_only_when_the_knob_is_on(tmp_path) -> None:
    written, sent = _write(tmp_path, ANSWER, facts="on")
    assert written.macros is not None and written.macros.facts == ("cards_in_hand",)
    assert "Game facts (declared by the game's developer)" in sent[0] and "Hand.cards" not in sent[0]

    off, sent_off = _write(tmp_path, ANSWER)
    assert off.macros is not None and off.macros.facts == ()
    assert "Game facts" not in sent_off[0]


def test_a_plan_naming_an_undeclared_fact_is_refused_and_rewritten(tmp_path) -> None:
    written, sent = _write(tmp_path, ANSWER.replace("```facts\ncards_in_hand", "```facts\nhand_size"), facts="on")
    assert written.macros is None and "hand_size" in written.refusal
    assert len(sent) > 1 and "hand_size" in sent[-1]


def test_the_executer_puts_the_chosen_facts_first_and_unknown_as_unknown(tmp_path) -> None:
    from app.agents.planner_executer.executer import Executer
    from app.agents.planner_executer.macros import StepObserve
    from app.qa.jev import JevClient

    declared = _declare(tmp_path)
    config = _config(state_source="state_set", observation="planner", facts="on")

    async def reader(_try_id, _spec):
        return _snapshot(hand_status="UNREAD")

    async def go():
        fake = FakeGame("Battle", {})
        executer = Executer(fake.channel, config.arch.planner, JevClient(post=None), state_reader=reader, facts=declared)
        executer._observe = StepObserve(paths=("Enemy",), members=("Enemy.hp",))
        executer._fact_sources = [("cards_in_hand", "objects", "Hand.cards.Count"), ("player_hp", "objects", "Player.hp")]
        return await executer._state(), executer._view_record(), executer._fact_reasons

    state, view, reasons = asyncio.run(go())
    assert list(state)[:2] == ["facts", "fact_definitions"]
    assert state["facts"] == {"cards_in_hand": "unknown", "player_hp": 90}
    assert state["fact_definitions"]["cards_in_hand"] == "Cards the player holds. (cards)"
    assert reasons == {"cards_in_hand": "not read (unread)"}
    assert view["facts"] == ["cards_in_hand", "player_hp"]


def test_declared_sources_are_watched_by_their_declared_path_once(tmp_path) -> None:
    from app.agents.planner_executer.facts import watch_facts, watch_name

    declared = _declare(tmp_path)
    assert [watch_name(fact) for fact in declared.facts] == ["Hand::cards.Count", "Player::hp", "Spell::active"]
    asked: list = []

    def answer(params):
        asked.append(params)
        return {"resolved": params[0][:2], "unresolvable": [{"name": "Player::hp", "reason": "no such member"}]}

    async def go():
        fake = FakeGame("Battle", {})
        fake.answers["watch"] = answer
        return await watch_facts(fake.channel, declared)

    missing = asyncio.run(go())
    assert asked == [[["Hand::cards.Count", "Player::hp", "Spell::active"]]]
    assert missing == [{"name": "Player::hp", "reason": "no such member"}]


def test_a_guidance_naming_a_fact_the_block_does_not_list_is_refused(tmp_path) -> None:
    from app.agents.planner_executer.facts import check_guidance_facts

    declared = _declare(tmp_path)
    check_guidance_facts("When facts.cards_in_hand is 0, stop.", ("cards_in_hand",), declared)
    check_guidance_facts("Strike the weakest enemy.", (), declared)
    with pytest.raises(MacroRejection, match="does not list"):
        check_guidance_facts("When facts.player_hp is low, defend.", ("cards_in_hand",), declared)
    with pytest.raises(MacroRejection, match="not a declared fact"):
        check_guidance_facts("When facts.mana is 0, wait.", (), declared)

    written, sent = _write(tmp_path, ANSWER.replace("```facts\ncards_in_hand\n```\n", ""), facts="on")
    assert written.macros is None and "does not list" in written.refusal


def test_declarations_are_read_from_orchestration_and_a_failure_reads_none() -> None:
    import httpx

    from app.agents.planner_executer.facts import fetch_game_facts

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/projects/3/game-facts"
        return httpx.Response(200, json={
            "facts": [
                {"name": "cards_in_hand", "scope": "objects", "member": "CardManager.myCards.Count", "type": "integer",
                 "unit": "cards", "means": "Cards in hand.", "version": 2},
                {"name": "Bad Name", "scope": "objects", "member": "X.y", "type": "integer", "unit": None, "means": "?", "version": 1},
            ],
            "rules": [
                {"name": "draws_two", "says": "Each turn draws two cards.", "facts": ["cards_in_hand"], "version": 1},
                {"name": "orphan", "says": "Names a fact that is not there.", "facts": ["word_hp"], "version": 1},
            ],
        })

    async def go(transport: httpx.MockTransport):
        async with httpx.AsyncClient(transport=transport) as client:
            return await fetch_game_facts(3, base_url="http://orch", client=client)

    declared = asyncio.run(go(httpx.MockTransport(handler)))
    assert declared.origin == "orchestration" and declared.revision
    # 규칙에 어긋난 fact 와 선언되지 않은 fact 를 가리키는 rule 은 로컬 선언처럼 빠진다.
    assert [fact.name for fact in declared.facts] == ["cards_in_hand"]
    assert declared.get("cards_in_hand").member == "CardManager.myCards.Count"
    assert [rule.name for rule in declared.rules] == ["draws_two"]
    assert asyncio.run(go(httpx.MockTransport(lambda request: httpx.Response(503)))) is NO_FACTS


def test_a_local_facts_directory_overrides_orchestration(tmp_path, monkeypatch) -> None:
    from app.agents.planner_executer import facts as facts_module
    from app.config import get_settings

    _declare(tmp_path)
    called: list = []

    async def fetch(project_id, *args, **kwargs):
        called.append(project_id)
        return NO_FACTS

    monkeypatch.setattr(facts_module, "fetch_game_facts", fetch)
    monkeypatch.setattr(get_settings(), "qa_facts_dir", str(tmp_path))
    local = asyncio.run(facts_module.resolve_game_facts(5))
    assert local.origin == "local" and local.facts and called == []

    monkeypatch.setattr(get_settings(), "qa_facts_dir", None)
    asyncio.run(facts_module.resolve_game_facts(5))
    assert called == [5]


def test_the_service_reads_declarations_once_per_run_only_when_facts_is_on(tmp_path, monkeypatch) -> None:
    from app.agents.qa.arch import QaArchSpec
    from app.agents.planner_executer.arch import PlannerExecuterArchSpec
    from tests.test_planner_executer_developer_macros import _serve

    declared = _declare(tmp_path)
    asked: list = []

    async def resolve(project_id):
        asked.append(project_id)
        return declared

    async def no_macros(project_id, game_build_id):
        return ()

    monkeypatch.setattr("app.qa.service.resolve_game_facts", resolve)
    seen, _ = _serve(monkeypatch, QaArchSpec(engine="planner_executer", planner=PlannerExecuterArchSpec(facts="on")), no_macros)
    assert asked == [5]
    assert [options["facts"] for options in seen] == [declared, declared]

    asked.clear()
    seen, _ = _serve(monkeypatch, QaArchSpec(engine="planner_executer"), no_macros)
    assert asked == [] and all("facts" not in options for options in seen)
