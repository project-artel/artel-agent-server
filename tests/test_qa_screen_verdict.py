"""화면 제안을 판정하는 agent 를 따로 띄운다 (ARTEL-656) — 그리고 `SCREEN_SETTLED` (ARTEL-668).

다섯 가지를 못박는다. 다섯은 서로 다른 방식으로 깨진다:

- **QA 런이 판정을 모르는가** — 인입 frame 을 읽는 loop 가 판정을 기다리면 그동안 `PULSE`
  도 `ACTION_RESULT` 도 안 들어온다. 판정이 걸려 있어도, 터져도, 시간을 넘겨도 `deliver`
  는 즉시 돌아와야 한다.
- **지어내지 않는가** — 형식을 어긴 답은 항목 없는 실패로 나가야 하고, 제안이 물어본 적
  없는 대상을 가리키는 항목은 버려야 한다. 잘못 앉은 항목은 그 `scene` 의 화면을 다음
  관측부터 갈라 놓고 되돌릴 수 없다.
- **한 게임에서만 맞는 판정기가 아닌가** — 기계 규칙 셋이 반례를 낸 자리를 fixture 로
  둔다. 이름이 매번 바뀌는 경우, 이름이 같은 형제 control 둘, 조작 없이 넘어가는 loading
  화면. 셋 다 판정이 살아남아야 하고, prompt 는 특정 게임의 관례를 말하지 않아야 한다.
- **계약대로 나가는가** — `SCREEN_SELECTOR_VERDICT`, `correlationId` 는 제안의
  `messageId`, `match` 는 셋뿐, `pattern` 은 정확 문자열.
- **제안이 한 장도 안 오는 런에서도 화면이 보이는가** — 제안은 `(scene, selector)` 마다
  평생 한 번만 나가므로 이미 한 번 플레이한 빌드에서는 안 온다. `SCREEN_SETTLED` 가 그
  구멍을 메우고, 빈 `discriminator` 는 빠진 값이 아니라 "이 `scene` 이 화면 한 행이다" 라는
  사실로 그려져야 한다.
"""

import asyncio
import json

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableLambda

from app.agents.base import AgentContext
from app.agents.qa.screen import MAX_PATTERN_LENGTH
from app.agents.qa.vision import CaptureFetchError
from app.agents.screen_verdict import (
    ProposedEntry,
    ProposedVerdict,
    ScreenVerdictAgent,
    ScreenVerdictError,
    ScreenVerdictRequest,
)
from app.agents.screen_verdict import capture as capture_module
from app.agents.screen_verdict.agent import ScreenVerdict
from app.agents.screen_verdict.capture import fetch_proposal_captures
from app.agents.screen_verdict.prompt import (
    build_chain_inputs,
    build_screen_verdict_prompt,
)
from app.agents.screen_verdict.validate import usable_entries
from app.llm.models import LLMModel
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType, ScreenSelectorProposalPayload
from app.qa.screen_verdict import ScreenSelectorAdjudicator
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore

_CTX = AgentContext(session_id="screen-verdict-test")


# --- fixtures -----------------------------------------------------------------
#
# 셋 다 실측 게임 하나에서 나온 것이 아니다. 기계 규칙 셋이 각각 어디서 깨지는지를 성격만
# 남기고 쓴 것이고, 이름은 일부러 서로 다른 관례로 지었다 — 한 게임의 관례에 기대는 판정
# 코드가 있으면 셋 중 어딘가에서 걸린다.


def _screen(screen_id: str, discriminator: list[dict], capture: str | None = None) -> dict:
    return {
        "screen_id": screen_id,
        "name": None,
        "discriminator": discriminator,
        "capture_url": capture,
        "capture_expires_at": None,
    }


def _candidate(selector: str, path: str, **counts) -> dict:
    base = {
        "selector": selector,
        "path": path,
        "active": True,
        "instances_in_reading": 1,
        "readings_seen_in_scene": 10,
        "distinct_values_observed": 1,
        "in_whitelist": False,
    }
    base.update(counts)
    return base


def _frame(payload: dict, message_id: str = "prop-1") -> dict:
    return {
        "type": "SCREEN_SELECTOR_PROPOSAL",
        "messageId": message_id,
        "payload": payload,
    }


def counters_in_names() -> dict:
    """이름이 매번 바뀌는 게임.

    한 번에 하나만 있는 것을 게임이 `agent(1)` · `agent(2)` 로 세어 이름에 붙인다. 그래서
    `instances_in_reading` 은 1 인데 `distinct_values_observed` 는 계속 자란다. 관측 하나의
    개수를 규칙으로 삼으면 이것을 놓친다.
    """
    return {
        "reason": "unknown-selector",
        "scene": {"scene_id": "3", "name": "Field"},
        "previous_screen": _screen("10", [{"selector": "hud/menu(0)", "active": True}]),
        "current_screen": _screen(
            "11",
            [{"selector": "hud/menu(0)", "active": True}],
            capture=None,
        ),
        "changes": [{"selector": "world/agent(4)", "was": None, "now": True}],
        "candidates": [
            _candidate(
                "world/agent(4)",
                "world/agent",
                instances_in_reading=1,
                readings_seen_in_scene=3,
                distinct_values_observed=4,
            )
        ],
    }


def same_named_siblings() -> dict:
    """이름이 같은 형제 control 둘.

    확인과 취소가 같은 이름을 달고 나란히 있다. `instances_in_reading` 이 2 라고 스폰된
    것으로 읽어 버리면 서로 다른 두 화면이 한 행에 앉는다.
    """
    return {
        "reason": "unknown-selector",
        "scene": {"scene_id": "8", "name": "shop_popup"},
        "previous_screen": _screen("40", []),
        "current_screen": _screen(
            "41",
            [{"selector": "UIRoot[0]/Dialog[1]/Btn[0]", "active": True}],
            capture=None,
        ),
        "changes": [
            {"selector": "UIRoot[0]/Dialog[1]/Btn[0]", "was": False, "now": True},
            {"selector": "UIRoot[0]/Dialog[1]/Btn[1]", "was": False, "now": True},
        ],
        "candidates": [
            _candidate(
                "UIRoot[0]/Dialog[1]/Btn[0]",
                "UIRoot/Dialog/Btn",
                instances_in_reading=2,
            ),
            _candidate(
                "UIRoot[0]/Dialog[1]/Btn[1]",
                "UIRoot/Dialog/Btn",
                instances_in_reading=2,
            ),
        ],
    }


def loading_becomes_game() -> dict:
    """조작 없이 넘어가는 loading 화면.

    아무도 아무것도 누르지 않았는데 화면이 바뀌었다. "조작 없이 바뀌었으니 화면을 안
    가른다" 를 규칙으로 삼으면 loading 과 본 화면이 한 행에 앉는다.
    """
    return {
        "reason": "unknown-selector",
        "scene": {"scene_id": "1", "name": "boot"},
        "previous_screen": _screen(
            "1", [{"selector": "Root/LoadingVeil", "active": True}]
        ),
        "current_screen": _screen(
            "2", [{"selector": "Root/LoadingVeil", "active": False}], capture=None
        ),
        "changes": [
            {"selector": "Root/LoadingVeil", "was": True, "now": False},
            {"selector": "Root/Board/Deck", "was": None, "now": True},
        ],
        "candidates": [
            _candidate(
                "Root/Board/Deck",
                "Root/Board/Deck",
                readings_seen_in_scene=1,
            )
        ],
    }


def _payload(raw: dict) -> ScreenSelectorProposalPayload:
    return ScreenSelectorProposalPayload.model_validate(raw)


# --- 기계 규칙 셋이 깨진 자리가 판정을 통과하는가 -------------------------------


@pytest.mark.parametrize(
    "proposal, answer, expected",
    [
        # 이름이 매번 바뀌므로 `path` 로 답한다. 관측 하나의 개수가 1 이라고 버리면 안 된다.
        (
            counters_in_names(),
            ProposedEntry(
                match="path",
                pattern="world/agent",
                screen_defining=True,
                reason="The agent panel is on screen only while it is the player's turn.",
            ),
            ("path", "world/agent", True),
        ),
        # 이름이 같아도 둘은 서로 다른 control 이다. 하나로 합치면 안 된다.
        (
            same_named_siblings(),
            ProposedEntry(
                match="selector",
                pattern="UIRoot[0]/Dialog[1]/Btn[1]",
                screen_defining=True,
                reason="The second button is the one that appears only on the confirm dialog.",
            ),
            ("selector", "UIRoot[0]/Dialog[1]/Btn[1]", True),
        ),
        # 조작 없이 바뀌었지만 loading 화면과 본 화면은 다른 화면이다.
        (
            loading_becomes_game(),
            ProposedEntry(
                match="selector",
                pattern="Root/Board/Deck",
                screen_defining=True,
                reason="The deck is only there once loading is over and the board is up.",
            ),
            ("selector", "Root/Board/Deck", True),
        ),
    ],
)
def test_the_cases_that_broke_the_machine_rules_still_get_an_answer(
    proposal, answer, expected
) -> None:
    kept, dropped = usable_entries([answer], _payload(proposal).candidates)

    assert dropped == []
    assert [(entry.match, entry.pattern, entry.screen_defining) for entry in kept] == [
        expected
    ]


@pytest.mark.parametrize(
    "proposal",
    [counters_in_names(), same_named_siblings(), loading_becomes_game()],
    ids=["counters", "siblings", "loading"],
)
def test_the_model_is_shown_everything_the_proposal_carries(proposal) -> None:
    """판정이 게임을 모른 채 나려면 제안에 실린 것이 하나도 안 빠져야 한다."""
    request = ScreenVerdictRequest(proposal=_payload(proposal))

    shown = json.loads(build_chain_inputs(request, captures=[])["proposal"])

    assert shown["reason"] == proposal["reason"]
    assert shown["previous_screen"] == proposal["previous_screen"]
    assert shown["current_screen"] == proposal["current_screen"]
    assert shown["changes"] == proposal["changes"]
    assert shown["candidates"] == proposal["candidates"]


def test_prompt_names_no_game_and_no_engine() -> None:
    """한 게임의 관례가 prompt 에 들어가면 그 게임에서만 맞는 판정기가 된다."""
    system = build_screen_verdict_prompt().messages[0].prompt.template

    for convention in (
        "(Clone)",
        "Unity",
        "MonoBehaviour",
        "GameObject",
        "Canvas",
        "TurnBattle",
        "CombineSystem",
        "prefab",
    ):
        assert convention not in system, convention


def test_prompt_says_the_counts_are_not_rules() -> None:
    """통계를 규칙으로 옮겨 적는 순간 반례가 난 규칙을 다시 구현하는 것이다."""
    system = build_screen_verdict_prompt().messages[0].prompt.template

    assert "None of them decides anything." in system
    assert "instances_in_reading" in system
    assert "readings_seen_in_scene" in system
    assert "distinct_values_observed" in system
    # 조작 없이 바뀌는 화면이 반례라는 것이 prompt 에 있어야 한다.
    assert "loading screen" in system


def test_prompt_forbids_regular_expressions() -> None:
    system = build_screen_verdict_prompt().messages[0].prompt.template

    assert "never a regular expression" in system
    assert str(MAX_PATTERN_LENGTH) in build_chain_inputs(
        ScreenVerdictRequest(proposal=_payload(counters_in_names())), captures=[]
    )["max_pattern_length"]


# --- 지어내지 않는다 -----------------------------------------------------------


@pytest.mark.parametrize(
    "entry, why",
    [
        (
            ProposedEntry(
                match="regex", pattern="world/agent", screen_defining=True, reason="a"
            ),
            "match",
        ),
        (
            ProposedEntry(match="path", pattern="  ", screen_defining=True, reason="a"),
            "pattern is empty",
        ),
        (
            ProposedEntry(
                match="path",
                pattern="x" * (MAX_PATTERN_LENGTH + 1),
                screen_defining=True,
                reason="a",
            ),
            "longer than",
        ),
        (
            ProposedEntry(
                match="path", pattern="world/agent", screen_defining=True, reason=" "
            ),
            "reason is empty",
        ),
        (
            ProposedEntry(
                match="path", pattern="world/.*", screen_defining=True, reason="a"
            ),
            "does not name anything",
        ),
        (
            ProposedEntry(
                match="selector",
                pattern="world/agent",
                screen_defining=True,
                reason="a",
            ),
            "does not name anything",
        ),
        (
            ProposedEntry(
                match="path", pattern="world/enemy", screen_defining=True, reason="a"
            ),
            "does not name anything",
        ),
    ],
    ids=["bad-match", "empty-pattern", "long-pattern", "no-reason", "regex", "wrong-kind", "invented"],
)
def test_an_entry_that_names_nothing_asked_about_is_dropped(entry, why) -> None:
    kept, dropped = usable_entries([entry], _payload(counters_in_names()).candidates)

    assert kept == []
    assert len(dropped) == 1
    assert why in dropped[0].reason


def test_a_subtree_entry_matches_at_node_boundaries_only() -> None:
    candidates = _payload(loading_becomes_game()).candidates

    covering, _ = usable_entries(
        [
            ProposedEntry(
                match="subtree",
                pattern="Root/Board",
                screen_defining=True,
                reason="The whole board branch comes and goes as one.",
            )
        ],
        candidates,
    )
    assert [(entry.match, entry.pattern) for entry in covering] == [
        ("subtree", "Root/Board")
    ]

    # `Root/Boa` 는 `Root/Board` 의 조상이 아니다. 문자열 접두로 맞추면 여기서 걸린다.
    partial, dropped = usable_entries(
        [
            ProposedEntry(
                match="subtree", pattern="Root/Boa", screen_defining=True, reason="a"
            )
        ],
        candidates,
    )
    assert partial == []
    assert "does not name anything" in dropped[0].reason


def test_answering_one_target_both_ways_drops_it_entirely() -> None:
    """어느 쪽을 골라도 절반은 모델이 하지 않은 판정이 된다."""
    candidates = _payload(counters_in_names()).candidates

    kept, dropped = usable_entries(
        [
            ProposedEntry(
                match="path", pattern="world/agent", screen_defining=True, reason="a"
            ),
            ProposedEntry(
                match="path", pattern="world/agent", screen_defining=False, reason="b"
            ),
        ],
        candidates,
    )

    assert kept == []
    assert len(dropped) == 2
    assert all("both ways" in item.reason for item in dropped)


def test_the_same_answer_twice_is_stored_once() -> None:
    candidates = _payload(counters_in_names()).candidates

    kept, dropped = usable_entries(
        [
            ProposedEntry(
                match="path", pattern="world/agent", screen_defining=True, reason="first"
            ),
            ProposedEntry(
                match="path", pattern="world/agent", screen_defining=True, reason="again"
            ),
        ],
        candidates,
    )

    assert [entry.reason for entry in kept] == ["first"]
    assert len(dropped) == 1


# --- agent -------------------------------------------------------------------


def _agent(answer) -> ScreenVerdictAgent:
    def respond(_inputs):
        if isinstance(answer, Exception):
            raise answer
        return answer

    return ScreenVerdictAgent(structured_factory=lambda model: RunnableLambda(respond))


def test_a_malformed_reply_is_a_failure_and_not_an_invention() -> None:
    agent = _agent(OutputParserException("not json"))

    with pytest.raises(ScreenVerdictError):
        asyncio.run(
            agent.run(
                ScreenVerdictRequest(proposal=_payload(counters_in_names())), _CTX
            )
        )


def test_an_empty_answer_is_a_complete_answer() -> None:
    agent = _agent(ProposedVerdict(entries=[], note=None))

    verdict = asyncio.run(
        agent.run(ScreenVerdictRequest(proposal=_payload(counters_in_names())), _CTX)
    )

    assert verdict.entries == []
    assert verdict.dropped == []


def test_the_agent_reports_what_it_dropped() -> None:
    agent = _agent(
        ProposedVerdict(
            entries=[
                ProposedEntry(
                    match="path",
                    pattern="world/agent",
                    screen_defining=True,
                    reason="It is only up on the player's turn.",
                ),
                ProposedEntry(
                    match="path", pattern="world/nowhere", screen_defining=True, reason="a"
                ),
            ]
        )
    )

    verdict = asyncio.run(
        agent.run(ScreenVerdictRequest(proposal=_payload(counters_in_names())), _CTX)
    )

    assert [entry.pattern for entry in verdict.entries] == ["world/agent"]
    assert len(verdict.dropped) == 1


def test_a_model_that_cannot_see_is_not_sent_a_capture(monkeypatch) -> None:
    """볼 수 없는 모델에게 그림을 보내면 호출이 통째로 거절된다."""
    asked: list[str] = []

    async def never(url: str) -> bytes:
        asked.append(url)
        raise AssertionError("a text-only model must not trigger a capture fetch")

    monkeypatch.setattr(capture_module, "download_capture", never)
    proposal = counters_in_names()
    proposal["current_screen"]["capture_url"] = "https://example.invalid/shot.jpg"

    captured: list[dict] = []

    def record(inputs):
        captured.append(inputs)
        return ProposedVerdict(entries=[])

    agent = ScreenVerdictAgent(
        structured_factory=lambda model: RunnableLambda(record)
    )
    # gemma_4_free 도 그림을 보므로, 카탈로그를 건드리지 않고 "못 본다" 를 만들려면
    # spec 을 갈아 끼우는 대신 이 경로를 직접 확인한다.
    monkeypatch.setattr(
        "app.agents.screen_verdict.agent.get_model_spec",
        lambda model: type("Spec", (), {"supports_vision": False})(),
    )

    asyncio.run(
        agent.run(
            ScreenVerdictRequest(proposal=_payload(proposal), model=LLMModel.gpt_4o),
            _CTX,
        )
    )

    assert asked == []


def test_a_capture_that_cannot_be_fetched_does_not_stop_the_verdict(monkeypatch) -> None:
    async def gone(url: str) -> bytes:
        raise CaptureFetchError("storage answered 403 — the link may have expired")

    monkeypatch.setattr(capture_module, "download_capture", gone)
    proposal = counters_in_names()
    proposal["current_screen"]["capture_url"] = "https://example.invalid/shot.jpg"

    messages = asyncio.run(fetch_proposal_captures(_payload(proposal)))

    assert messages == []


def test_a_fetched_capture_carries_the_type_its_bytes_say(monkeypatch) -> None:
    """제안은 mime 을 안 싣는다. 확장자를 믿으면 provider 가 그림을 거절한다."""

    async def png(url: str) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"body"

    monkeypatch.setattr(capture_module, "download_capture", png)
    proposal = counters_in_names()
    proposal["current_screen"]["capture_url"] = "https://example.invalid/shot.jpg"

    messages = asyncio.run(fetch_proposal_captures(_payload(proposal)))

    assert len(messages) == 1
    assert messages[0].content[1]["image_url"]["url"].startswith("data:image/png;base64,")


# --- QA 런과의 격리 ------------------------------------------------------------


class _StubAgent:
    """정해진 답을 내는 판정 agent. `run` 이 오래 걸리거나 터지게 할 수 있다."""

    def __init__(self, verdict=None, error: Exception | None = None, delay: float = 0.0):
        self._verdict = verdict or ScreenVerdict(entries=[], note=None, dropped=[])
        self._error = error
        self._delay = delay
        self.calls = 0

    async def run(self, request, context):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._verdict


def _channel():
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    return QaRunChannel(qa_try_id=7, send=send), sent


def _verdict_frames(sent: list[dict]) -> list[dict]:
    return [
        frame for frame in sent if frame["type"] == MessageType.SCREEN_SELECTOR_VERDICT
    ]


def test_the_verdict_answers_the_proposal_it_was_asked_about() -> None:
    from app.qa.envelope import ScreenSelectorEntry

    entry = ScreenSelectorEntry(
        match="path",
        pattern="world/agent",
        screen_defining=True,
        reason="It is only up on the player's turn.",
    )

    async def scenario():
        channel, sent = _channel()
        adjudicator = ScreenSelectorAdjudicator(
            agent=_StubAgent(ScreenVerdict(entries=[entry], note=None, dropped=[]))
        )
        adjudicator.answer_later(channel, _frame(counters_in_names(), "prop-77"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return sent

    sent = asyncio.run(scenario())

    frames = _verdict_frames(sent)
    assert len(frames) == 1
    assert frames[0]["correlationId"] == "prop-77"
    assert frames[0]["payload"]["proposal_id"] == "prop-77"
    assert frames[0]["payload"]["entries"] == [
        {
            "match": "path",
            "pattern": "world/agent",
            "screen_defining": True,
            "reason": "It is only up on the player's turn.",
        }
    ]


def test_a_failing_verdict_answers_with_no_entries() -> None:
    async def scenario():
        channel, sent = _channel()
        adjudicator = ScreenSelectorAdjudicator(
            agent=_StubAgent(error=ScreenVerdictError("the model did not answer in shape"))
        )
        adjudicator.answer_later(channel, _frame(counters_in_names()))
        for _ in range(4):
            await asyncio.sleep(0)
        return sent

    frames = _verdict_frames(asyncio.run(scenario()))

    assert len(frames) == 1
    assert frames[0]["payload"]["entries"] == []
    assert "could not be judged" in frames[0]["payload"]["note"]


def test_a_verdict_past_its_deadline_answers_with_no_entries() -> None:
    async def scenario():
        channel, sent = _channel()
        adjudicator = ScreenSelectorAdjudicator(
            agent=_StubAgent(delay=5.0), timeout=0.01
        )
        adjudicator.answer_later(channel, _frame(counters_in_names()))
        await asyncio.sleep(0.05)
        return sent

    frames = _verdict_frames(asyncio.run(scenario()))

    assert len(frames) == 1
    assert frames[0]["payload"]["entries"] == []
    assert "longer than" in frames[0]["payload"]["note"]


def test_a_slow_verdict_does_not_hold_the_inbound_loop() -> None:
    """`deliver` 가 판정을 기다리면 그 사이 `PULSE` 도 `ACTION_RESULT` 도 안 들어온다."""

    async def scenario():
        store = InMemoryQaSessionStore()
        service = QaExecutionService(store)
        channel, sent = _channel()
        service._channels["s"] = channel
        service._adjudicators["s"] = ScreenSelectorAdjudicator(
            agent=_StubAgent(delay=30.0)
        )

        accepted = service.deliver("s", _frame(counters_in_names()))
        # 아직 한 장도 안 나갔고, 그래도 다음 frame 이 곧바로 처리된다.
        delivered_next = service.deliver(
            "s", {"type": "PULSE", "payload": {"schema": 2, "reading": 1, "scene": "Field"}}
        )
        service._adjudicators["s"].close()
        return accepted, delivered_next, sent

    accepted, delivered_next, sent = asyncio.run(scenario())

    assert accepted is True
    assert delivered_next is True
    assert _verdict_frames(sent) == []


def test_a_cancelled_run_is_not_answered_over_a_closing_socket() -> None:
    async def scenario():
        channel, sent = _channel()
        adjudicator = ScreenSelectorAdjudicator(agent=_StubAgent())
        channel.on_cancel()
        adjudicator.answer_later(channel, _frame(counters_in_names()))
        for _ in range(4):
            await asyncio.sleep(0)
        return sent

    assert _verdict_frames(asyncio.run(scenario())) == []


def test_closing_the_adjudicator_stops_what_is_still_running() -> None:
    async def scenario():
        channel, sent = _channel()
        stub = _StubAgent(delay=30.0)
        adjudicator = ScreenSelectorAdjudicator(agent=stub)
        adjudicator.answer_later(channel, _frame(counters_in_names()))
        await asyncio.sleep(0)
        adjudicator.close()
        await asyncio.sleep(0)
        return sent, adjudicator

    sent, adjudicator = asyncio.run(scenario())

    assert _verdict_frames(sent) == []
    assert adjudicator._running == set()


def test_a_proposal_with_no_candidates_never_reaches_the_model() -> None:
    async def scenario():
        channel, _sent = _channel()
        stub = _StubAgent()
        adjudicator = ScreenSelectorAdjudicator(agent=stub)
        empty = counters_in_names()
        empty["candidates"] = []
        adjudicator.answer_later(channel, _frame(empty))
        await asyncio.sleep(0)
        return stub

    assert asyncio.run(scenario()).calls == 0


def test_a_proposal_without_a_message_id_is_not_answered() -> None:
    async def scenario():
        channel, sent = _channel()
        stub = _StubAgent()
        adjudicator = ScreenSelectorAdjudicator(agent=stub)
        frame = _frame(counters_in_names())
        del frame["messageId"]
        adjudicator.answer_later(channel, frame)
        await asyncio.sleep(0)
        return stub, sent

    stub, sent = asyncio.run(scenario())

    assert stub.calls == 0
    assert _verdict_frames(sent) == []


def test_too_many_verdicts_at_once_are_dropped_rather_than_queued() -> None:
    async def scenario():
        channel, sent = _channel()
        adjudicator = ScreenSelectorAdjudicator(
            agent=_StubAgent(delay=30.0), max_in_flight=2
        )
        for index in range(5):
            adjudicator.answer_later(channel, _frame(counters_in_names(), f"p{index}"))
        await asyncio.sleep(0)
        running = len(adjudicator._running)
        adjudicator.close()
        return running

    assert asyncio.run(scenario()) == 2


# --- SCREEN_SETTLED (ARTEL-668) ----------------------------------------------
#
# 제안은 `(scene, selector)` 마다 평생 한 번만 나간다. 이미 한 번 플레이한 빌드에서는 한
# 장도 안 오고, 그때 QA agent 는 자기가 어느 `screen` 에 있는지 못 본다 — 목록을 고치는
# tool 둘이 부를 계기를 잃는 상태다. 이 프레임이 그 구멍을 메운다.


def settled(
    scene: str = "Field",
    screen_id: str = "11",
    previous_screen_id: str | None = "10",
    discriminator: list[dict] | None = None,
) -> dict:
    """`SCREEN_SETTLED` 한 장. payload 철자가 제안과 같다."""
    entries = (
        discriminator
        if discriminator is not None
        else [{"selector": "hud/menu(0)", "active": True}]
    )
    return {
        "type": "SCREEN_SETTLED",
        "messageId": "settled-1",
        "correlationId": None,
        "payload": {
            "scene": {"scene_id": "3", "name": scene},
            "previous_screen": (
                None
                if previous_screen_id is None
                else _screen(previous_screen_id, [])
            ),
            "current_screen": _screen(screen_id, entries),
        },
    }


def _service_with_channel():
    store = InMemoryQaSessionStore()
    service = QaExecutionService(store)
    channel, sent = _channel()
    service._channels["s"] = channel
    return service, channel, sent


def test_the_agent_knows_its_screen_in_a_run_with_no_proposal_at_all() -> None:
    """이 프레임이 존재하는 이유. 제안이 한 장도 안 오는 런에서도 화면이 보여야 한다."""
    service, channel, _sent = _service_with_channel()

    assert service.deliver("s", settled()) is True
    channel.on_pulse({"payload": {"schema": 2, "reading": 1, "scene": "Field"}})

    block = channel.scene.screen_map_block()
    assert block is not None
    assert "you are on screen 11 of Field" in block
    assert "reached from screen 10" in block
    assert "told apart by: hud/menu(0) on" in block


def test_a_settled_frame_is_not_sent_to_the_adjudicator() -> None:
    """이 프레임은 아무것도 안 물어보고 후보도 안 싣는다."""

    async def scenario():
        service, _channel_, _sent = _service_with_channel()
        stub = _StubAgent()
        service._adjudicators["s"] = ScreenSelectorAdjudicator(agent=stub)
        accepted = service.deliver("s", settled())
        await asyncio.sleep(0)
        return accepted, stub

    accepted, stub = asyncio.run(scenario())

    assert accepted is True
    assert stub.calls == 0


def test_an_empty_discriminator_is_reported_as_the_state_it_is() -> None:
    """빠진 값이 아니라 사실이다 — 그 `scene` 의 모든 관측이 화면 한 행에 앉아 있다."""
    service, channel, _sent = _service_with_channel()

    service.deliver("s", settled(discriminator=[]))
    channel.on_pulse({"payload": {"schema": 2, "reading": 1, "scene": "Field"}})

    block = channel.scene.screen_map_block()
    assert block is not None
    assert "told apart by: nothing" in block
    assert "every observation in this scene lands on this one screen row" in block


def test_a_later_settled_frame_replaces_the_earlier_one() -> None:
    service, channel, _sent = _service_with_channel()

    service.deliver("s", settled(screen_id="11", previous_screen_id="10"))
    service.deliver("s", settled(screen_id="12", previous_screen_id="11"))
    channel.on_pulse({"payload": {"schema": 2, "reading": 1, "scene": "Field"}})

    block = channel.scene.screen_map_block()
    assert "you are on screen 12 of Field" in block
    assert "reached from screen 11" in block


def test_another_scenes_settled_frame_is_not_drawn_as_the_current_screen() -> None:
    service, channel, _sent = _service_with_channel()

    service.deliver("s", settled(scene="Field", screen_id="11"))
    channel.on_pulse({"payload": {"schema": 2, "reading": 1, "scene": "shop_popup"}})

    assert channel.scene.screen_map_block() is None


def test_an_unreadable_settled_frame_does_not_end_the_run() -> None:
    service, channel, _sent = _service_with_channel()
    service.deliver("s", settled(screen_id="11"))
    broken = settled()
    broken["payload"]["current_screen"] = {"discriminator": "not a list"}

    assert service.deliver("s", broken) is False
    channel.on_pulse({"payload": {"schema": 2, "reading": 1, "scene": "Field"}})
    # 종전 판정이 살아 있다. 못 읽은 한 장이 이미 아는 것을 지우면 안 된다.
    assert "you are on screen 11 of Field" in channel.scene.screen_map_block()
