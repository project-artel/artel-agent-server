"""ERROR frame 에 실리는 exceptionType 과 frames (ARTEL telemetry)."""

import asyncio

import pytest

from app.agents.qa.runner import QaRunner
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import ErrorPayload, MessageType, outbound_envelope
from app.qa.failure_site import MAX_FRAMES, failure_site_of
from app.qa.schemas import QaRunScenario, QaScenario
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore

SECRET_MESSAGE = "secret-player-name-4242"


def _scenario() -> QaScenario:
    return QaScenario(title="t", description="d")


def _channel() -> QaRunChannel:
    async def send(_frame: dict) -> None:
        return None

    return QaRunChannel(qa_try_id=7, send=send)


def _recurse(depth: int) -> None:
    if depth == 0:
        raise ValueError(SECRET_MESSAGE)
    _recurse(depth - 1)


def test_failure_site_keeps_class_name_and_only_app_frames() -> None:
    # 이 파일의 frame 은 `tests.` 모듈이라 걸러지고, `app.` 모듈 frame 만 남는다.
    try:
        asyncio.run(asyncio.wait_for(asyncio.sleep(5), timeout=0))
    except TimeoutError as error:
        site = failure_site_of(error)
    assert site.exception_type == "TimeoutError"
    assert all(frame.startswith("app.") for frame in site.frames)


def test_failure_site_has_no_path_no_message_and_at_most_ten_frames() -> None:
    # `app` 모듈 안에서 깊게 쌓인 stack 을 흉내 내려고 globals 의 `__name__` 이 `app.` 인 함수를 만든다.
    namespace = {"__name__": "app.fake.deep"}
    exec(
        "def descend(depth):\n"
        "    if depth == 0:\n"
        f"        raise ValueError({SECRET_MESSAGE!r})\n"
        "    descend(depth - 1)\n",
        namespace,
    )
    with pytest.raises(ValueError) as caught:
        namespace["descend"](30)
    site = failure_site_of(caught.value)

    assert site.exception_type == "ValueError"
    assert len(site.frames) == MAX_FRAMES
    assert all(frame.startswith("app.fake.deep:descend:") for frame in site.frames)
    joined = " ".join(site.frames) + site.exception_type
    assert "/" not in joined
    assert SECRET_MESSAGE not in joined


def test_failure_site_without_app_frames_is_empty_list() -> None:
    with pytest.raises(ValueError) as caught:
        _recurse(3)
    assert failure_site_of(caught.value).frames == []


def test_error_envelope_uses_camel_case_keys() -> None:
    payload = ErrorPayload(
        message="m",
        code="run_incomplete",
        exception_type="TimeoutError",
        frames=["app.agents.qa.runner:run_with_deadline:880"],
    )
    frame = outbound_envelope(MessageType.ERROR, 7, 1, payload)
    assert frame["payload"] == {
        "message": "m",
        "code": "run_incomplete",
        "retryable": False,
        "detail": None,
        "exceptionType": "TimeoutError",
        "frames": ["app.agents.qa.runner:run_with_deadline:880"],
    }


def test_error_envelope_without_exception_leaves_both_null() -> None:
    frame = outbound_envelope(
        MessageType.ERROR, 7, 1, ErrorPayload(message="m", code="x")
    )
    assert frame["payload"]["exceptionType"] is None
    assert frame["payload"]["frames"] is None


def _run_with_patched(monkeypatch: pytest.MonkeyPatch, error: BaseException):
    async def fake_run(self, channel, scenario, state):
        raise error

    monkeypatch.setattr(QaRunner, "run", fake_run)
    return asyncio.run(QaRunner().run_with_deadline(_channel(), _scenario()))


def test_runner_records_failure_site_for_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    state, failure = _run_with_patched(monkeypatch, TimeoutError())
    assert failure is not None and "exceeded" in failure
    assert state.failure_site.exception_type == "TimeoutError"
    assert state.failure_site.frames
    assert state.failure_site.frames[-1].startswith(
        "app.agents.qa.runner:run_with_deadline:"
    )


def test_runner_records_failure_site_for_exception_without_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _failure = _run_with_patched(monkeypatch, RuntimeError(SECRET_MESSAGE))
    assert state.failure_site.exception_type == "RuntimeError"
    assert SECRET_MESSAGE not in " ".join(state.failure_site.frames)


def test_runner_leaves_failure_site_null_for_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, failure = _run_with_patched(monkeypatch, QaCancelled())
    assert failure is None
    assert state.failure_site is None


def test_runner_leaves_failure_site_null_when_agent_stops_without_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def quiet_run(self, channel, scenario, state):
        return None

    monkeypatch.setattr(QaRunner, "run", quiet_run)
    state, failure = asyncio.run(QaRunner().run_with_deadline(_channel(), _scenario()))
    assert failure == "The agent stopped without closing the run."
    assert state.failure_site is None


def test_service_emits_error_frame_with_failure_site(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingRunner:
        async def run_with_deadline(self, channel, scenario):
            return await QaRunner().run_with_deadline(channel, scenario)

    async def fake_run(self, channel, scenario, state):
        raise TimeoutError()

    monkeypatch.setattr(QaRunner, "run", fake_run)

    async def run() -> list[dict]:
        service = QaExecutionService(
            store=InMemoryQaSessionStore(),
            runner_factory=lambda *, config: FailingRunner(),
        )
        session_id, _ = await service.open(
            qa_run_id=9,
            game_instance_id=1,
            scenarios=[QaRunScenario(qa_try_id=101, test_scenario_id=1, scenario=_scenario())],
        )
        sent: list[dict] = []

        async def send(frame: dict) -> None:
            sent.append(frame)

        await service.run(session_id, send)
        return sent

    errors = [frame for frame in asyncio.run(run()) if frame["type"] == "ERROR"]
    assert len(errors) == 1
    payload = errors[0]["payload"]
    assert payload["code"] == "run_incomplete"
    assert payload["exceptionType"] == "TimeoutError"
    assert payload["frames"][-1].startswith("app.agents.qa.runner:run_with_deadline:")
