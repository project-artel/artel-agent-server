"""새로 굳은 `screen` 에 이름을 짓는 단발 agent (ARTEL-909).

네 가지를 못박는다. 넷은 서로 다른 방식으로 깨진다:

- **QA 런이 이름 짓기를 모르는가** — 인입 frame 을 읽는 loop 가 이름 하나를 기다리면 그
  사이 `PULSE` 도 `ACTION_RESULT` 도 안 들어온다. 이름 짓기가 걸려 있어도, 터져도, 시간을
  넘겨도 `deliver` 는 즉시 돌아와야 한다.
- **지어내지 않는가** — 형식을 어긴 답, 상한을 넘긴 이름, selector 를 그대로 옮긴 답은
  전부 이름 없는 답으로 나가야 한다. 이름은 표시값이라 없는 것이 정상이고, 지어낸 이름은
  사람이 content map 에서 그 화면을 계속 잘못 찾게 만든다.
- **화면을 부르는가, 목록을 옮겨 적는가** — `discriminator` 는 기계 문자열 목록이다.
  prompt 가 그 차이를 말해야 하고, 근거가 하나도 없는 요청은 모델까지 가지도 않아야 한다.
- **계약대로 나가는가** — `SCREEN_NAME`, `correlationId` 는 요청의 `request_id`,
  payload 는 `request_id` · `name` · `note` 셋.
"""

import asyncio
import json

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableLambda

from app.agents.base import AgentContext
from app.agents.qa.vision import CaptureFetchError
from app.agents.screen_name import (
    ProposedName,
    ScreenName,
    ScreenNameAgent,
    ScreenNameError,
    ScreenNameRequest,
)
from app.agents.screen_name import capture as capture_module
from app.agents.screen_name.capture import fetch_screen_capture
from app.agents.screen_name.prompt import build_chain_inputs, build_screen_name_prompt
from app.agents.screen_name.validate import usable_name
from app.llm.models import LLMModel
from app.qa.channel import QaRunChannel
from app.qa.envelope import (
    MAX_SCREEN_NAME_LENGTH,
    MessageType,
    ScreenNameRequestPayload,
    ScreenSelectorSceneRef,
    ScreenSelectorScreenRef,
)
from app.qa.screen_name import ScreenNamer
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore

_CTX = AgentContext(session_id="screen-name-test")


# --- fixtures -----------------------------------------------------------------


def _screen(
    screen_id: str = "11",
    discriminator: list[dict] | None = None,
    capture: str | None = None,
    name: str | None = None,
) -> dict:
    return {
        "screen_id": screen_id,
        "name": name,
        "discriminator": (
            discriminator
            if discriminator is not None
            else [{"selector": "UIRoot[0]/Title[0]", "active": True}]
        ),
        "capture_url": capture,
        "capture_expires_at": None,
    }


def _request(
    request_id: str = "name-1",
    screen: dict | None = None,
    scene: dict | None = None,
) -> dict:
    return {
        "request_id": request_id,
        "screen": screen if screen is not None else _screen(),
        "scene": scene if scene is not None else {"scene_id": "3", "name": "Field"},
    }


def _frame(payload: dict, message_id: str = "msg-1") -> dict:
    return {
        "type": "SCREEN_NAME_REQUEST",
        "messageId": message_id,
        "payload": payload,
    }


def _agent_request(payload: dict) -> ScreenNameRequest:
    parsed = ScreenNameRequestPayload.model_validate(payload)
    return ScreenNameRequest(screen=parsed.screen, scene=parsed.scene)


# --- prompt -------------------------------------------------------------------


def test_the_model_is_shown_the_screen_and_the_scene_it_sits_in() -> None:
    """같은 `scene` 의 다른 화면과 구별되는 이름이 나오려면 둘 다 보여야 한다."""
    payload = _request(
        screen=_screen(discriminator=[{"selector": "Shop/Weapons", "active": True}])
    )

    inputs = build_chain_inputs(_agent_request(payload), captures=[])

    assert json.loads(inputs["scene"])["name"] == "Field"
    shown = json.loads(inputs["screen"])
    assert shown["screen_id"] == "11"
    assert shown["discriminator"] == [{"selector": "Shop/Weapons", "active": True}]


def test_the_signed_capture_url_is_not_put_in_front_of_the_model() -> None:
    """주소는 근거가 아니라 전송 수단이다. 그림 자체는 자기 turn 으로 온다."""
    payload = _request(screen=_screen(capture="https://example.invalid/shot.jpg?sig=x"))

    shown = json.loads(build_chain_inputs(_agent_request(payload), captures=[])["screen"])

    assert "capture_url" not in shown
    assert "capture_expires_at" not in shown


def test_the_prompt_separates_naming_a_screen_from_restating_its_selectors() -> None:
    """이 agent 의 실패 모드가 그것이다 — 이름 대신 목록을 옮겨 적는 것."""
    system = build_screen_name_prompt().messages[0].prompt.template

    assert "Canvas with continue button active" in system
    assert "is a restatement" in system
    assert "Title screen" in system


def test_the_prompt_says_a_missing_name_is_an_answer() -> None:
    system = build_screen_name_prompt().messages[0].prompt.template

    assert "`null`" in system
    assert str(MAX_SCREEN_NAME_LENGTH) in build_chain_inputs(
        _agent_request(_request()), captures=[]
    )["max_name_length"]


def test_the_prompt_names_no_game_and_no_engine() -> None:
    """한 게임의 관례가 prompt 에 들어가면 그 게임에서만 맞는 이름이 나온다."""
    system = build_screen_name_prompt().messages[0].prompt.template

    for convention in ("(Clone)", "Unity", "MonoBehaviour", "GameObject", "prefab"):
        assert convention not in system, convention


def test_the_prompt_changes_when_no_capture_came() -> None:
    """라벨만 남은 자리는 "그림이 있었는데 못 봤다" 로 읽히고, 그때 모델이 묘사를 시작한다."""
    request = _agent_request(_request())

    with_none = build_chain_inputs(request, captures=[])["capture_note"]
    with_one = build_chain_inputs(request, captures=["a picture"])["capture_note"]

    assert "No capture came with this request" in with_none
    assert with_one != with_none


# --- 지어내지 않는다 -----------------------------------------------------------


def test_a_name_over_the_cap_is_discarded_rather_than_cut() -> None:
    """잘린 이름은 틀린 이름이고, 이름 없는 `screen` 은 그냥 이름이 없을 뿐이다."""
    too_long = "x" * (MAX_SCREEN_NAME_LENGTH + 1)

    name, dropped = usable_name(
        ProposedName(name=too_long), ScreenSelectorScreenRef.model_validate(_screen())
    )

    assert name is None
    assert "longer than" in dropped


def test_a_name_exactly_at_the_cap_is_kept() -> None:
    at_cap = "x" * MAX_SCREEN_NAME_LENGTH

    name, dropped = usable_name(
        ProposedName(name=at_cap), ScreenSelectorScreenRef.model_validate(_screen())
    )

    assert name == at_cap
    assert dropped is None


def test_a_selector_copied_back_as_the_name_is_dropped() -> None:
    """`UIRoot[0]/Title[0]` 은 이름이 아니라 복사다."""
    screen = ScreenSelectorScreenRef.model_validate(_screen())

    name, dropped = usable_name(ProposedName(name="UIRoot[0]/Title[0]"), screen)

    assert name is None
    assert "not a name" in dropped


def test_a_plain_word_that_matches_a_selector_is_still_a_name() -> None:
    """`Shop` 이라는 이름은 옮겨 적은 것이 아니라 사람이 그 화면을 부르는 말이다."""
    screen = ScreenSelectorScreenRef.model_validate(
        _screen(discriminator=[{"selector": "Shop", "active": True}])
    )

    name, dropped = usable_name(ProposedName(name="Shop"), screen)

    assert name == "Shop"
    assert dropped is None


@pytest.mark.parametrize(
    "answered, expected",
    [("  Title screen  ", "Title screen"), ("Title\nscreen", "Title screen")],
    ids=["padded", "two-lines"],
)
def test_a_name_is_one_line_with_its_spacing_collapsed(answered, expected) -> None:
    """한 칸짜리 표시값이다. 개행이 들어가면 이름이 아니라 문단이 된다."""
    name, dropped = usable_name(
        ProposedName(name=answered), ScreenSelectorScreenRef.model_validate(_screen())
    )

    assert name == expected
    assert dropped is None


def test_a_blank_name_is_dropped_and_a_null_one_is_not() -> None:
    """둘은 다른 일이다. 하나는 못 실은 답이고, 하나는 짓지 않기로 한 답이다."""
    screen = ScreenSelectorScreenRef.model_validate(_screen())

    blank, blank_reason = usable_name(ProposedName(name="   "), screen)
    missing, missing_reason = usable_name(ProposedName(name=None), screen)

    assert (blank, missing) == (None, None)
    assert "blank" in blank_reason
    assert missing_reason is None


# --- agent -------------------------------------------------------------------


def _agent(answer) -> ScreenNameAgent:
    def respond(_inputs):
        if isinstance(answer, Exception):
            raise answer
        return answer

    return ScreenNameAgent(structured_factory=lambda model: RunnableLambda(respond))


def test_the_agent_answers_with_the_name_the_model_gave() -> None:
    agent = _agent(ProposedName(name="Title screen", note="The game spells it that way."))

    named = asyncio.run(agent.run(_agent_request(_request()), _CTX))

    assert named.name == "Title screen"
    assert named.note == "The game spells it that way."
    assert named.dropped is None


def test_a_malformed_reply_is_a_failure_and_not_an_invention() -> None:
    agent = _agent(OutputParserException("not json"))

    with pytest.raises(ScreenNameError):
        asyncio.run(agent.run(_agent_request(_request()), _CTX))


def test_a_model_that_will_not_name_the_screen_is_a_complete_answer() -> None:
    agent = _agent(ProposedName(name=None, note="The capture is a black frame."))

    named = asyncio.run(agent.run(_agent_request(_request()), _CTX))

    assert named.name is None
    assert named.dropped is None


def test_the_agent_reports_the_name_it_dropped() -> None:
    agent = _agent(ProposedName(name="y" * (MAX_SCREEN_NAME_LENGTH + 1)))

    named = asyncio.run(agent.run(_agent_request(_request()), _CTX))

    assert named.name is None
    assert "longer than" in named.dropped


def test_a_model_that_cannot_see_is_not_sent_a_capture(monkeypatch) -> None:
    """볼 수 없는 모델에게 그림을 보내면 호출이 통째로 거절된다."""

    async def never(url: str) -> bytes:
        raise AssertionError("a text-only model must not trigger a capture fetch")

    monkeypatch.setattr(capture_module, "download_capture", never)
    monkeypatch.setattr(
        "app.agents.screen_name.agent.get_model_spec",
        lambda model: type("Spec", (), {"supports_vision": False})(),
    )
    payload = _request(screen=_screen(capture="https://example.invalid/shot.jpg"))

    agent = ScreenNameAgent(
        structured_factory=lambda model: RunnableLambda(lambda _inputs: ProposedName())
    )
    request = ScreenNameRequest(
        screen=_agent_request(payload).screen,
        scene=_agent_request(payload).scene,
        model=LLMModel.gpt_chat_latest,
    )

    assert asyncio.run(agent.run(request, _CTX)).name is None


def test_a_capture_that_cannot_be_fetched_does_not_stop_the_naming(monkeypatch) -> None:
    async def gone(url: str) -> bytes:
        raise CaptureFetchError("storage answered 403 — the link may have expired")

    monkeypatch.setattr(capture_module, "download_capture", gone)
    screen = ScreenSelectorScreenRef.model_validate(
        _screen(capture="https://example.invalid/shot.jpg")
    )

    assert asyncio.run(fetch_screen_capture(screen)) == []


def test_a_fetched_capture_carries_the_type_its_bytes_say(monkeypatch) -> None:
    """요청은 mime 을 안 싣는다. 확장자를 믿으면 provider 가 그림을 거절한다."""

    async def png(url: str) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"body"

    monkeypatch.setattr(capture_module, "download_capture", png)
    screen = ScreenSelectorScreenRef.model_validate(
        _screen(capture="https://example.invalid/shot.jpg")
    )

    messages = asyncio.run(fetch_screen_capture(screen))

    assert len(messages) == 1
    assert messages[0].content[1]["image_url"]["url"].startswith("data:image/png;base64,")


# --- QA 런과의 격리 ------------------------------------------------------------


class _StubAgent:
    """정해진 답을 내는 이름 agent. `run` 이 오래 걸리거나 터지게 할 수 있다."""

    def __init__(self, named=None, error: Exception | None = None, delay: float = 0.0):
        self._named = named or ScreenName(name=None, note=None, dropped=None)
        self._error = error
        self._delay = delay
        self.calls = 0

    async def run(self, request, context):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._named


def _channel():
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    return QaRunChannel(qa_try_id=7, send=send), sent


def _name_frames(sent: list[dict]) -> list[dict]:
    return [frame for frame in sent if frame["type"] == MessageType.SCREEN_NAME]


async def _settle() -> None:
    for _ in range(6):
        await asyncio.sleep(0)


def test_the_name_answers_the_request_it_was_asked_about() -> None:
    async def scenario():
        channel, sent = _channel()
        namer = ScreenNamer(
            agent=_StubAgent(
                ScreenName(name="Title screen", note="It says so on the screen.", dropped=None)
            )
        )
        namer.answer_later(channel, _frame(_request("name-77")))
        await _settle()
        return sent

    frames = _name_frames(asyncio.run(scenario()))

    assert len(frames) == 1
    assert frames[0]["correlationId"] == "name-77"
    assert frames[0]["payload"] == {
        "request_id": "name-77",
        "name": "Title screen",
        "note": "It says so on the screen.",
    }


def test_a_failing_naming_answers_with_no_name() -> None:
    async def scenario():
        channel, sent = _channel()
        namer = ScreenNamer(
            agent=_StubAgent(error=ScreenNameError("the model did not answer in shape"))
        )
        namer.answer_later(channel, _frame(_request()))
        await _settle()
        return sent

    frames = _name_frames(asyncio.run(scenario()))

    assert len(frames) == 1
    assert frames[0]["payload"]["name"] is None
    assert "could not be named" in frames[0]["payload"]["note"]


def test_a_naming_past_its_deadline_answers_with_no_name() -> None:
    async def scenario():
        channel, sent = _channel()
        namer = ScreenNamer(agent=_StubAgent(delay=5.0), timeout=0.01)
        namer.answer_later(channel, _frame(_request()))
        await asyncio.sleep(0.05)
        return sent

    frames = _name_frames(asyncio.run(scenario()))

    assert len(frames) == 1
    assert frames[0]["payload"]["name"] is None
    assert "longer than" in frames[0]["payload"]["note"]


def test_a_dropped_name_says_so_on_the_frame() -> None:
    """버림이 조용하면 "왜 이 화면만 이름이 없나" 를 되짚을 자리가 없다."""

    async def scenario():
        channel, sent = _channel()
        namer = ScreenNamer(
            agent=_StubAgent(
                ScreenName(name=None, note=None, dropped="the name is longer than 60 characters")
            )
        )
        namer.answer_later(channel, _frame(_request()))
        await _settle()
        return sent

    frames = _name_frames(asyncio.run(scenario()))

    assert frames[0]["payload"]["name"] is None
    assert "longer than 60 characters" in frames[0]["payload"]["note"]


def test_a_request_with_no_evidence_never_reaches_the_model() -> None:
    """그림도 없고 `discriminator` 도 비었으면 남는 것은 `scene` 이름뿐이다."""

    async def scenario():
        channel, sent = _channel()
        stub = _StubAgent()
        namer = ScreenNamer(agent=stub)
        namer.answer_later(channel, _frame(_request(screen=_screen(discriminator=[]))))
        await _settle()
        return stub, sent

    stub, sent = asyncio.run(scenario())
    frames = _name_frames(sent)

    assert stub.calls == 0
    assert len(frames) == 1
    assert frames[0]["payload"]["name"] is None
    assert "nothing to name it from" in frames[0]["payload"]["note"]


def test_a_request_without_a_request_id_is_not_answered() -> None:
    async def scenario():
        channel, sent = _channel()
        stub = _StubAgent()
        namer = ScreenNamer(agent=stub)
        frame = _frame(_request(""))
        del frame["messageId"]
        namer.answer_later(channel, frame)
        await _settle()
        return stub, sent

    stub, sent = asyncio.run(scenario())

    assert stub.calls == 0
    assert _name_frames(sent) == []


def test_the_envelope_message_id_answers_a_request_that_left_the_field_empty() -> None:
    """계약은 `request_id` 를 쓰지만, 그것이 비면 봉투의 값으로라도 답을 붙인다."""

    async def scenario():
        channel, sent = _channel()
        namer = ScreenNamer(agent=_StubAgent(ScreenName("Shop", None, None)))
        namer.answer_later(channel, _frame(_request(""), message_id="msg-42"))
        await _settle()
        return sent

    frames = _name_frames(asyncio.run(scenario()))

    assert frames[0]["correlationId"] == "msg-42"
    assert frames[0]["payload"]["request_id"] == "msg-42"


def test_a_cancelled_run_is_not_answered_over_a_closing_socket() -> None:
    async def scenario():
        channel, sent = _channel()
        namer = ScreenNamer(agent=_StubAgent())
        channel.on_cancel()
        namer.answer_later(channel, _frame(_request()))
        await _settle()
        return sent

    assert _name_frames(asyncio.run(scenario())) == []


def test_closing_the_namer_stops_what_is_still_running() -> None:
    async def scenario():
        channel, sent = _channel()
        namer = ScreenNamer(agent=_StubAgent(delay=30.0))
        namer.answer_later(channel, _frame(_request()))
        await asyncio.sleep(0)
        namer.close()
        await asyncio.sleep(0)
        return sent, namer

    sent, namer = asyncio.run(scenario())

    assert _name_frames(sent) == []
    assert namer._running == set()


def test_too_many_namings_at_once_are_dropped_rather_than_queued() -> None:
    async def scenario():
        channel, _sent = _channel()
        namer = ScreenNamer(agent=_StubAgent(delay=30.0), max_in_flight=2)
        for index in range(5):
            namer.answer_later(channel, _frame(_request(f"n{index}")))
        await asyncio.sleep(0)
        running = len(namer._running)
        namer.close()
        return running

    assert asyncio.run(scenario()) == 2


# --- frame routing ------------------------------------------------------------


def test_a_slow_naming_does_not_hold_the_inbound_loop() -> None:
    """`deliver` 가 이름을 기다리면 그 사이 `PULSE` 도 `ACTION_RESULT` 도 안 들어온다."""

    async def scenario():
        service = QaExecutionService(InMemoryQaSessionStore())
        channel, sent = _channel()
        service._channels["s"] = channel
        service._namers["s"] = ScreenNamer(agent=_StubAgent(delay=30.0))

        accepted = service.deliver("s", _frame(_request()))
        delivered_next = service.deliver(
            "s", {"type": "PULSE", "payload": {"schema": 2, "reading": 1, "scene": "Field"}}
        )
        service._namers["s"].close()
        return accepted, delivered_next, sent

    accepted, delivered_next, sent = asyncio.run(scenario())

    assert accepted is True
    assert delivered_next is True
    assert _name_frames(sent) == []


def test_the_request_frame_is_accepted_even_with_no_namer_standing() -> None:
    """모르는 타입은 "unsupported inbound frame" 으로 답하고, 저쪽은 그것을 오류로 읽는다."""
    service = QaExecutionService(InMemoryQaSessionStore())
    channel, _sent = _channel()
    service._channels["s"] = channel

    assert service.deliver("s", _frame(_request())) is True


def test_an_unreadable_request_does_not_end_the_run() -> None:
    async def scenario():
        service = QaExecutionService(InMemoryQaSessionStore())
        channel, sent = _channel()
        service._channels["s"] = channel
        stub = _StubAgent()
        service._namers["s"] = ScreenNamer(agent=stub)
        accepted = service.deliver(
            "s", _frame({"request_id": "name-1", "screen": "not an object"})
        )
        await _settle()
        return accepted, stub, sent

    accepted, stub, sent = asyncio.run(scenario())

    assert accepted is True
    assert stub.calls == 0
    assert _name_frames(sent) == []


def test_a_naming_request_does_not_move_the_screen_the_agent_is_standing_on() -> None:
    """방금 굳은 행 하나에 이름을 묻는 frame 이다. 어디에 서 있는가는 `SCREEN_SETTLED` 가 말한다."""
    service = QaExecutionService(InMemoryQaSessionStore())
    channel, _sent = _channel()
    service._channels["s"] = channel
    channel.on_pulse({"payload": {"schema": 2, "reading": 1, "scene": "Field"}})

    service.deliver("s", _frame(_request(screen=_screen(screen_id="99"))))

    assert channel.scene.screen_map_block() is None
