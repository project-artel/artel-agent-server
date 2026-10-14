"""`search_macros` 가 `MACRO_SEARCH` 를 보내고 답을 첫 메시지 목록과 같은 모양으로 그리는지 (ARTEL-984).

저쪽 철자는 orchestration 의 구현(ARTEL-986)이 정했다. 요청은 `query` · `limit` · `step` 이고,
답은 `items` 와 `vectorAvailable`, 항목마다 `name` · `parameters` · `docstring` · `screens`
(`screenId`, `sceneName`) · `matchedBy` · `score` 다. 이 파일의 답 JSON 은 그 철자를 그대로
적은 것이다 — 이쪽 모델의 철자로 고쳐 적으면 철자가 어긋나도 테스트가 통과한다.
"""

import asyncio
from unittest.mock import patch

from tests.test_qa_macro_tools import call, macro_frames, make, refuse_macro

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools.phase import ALWAYS_ALLOWED, RunPhase, build_phase_cycle
from app.llm.models import LLMModel
from app.qa.channel import QaRunChannel
from app.qa.envelope import MacroSearchResultPayload, MessageType
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore

# orchestration 이 보내는 답 하나, 철자 그대로.
FOUND = {
    "items": [
        {
            "name": "cast_pair_at_enemy",
            "parameters": [
                {"name": "spell", "type": "object"},
                {"name": "enemy", "type": "object"},
            ],
            "docstring": "Cast the spell card and the element card at one enemy.\n\n"
            "Use it on the battle screen.",
            "screens": [
                {"screenId": "7", "sceneName": "TurnBattleScene"},
                {"screenId": "8", "sceneName": "TurnBattleScene"},
            ],
            "matchedBy": ["KEYWORD", "VECTOR"],
            "score": 0.0328,
        },
        {
            "name": "advance_dialogue",
            "parameters": [],
            "docstring": None,
            "screens": [],
            "matchedBy": ["VECTOR"],
            "score": 0.0161,
        },
    ],
    "vectorAvailable": True,
}


def answer_search(channel: QaRunChannel, sent: list[dict], payload: dict):
    """저쪽이 답하는 것처럼, 방금 나간 `MACRO_SEARCH` 의 `messageId` 를 물고."""
    already = len(macro_frames(sent, MessageType.MACRO_SEARCH))

    async def reply() -> None:
        for _ in range(200):
            if len(macro_frames(sent, MessageType.MACRO_SEARCH)) > already:
                break
            await asyncio.sleep(0)
        channel.on_macro_search_result(
            {
                "type": MessageType.MACRO_SEARCH_RESULT.value,
                "correlationId": macro_frames(sent, MessageType.MACRO_SEARCH)[-1]["messageId"],
                "payload": payload,
            }
        )

    return asyncio.create_task(reply())


def search(tools, reply=None, **arguments) -> str:
    async def both() -> str:
        if reply is not None:
            reply()
        return await tools["search_macros"].ainvoke({"step": 4, "thought": "t", **arguments})

    return asyncio.run(both())


def test_the_literal_wire_json_parses() -> None:
    parsed = MacroSearchResultPayload.model_validate(FOUND)

    first = parsed.items[0]
    assert parsed.vector_available is True
    assert first.matched_by == ["KEYWORD", "VECTOR"]
    assert [(one.screen_id, one.scene_name) for one in first.screens] == [
        ("7", "TurnBattleScene"),
        ("8", "TurnBattleScene"),
    ]
    assert parsed.items[1].docstring is None
    assert MacroSearchResultPayload.model_validate({"items": []}).vector_available is True
    assert MacroSearchResultPayload.model_validate(
        {"items": [], "vectorAvailable": False}
    ).vector_available is False


def test_the_request_carries_query_limit_and_step() -> None:
    channel, _state, tools, sent = make()

    search(
        tools,
        lambda: answer_search(channel, sent, FOUND),
        query="  cast spell at enemy ",
        limit=3,
    )

    frames = macro_frames(sent, MessageType.MACRO_SEARCH)
    assert len(frames) == 1
    assert frames[0]["payload"] == {"query": "cast spell at enemy", "limit": 3, "step": 4}


def test_results_are_drawn_as_defs_with_their_docstrings() -> None:
    channel, _state, tools, sent = make()

    answer = search(tools, lambda: answer_search(channel, sent, FOUND), query="cast spell")

    assert answer == (
        "Registered macros matching 'cast spell', best first:\n"
        "def cast_pair_at_enemy(spell: object, enemy: object)  [TurnBattleScene]\n"
        '    """Cast the spell card and the element card at one enemy."""\n'
        "def advance_dialogue()  [no screen yet]\n"
        "`run_macro` calls one by name; `read_macro` shows its source first. Check the "
        "scene — a macro tagged with a scene was written there."
    )
    assert macro_frames(sent, MessageType.MACRO_SEARCH)[0]["payload"]["limit"] == 5


def test_keyword_only_results_say_so() -> None:
    channel, _state, tools, sent = make()

    answer = search(
        tools,
        lambda: answer_search(channel, sent, {**FOUND, "vectorAvailable": False}),
        query="cast",
    )

    assert "(keyword matches only; similarity search was unavailable)" in answer


def test_no_result_says_so_and_suggests_writing_one() -> None:
    channel, _state, tools, sent = make()

    answer = search(
        tools,
        lambda: answer_search(channel, sent, {"items": [], "vectorAvailable": True}),
        query="open the shop",
    )

    assert "No registered macro matches 'open the shop'" in answer
    assert "`write_macro`" in answer


def test_an_error_frame_says_search_is_unavailable_and_what_still_works() -> None:
    channel, _state, tools, sent = make()

    answer = search(
        tools,
        lambda: refuse_macro(channel, sent, MessageType.MACRO_SEARCH, "no content map"),
        query="cast",
    )

    assert answer.startswith("Macro search is unavailable right now")
    assert "`read_macro`" in answer and "first message" in answer


def test_no_answer_says_the_same() -> None:
    _channel, _state, tools, _sent = make()

    with patch("app.qa.channel.KNOWLEDGE_SEARCH_TIMEOUT_SECONDS", 0.05):
        answer = search(tools, query="cast")

    assert answer.startswith("Macro search is unavailable right now")


def test_a_blank_query_or_a_limit_out_of_range_sends_nothing() -> None:
    _channel, _state, tools, sent = make()

    blank = call(tools["search_macros"], query="   ")
    too_many = call(tools["search_macros"], query="cast", limit=11)
    none = call(tools["search_macros"], query="cast", limit=0)
    too_long = call(tools["search_macros"], query="x" * 501)

    assert "nothing was searched" in blank
    assert "between 1 and 10" in too_many and "between 1 and 10" in none
    assert "500" in too_long
    assert macro_frames(sent, MessageType.MACRO_SEARCH) == []


def test_the_service_routes_the_answer_to_the_waiting_tool() -> None:
    channel, _state, tools, sent = make()
    service = QaExecutionService(store=InMemoryQaSessionStore())
    service._channels["s"] = channel

    async def both() -> str:
        async def reply() -> None:
            while not macro_frames(sent, MessageType.MACRO_SEARCH):
                await asyncio.sleep(0)
            accepted = service.deliver(
                "s",
                {
                    "type": MessageType.MACRO_SEARCH_RESULT.value,
                    "correlationId": macro_frames(sent, MessageType.MACRO_SEARCH)[0]["messageId"],
                    "payload": FOUND,
                },
            )
            assert accepted

        asyncio.create_task(reply())
        return await tools["search_macros"].ainvoke({"step": 1, "thought": "t", "query": "cast"})

    assert "def cast_pair_at_enemy(" in asyncio.run(both())


def test_search_macros_is_open_in_every_phase_and_ends_none() -> None:
    assert "search_macros" in ALWAYS_ALLOWED
    cycle = build_phase_cycle(PhaseCycleMode.full)
    for phase in RunPhase:
        cycle.phase = phase
        assert cycle.refusal_for("search_macros") is None
        cycle.advance("search_macros")
        assert cycle.phase is phase


def test_macros_off_removes_search_macros() -> None:
    async def discard(frame: dict) -> None:
        pass

    channel = QaRunChannel(qa_try_id=9, send=discard)
    for macros, offered in (("on", True), ("off", False)):
        arch = resolve_arch(
            QaArchSpec(vision=VisionMode.on, phase_cycle=PhaseCycleMode.lite, macros=macros),
            "openai/gpt-6-luna",
        )
        names = {one.name for one in build_tools(channel, QaRunState(total_steps=1), arch)}
        assert ("search_macros" in names) is offered
