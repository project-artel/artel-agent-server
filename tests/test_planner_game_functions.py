import asyncio

import pytest

from app.agents.evaluator.verdict import turn_line
from app.agents.planner_executer.executer import recent_action_lines
from app.agents.planner_executer.game_functions import PlannerGameFunctions
from app.agents.planner_executer.macro_host import ChannelMacroHost
from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.macro.runner import run_macro
from app.agents.qa.tools.tool_context import ToolContext
from app.qa.game_functions import CaseFunctions, GameFunctionEntry
from tests.test_qa_game_functions import OTHER_PLAYER, PLAYER, actions, answer, make, pulse


SOURCE = """def setup() -> None:
    call_game_function("give_gold", '{"amount":4}')
"""


def test_setup_action_uses_the_registered_sdk_function_and_records_the_result():
    async def play():
        channel, state, _, sent, arch = make()
        functions = PlannerGameFunctions(ToolContext(channel, state, arch))
        host = ChannelMacroHost(channel, None, 1, game_functions=functions)
        macro = macro_definition_from_source("setup", SOURCE, game_functions=True)
        reply = answer(channel, sent, {"id": 1, "success": True, "returnValue": {"message": "simulated-secret-credential"}})
        result = await run_macro(host, macro, {}, 1)
        await reply
        assert result.failure is None
        assert actions(sent)[0]["payload"]["actions"][0]["params"] == [{"name": "give_gold", "args": {"amount": 4}, "target": None}]
        assert state.used_cheats[0].success
        assert host.sent[0]["value"] is None
        assert host.sent[0]["kind"] == "setup_function"
        assert "simulated-secret-credential" not in str(result.outcomes) + str(host.sent)
        assert "give_gold" in functions.guidance(1) and "give_gold" not in functions.guidance(2)
    asyncio.run(play())


def test_unprivileged_macros_and_probes_cannot_invoke_game_functions():
    with pytest.raises(MacroRejection):
        macro_definition_from_source("setup", SOURCE)
    for call in ('call_game_function("give_gold")', 'setup()'):
        predicate = f"def done() -> bool:\n    {call}\n    return True\n"
        with pytest.raises(MacroRejection):
            macro_definition_from_source("done", predicate + SOURCE, game_functions=True)

    async def probe():
        channel, _, _, sent, _ = make()
        host = ChannelMacroHost(channel, None, 1)
        macro = macro_definition_from_source("probe", "def probe() -> None:\n    setup()\n" + SOURCE, game_functions=True)
        result = await run_macro(host, macro, {}, 1)
        assert result.failure is not None and not actions(sent)
    asyncio.run(probe())


@pytest.mark.parametrize("step,name,args,target", [
    (2, "give_gold", '{"amount":4}', None), (1, "reset_game", "{}", None),
    (1, "unregistered", "{}", None), (1, "give_gold", '{"amount":true}', None),
    (1, "give_gold", '{"amount":-5}', None), (1, "give_gold", "{}", None),
    (1, "give_gold", '{"amount":4}', "Player"), (1, "give_gold", "[]", None),
    (1, "give_gold", '{"amount":4,"extra":1}', None), (0, "give_gold", '{"amount":4}', None),
])
def test_forbidden_or_invalid_calls_never_reach_the_sdk(step, name, args, target):
    channel, state, _, sent, arch = make()
    functions = PlannerGameFunctions(ToolContext(channel, state, arch))
    reply = asyncio.run(functions.call(step, name, args, target))
    assert not reply.sent and not reply.ok and not actions(sent)


def test_read_only_query_during_verification_respects_case_permission_and_sdk_failure():
    async def play():
        channel, state, _, sent, arch = make(step_functions=[CaseFunctions(cheats="none"), CaseFunctions(cheats=["query"])])
        channel.game_functions.cheats.append(GameFunctionEntry(name="query", readOnly=True))
        functions = PlannerGameFunctions(ToolContext(channel, state, arch))
        assert not functions.available(1)
        assert [entry.name for entry in functions.available(2)] == ["query"]
        reply = answer(channel, sent, {"id": 1, "success": False, "error": "player missing"})
        result = await functions.call(2, "query", "{}", None)
        await reply
        assert result.sent and not result.ok and "player missing" in result.detail
        assert actions(sent)[0]["payload"]["actions"][0]["params"][0]["requireReadOnly"] is True
        assert not state.used_cheats
    asyncio.run(play())


def test_verification_can_use_a_setup_function_named_in_its_case_permissions():
    channel, state, _, _, arch = make(step_functions=[None, CaseFunctions(cheats=["give_gold"])])
    functions = PlannerGameFunctions(ToolContext(channel, state, arch))
    assert [entry.name for entry in functions.available(2)] == ["give_gold"]


def test_target_availability_is_checked_for_the_selected_instance():
    channel, state, _, sent, arch = make()
    pulse(channel, whole=True, functions={"whole": True, "entries": [
        {"kind": "cheat", "name": "heal", "target": PLAYER, "available": True},
        {"kind": "cheat", "name": "heal", "target": OTHER_PLAYER, "available": False},
    ]})
    functions = PlannerGameFunctions(ToolContext(channel, state, arch))
    for target in (None, OTHER_PLAYER):
        assert not asyncio.run(functions.call(1, "heal", "{}", target)).sent
    assert not actions(sent)


def test_arch_disabled_and_reserved_reset_are_excluded():
    for cheats in ("off", "on"):
        channel, state, _, _, arch = make(cheats=cheats)
        channel.game_functions.cheats.append(GameFunctionEntry(name="reset_game"))
        available = PlannerGameFunctions(ToolContext(channel, state, arch)).available(1)
        assert all(entry.name != "reset_game" for entry in available)
        if cheats == "off":
            assert not available


def test_failed_setup_redacts_sdk_text_and_read_only_success_keeps_observation():
    async def play():
        channel, state, _, sent, arch = make(step_functions=[None, CaseFunctions(cheats=["query"])])
        channel.game_functions.cheats.append(GameFunctionEntry(name="query", readOnly=True))
        functions = PlannerGameFunctions(ToolContext(channel, state, arch))
        setup = ChannelMacroHost(channel, None, 1, game_functions=functions)
        reply = answer(channel, sent, {"id": 1, "success": False, "error": "simulated-secret-credential"})
        result = await setup.call_game_function("give_gold", '{"amount":4}', "")
        await reply
        assert result[:2] == (True, False)
        assert "simulated-secret-credential" not in str(result) + str(setup.sent)
        query = ChannelMacroHost(channel, None, 2, game_functions=functions)
        reply = answer(channel, sent, {"id": 1, "success": True, "returnValue": {"message": "hp=4"}})
        assert (await query.call_game_function("query", "{}", ""))[:2] == (True, True)
        await reply
        assert query.sent[0]["kind"] == "read_only_function" and "hp=4" in query.sent[0]["value"]
        turn = {"turn": 1, "picked": "observe", "outcome": "passed", "sent": setup.sent + query.sent}
        for line in [*recent_action_lines([turn]), turn_line(turn)]:
            assert "observed query:" in line and "hp=4" in line
            assert "observed give_gold" not in line
    asyncio.run(play())


def test_contract_checks_and_sends_the_resolved_instance_when_target_is_omitted():
    async def play():
        channel, state, _, sent, arch = make()
        functions = PlannerGameFunctions(ToolContext(channel, state, arch))
        host = ChannelMacroHost(channel, None, 1, allowed_targets=frozenset([PLAYER]), game_functions=functions)
        assert not (await host.call_game_function("heal", "{}", ""))[0]
        for instance in [OTHER_PLAYER, PLAYER]:
            pulse(channel, whole=True, functions={"whole": True, "entries": [
                {"kind": "cheat", "name": "heal", "target": instance},
            ]})
            if instance == OTHER_PLAYER:
                assert not (await host.call_game_function("heal", "{}", ""))[0]
                assert not actions(sent)
            else:
                reply = answer(channel, sent, {"id": 1, "success": True})
                assert (await host.call_game_function("heal", "{}", ""))[:2] == (True, True)
                await reply
                assert actions(sent)[0]["payload"]["actions"][0]["params"][0]["target"] == PLAYER
    asyncio.run(play())
