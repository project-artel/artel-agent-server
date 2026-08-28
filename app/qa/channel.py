"""The bridge between the agent's tools and the WebSocket.

A tool call looks synchronous — ask for the scene, get the scene. The transport
is not: the request goes out on the socket and the answer arrives later as a
separate inbound message. This module holds the futures that make one look like
the other, so `app/agents/qa/tools.py` can simply `await`.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.qa.envelope import (
    ActionPayload,
    ActionResultPayload,
    CapturedImage,
    ChatPayload,
    GameState,
    JsonRpcAction,
    KnowledgeExpandPayload,
    KnowledgeExpandResultPayload,
    KnowledgeSearchPayload,
    KnowledgeSearchResultPayload,
    KnowledgeWriteResultPayload,
    LogCategory,
    LogPayload,
    MessageType,
    ScreenCapturePayload,
    ScreenCreatedPayload,
    ToolCallPayload,
    ToolResultPayload,
    outbound_envelope,
)
from app.qa.pulse import PulseReading
from app.qa.scene import SceneMemory

logger = logging.getLogger(__name__)

# How long a tool waits for the game before giving up on one request.
#
# Returned as a value rather than raised: the server cannot tell a long load from
# a dead game, so the agent decides whether to retry, wait longer, or fail the
# step. The run's overall deadline is the real backstop.
ACTION_TIMEOUT_SECONDS = 30.0

# Ceiling on a tool's requested wait. The agent picks the number, and an
# unbounded one would park the run until the overall deadline killed it.
MAX_SCENE_WAIT_SECONDS = 30.0

# 액션 뒤에 다음 판독을 기다리는 상한.
#
# SDK 는 0.1초마다 읽고 1초마다 배치로 보낸다. 액션 직후에 돌아오면 그 배치가 아직
# 안 나갔을 수 있고, 그러면 도구가 액션 **이전**의 화면을 그린다. 배치 주기보다 조금
# 길게 잡아 그 창을 덮는다.
#
# 새 대기가 아니다 — 종전에는 액션 배치 끝에 `scan_scene` 을 태워 그 왕복을 기다렸다.
# 이 값은 그 왕복을 대신하고, 화면이 실제로 움직였으면 그보다 먼저 풀린다.
READING_WAIT_SECONDS = 1.5

# Ceiling on a wait for the operator. Longer than a scene wait because a person
# has to read the question and type, and shorter than the run's deadline so the
# run still ends by its own account rather than by being killed mid-wait.
MAX_OPERATOR_WAIT_SECONDS = 300.0

# How long a knowledge search waits before the agent is told nobody answered.
#
# Shorter than an action's wait on purpose. An action is the run making progress,
# so it is worth waiting out; a knowledge search only buys context for a judgement
# the agent can also make without it. The round trip is not free on the far side —
# Orchestration embeds the query and then queries pgvector — so this is not as
# tight as a local call, but it must never be the reason a run misses its deadline.
KNOWLEDGE_SEARCH_TIMEOUT_SECONDS = 20.0

# How long a knowledge write waits for its answer.
#
# Much shorter than a search's, and the difference is the point: a search makes
# Orchestration embed a query and hit pgvector, while a write is one database
# statement it has already finished by the time it answers. The number that
# matters is the one paid when NO answer comes — against an Orchestration that
# predates ARTEL-331 every write waits this out, and that cost is per write, per
# run. Silence is not a failure here (see `write_knowledge`), so waiting longer
# would buy nothing but a slower run.
KNOWLEDGE_WRITE_TIMEOUT_SECONDS = 5.0


class QaCancelled(Exception):
    """Raised inside the agent loop when the operator ends the run."""


@dataclass(frozen=True)
class _PendingRequest:
    """One request waiting on its answer.

    Carries the request's own type beside the future because the answer is
    checked against it (ARTEL-367). Orchestration echoes the request type in a
    write's result, and nothing was reading that echo — a mismatch would have
    put the wrong id into `knowledge_seen`, and the next correction would go to
    the wrong entry. Nothing produces a mismatch today; the point is that if
    something started to, no one would find out.
    """

    waiter: "asyncio.Future[Any]"
    request_type: str


@dataclass(frozen=True)
class KnowledgeRequestFailed:
    """Orchestration answered the request with an ERROR frame.

    Distinct from an ordinary answer — a search's empty `results`, a write that
    went through — and from no answer at all. Only this one means the request
    itself was refused or could not run: a bad filter, an embedding model
    mismatch, a database that would not answer, an entry that is not there.

    Shared by the search, the expansion and the writes (ARTEL-332). Orchestration
    reports all three failures the same way, so telling them apart here would be
    this side inventing a distinction the wire does not carry.
    """

    reason: str


class QaRunChannel:
    """Sends frames for one QA session and resolves what comes back."""

    def __init__(
        self,
        qa_try_id: int,
        send: Callable[[dict], Awaitable[None]],
        action_timeout: float = ACTION_TIMEOUT_SECONDS,
        write_timeout: float = KNOWLEDGE_WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self._qa_try_id = qa_try_id
        self._send = send
        self._action_timeout = action_timeout
        # Injectable for the same reason `action_timeout` is: a suite that waited
        # out the real one would pay it on every write it exercises.
        self._write_timeout = write_timeout
        self._sequence = 0
        # 나간 ACTION frame 의 messageId → 그 답을 기다리는 future.
        #
        # 필드 하나가 아니라 map 인 것은 이 채널에 action 을 내는 곳이 둘이 되었기
        # 때문이다(ARTEL-595): 모델이 부르는 도구와, 새 screen 을 찍는 자동 capture. 필드 하나로
        # 들고 있으면 뒤에 나간 것이 앞의 것을 덮어써서, 앞의 action 은 자기 답이 도착해도
        # 못 받고 타임아웃까지 앉아 있다가 "게임이 답하지 않았다"가 된다 — 도구가 방금 누른
        # 버튼이 안 먹혔다고 읽는 자리다.
        self._action_waiters: dict[str, asyncio.Future[ActionResultPayload]] = {}
        # Knowledge requests waiting on an answer, keyed by the messageId
        # Orchestration echoes back as `correlationId`.
        #
        # One map rather than a pair of fields per request kind (ARTEL-332). Those
        # fields existed to stop two frames in flight from resolving each other's
        # future — an expansion answering a search with the wrong content, which
        # nothing about the payload would make obvious. Keying on the correlation
        # rules that out by construction instead, and it scales: writes are the
        # third kind to need it and would have been a third pair of fields.
        #
        # The ACTION waiter above stays separate on purpose. Its timeout, its
        # cancellation and its late-answer rule are the game's, not the knowledge
        # base's, and folding it in here would spread this change across a path
        # this issue does not touch.
        self._pending: dict[str, _PendingRequest] = {}
        # tool_call_id -> 그 호출을 실은 TOOL 프레임의 messageId (ARTEL-609).
        #
        # 답이 correlation 으로 그것을 실어야 화면이 호출과 답을 한 행으로 묶는다. 짝을
        # 맺는 두 값 중 앞의 것은 모델이 지은 id 라, 이 대응을 아는 자리가 여기 말고 없다.
        #
        # 답을 기다리는 것이 아니라서 `_pending` 과 섞지 않는다. 저쪽은 future 를 들고
        # 타임아웃과 취소 규칙이 붙지만, 이쪽은 기억해 두었다가 꺼내 쓰는 것이 전부다.
        self._tool_frames: dict[str, str] = {}
        self.scene = SceneMemory()
        self.cancelled = False
        # Operator messages that arrived since the agent last looked. Delivered
        # by appending to the next tool result rather than interrupting the graph:
        # simpler, and it guarantees the words reach the next decision.
        self._operator_messages: list[str] = []
        # Set exactly while that list has something in it, so a tool can wait on
        # the operator instead of only picking messages up in passing.
        self._operator_arrived = asyncio.Event()
        # 판독이 하나 도착할 때마다 세운다. 액션 뒤에 다음 배치를 기다리는 쪽을 깨우는
        # 데만 쓰고, "몇 개 왔나" 는 `pulse.readings` 가 센다 — 이벤트는 놓칠 수 있고
        # 개수는 놓칠 수 없다.
        self._reading_arrived = asyncio.Event()
        # Everything the operator has said this run, in order — the durable record
        # beside the delivery queue above, which is emptied as soon as a tool picks
        # it up. Once delivered, an instruction exists only inside one tool result's
        # text, and compaction replaces exactly that text; "it applies from now on"
        # then stops being true halfway through a run. This list is what
        # `render_progress_ledger` restates afterwards.
        #
        # Recorded in `on_chat` rather than at any tool, because that is the single
        # funnel every operator message passes through: a tool added later cannot
        # forget to do it.
        #
        # Unbounded, and bounded in practice by the run's own deadline: an operator
        # cannot type enough in 600 seconds for the restated block to matter.
        self.operator_instructions: list[str] = []
        # 새 screen 을 찍는 중인 백그라운드 task 들(ARTEL-595). 참조를 들고 있는 것은
        # asyncio 가 아무도 안 붙든 task 를 도중에 거둬 갈 수 있기 때문이고, 시나리오가
        # 끝날 때 `close` 가 이 집합을 끊는다.
        self._capture_tasks: set[asyncio.Task] = set()
        # 자동 capture 끼리 한 줄로 세운다. screen 이 빠르게 갈리면 `SCREEN_CREATED` 가 연달아
        # 오는데, 그때마다 배치를 동시에 밀면 게임 쪽에 몇 개가 떠 있는지 아무도 모른다.
        # 도구의 action 과는 나누지 않는다 — 그쪽을 이 락 뒤로 넣으면 지도의 곁일이 런의
        # 판단을 기다리게 만든다.
        self._capture_lock = asyncio.Lock()
        # 게임이 이 action 자체를 모른다고 답했다. 그 뒤로는 새 screen 마다 같은 거절을 사러
        # 가지 않는다 — 도구가 모델에게 "이 런에서 다시 찍지 마라" 고 말하는 것과 같은
        # 판단이다. 무응답에는 세우지 않는다. 그것은 이번에 늦었다는 뜻이지 못 한다는
        # 뜻이 아니다.
        self._capture_unsupported = False

    @property
    def qa_try_id(self) -> int:
        """Stable correlation value exposed for trace metadata."""
        return self._qa_try_id

    # --- outbound -------------------------------------------------------------

    def _frame(self, message_type: MessageType, payload, correlation_id: str | None = None) -> dict:
        self._sequence += 1
        return outbound_envelope(
            message_type,
            self._qa_try_id,
            self._sequence,
            payload,
            correlation_id=correlation_id,
        )

    async def note(self, message: str, category: LogCategory, step: int | None = None) -> None:
        """Put a line on the timeline. Nothing waits for it."""
        await self._send(
            self._frame(MessageType.LOG, LogPayload(category=category, message=message, step=step))
        )

    async def say(self, message: str, step: int | None = None) -> None:
        await self._send(self._frame(MessageType.CHAT, ChatPayload(message=message, step=step)))

    async def tool_call(
        self, tool: str, tool_call_id: str, args: dict[str, Any], step: int | None = None
    ) -> None:
        """모델이 부른 tool 하나를 타임라인에 남긴다. 아무것도 기다리지 않는다.

        그 프레임의 messageId 를 `tool_call_id` 로 기억해 둔다. 답이 그것을 correlation
        으로 실어야 화면이 호출과 답을 짝지을 수 있고, 짝을 맺는 두 값 중 하나는 모델이
        지은 id 라 여기 말고는 대응을 아는 자리가 없다.
        """
        frame = self._frame(
            MessageType.TOOL,
            ToolCallPayload(
                message=tool, tool=tool, tool_call_id=tool_call_id, args=args, step=step
            ),
        )
        self._tool_frames[tool_call_id] = frame["messageId"]
        await self._send(frame)

    async def tool_result(
        self, tool: str, tool_call_id: str, content: str, step: int | None = None
    ) -> None:
        """그 tool 이 돌려준 것을 타임라인에 남긴다.

        대응은 꺼내면서 지운다. 답이 오지 않는 호출은 런이 끝날 때까지 한 칸을 차지하지만,
        런 하나의 tool 호출 수만큼이 상한이라 자라도 문제가 되지 않는다.

        correlation 이 없을 수 있다. 호출 프레임을 못 본 답 — 컴팩션 뒤에 남은 꼬리가
        그렇다 — 은 짝 없이 제 줄로 뜬다. 지어낸 correlation 을 다는 것보다 낫다.
        """
        await self._send(
            self._frame(
                MessageType.TOOL_RESULT,
                ToolResultPayload(
                    message=tool, tool=tool, tool_call_id=tool_call_id, content=content, step=step
                ),
                self._tool_frames.pop(tool_call_id, None),
            )
        )

    async def emit(self, message_type: MessageType, payload, correlation_id: str | None = None) -> None:
        await self._send(self._frame(message_type, payload, correlation_id))

    async def look(self, after_seconds: float) -> bool:
        """게임의 지금 화면을 본다. 볼 것이 있으면 참.

        **아무것도 보내지 않는다.** 판독은 물어서 오는 것이 아니라 게임이 도는 동안 계속
        도착하는 관측이고, `GAME_STATE` 도 켜져 있으면 `PollSceneState` 가 스스로 올린다.
        두 채널 다 묻지 않는다 — 그래서 여기서 할 일은 이미 쥐고 있는 그림이 쓸 만한지
        가리는 것뿐이다.

        여기서 `scan_scene` 이 사라졌다(ARTEL-516). 그 액션의 유일한 일이 `GAME_STATE` 를
        만드는 것인데, 채널이 꺼진 빌드에서는 오류를 답하고 켜진 빌드에서는 폴러가 이미
        같은 것을 올리고 있으므로 어느 쪽에서도 하는 일이 없다.

        아무것도 들은 적이 없으면 한 배치만 기다려 본다. 런이 막 시작한 창이 그 모양이다 —
        `start_readings` 는 나갔고 첫 배치는 아직이다. 그래도 안 오면 거짓이고, 부르는 쪽이
        더 기다릴지 스텝을 실패로 볼지 정한다.
        """
        self._raise_if_cancelled()
        if after_seconds > 0:
            # Safe to sleep: the run is its own asyncio task (see
            # app/api/qa_sessions.py), so this holds up nothing but this tool.
            await asyncio.sleep(min(after_seconds, MAX_SCENE_WAIT_SECONDS))

        if self._something_to_show():
            return True

        await self._await_reading(self.scene.pulse.readings, READING_WAIT_SECONDS)
        return self._something_to_show()

    def _something_to_show(self) -> bool:
        """볼 것이 있나. 두 채널 중 하나라도 무언가 실어 왔으면 참.

        Frames, not observations: `updates` restarts at 1 on a scene change, so
        comparing that would report a transition as the game having stayed silent.

        `GAME_STATE` 를 여전히 세는 것은 그 채널에 여지를 남기는 것이 아니라 **도착한 것을
        없다고 하지 않는 것**이다. ARTEL-513 의 스위치를 되돌리면 폴러가 프레임을 올리고,
        그때 그것을 못 본 척하면 이 도구가 거짓말하던 자리로 되돌아간다.
        """
        return self.scene.pulse.seen or self.scene.frames > 0

    async def act_and_look(
        self, actions: list[JsonRpcAction], message: str, step: int | None = None
    ) -> tuple[ActionResultPayload | None, bool]:
        """액션을 돌리고, 그것이 만든 화면을 읽는다.

        배치에 `scan_scene` 을 태우지 않는다. 대신 **다음 판독을 기다린다.** 판독은 1초
        배치라 `ACTION_RESULT` 가 돌아온 시점에는 액션의 결과가 아직 안 나갔을 수 있고,
        그대로 그리면 도구가 액션 이전의 화면을 보여 준다. `scan_scene` 이 같은 배치 끝에
        탔던 이유가 정확히 그것이었으므로 — 따로 물으면 SDK 의 메시지 핸들러가 곧장 답해
        클릭의 커서 이동이 끝나기 전 화면이 돌아왔다 — 이 기다림이 그 왕복을 대신한다.

        기다림이 빈손으로 끝나는 것은 정상이다. SDK 는 움직인 것이 없으면 판독을 아예 내지
        않는다(`Pulse.Take` 의 `settled`). 그래서 거짓은 "게임이 죽었다"가 아니라 "화면이
        그대로다"를 뜻하고, 부르는 쪽이 그 둘을 갈라 읽는다.

        Returns the results and whether anything fresh arrived.
        """
        before_frames = self.scene.frames
        before_readings = self.scene.pulse.readings

        result = await self.dispatch_actions(actions, message, step)
        await self._await_reading(before_readings, READING_WAIT_SECONDS, getattr(result, "frame", None))

        arrived = (
            self.scene.frames > before_frames
            or self.scene.pulse.readings > before_readings
        )
        return result, arrived

    async def _await_reading(
        self, after: int, timeout: float, frame: int | None = None
    ) -> bool:
        """액션 이후의 판독이 올 때까지 기다린다. 최대 `timeout` 초.

        도착 이벤트가 아니라 **개수**를 본다. 이벤트만 보면 기다리기 시작하기 전에 이미
        도착한 판독을 못 본 것으로 세는데, 액션의 결과를 실은 배치가 `ACTION_RESULT` 보다
        먼저 도착하는 일이 실제로 있다 — SDK 가 0.1초마다 읽으므로 그쪽이 더 빠를 수 있다.

        `frame` 이 있으면 **개수만으로는 부족하다.** 읽기(0.1초)와 전달(1초)이 두 속도라,
        액션 직후 도착하는 첫 배치는 액션 **전에** 잡힌 것일 수 있다. 그것을 결과로 치면
        도구가 액션 이전 화면을 보여 주고, 에이전트는 안 먹혔다고 읽어 같은 것을 또 누른다.
        그래서 그 프레임보다 뒤에 잡힌 판독이 올 때까지 기다린다(ARTEL-621).

        `frame` 이 없으면 종전대로 개수만 본다 — 그 필드를 모르는 옛 SDK 다.
        """
        def arrived() -> bool:
            if self.scene.pulse.readings <= after:
                return False
            if frame is None:
                return True
            # 판독의 frame 은 그것이 잡힌 프레임이다. 액션이 끝난 뒤라야 결과다.
            latest = self.scene.pulse.frame
            return latest is None or latest > frame

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not arrived():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            # `on_pulse` 는 같은 이벤트 루프의 동기 콜백이라, 이 clear 와 위의 검사
            # 사이에 판독이 끼어들 수 없다.
            self._reading_arrived.clear()
            try:
                await asyncio.wait_for(self._reading_arrived.wait(), remaining)
            except asyncio.TimeoutError:
                return False
        return True

    async def dispatch_actions(
        self, actions: list[JsonRpcAction], message: str, step: int | None = None
    ) -> ActionResultPayload | None:
        """Run actions on the game. `None` means no result came back."""
        self._raise_if_cancelled()
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[ActionResultPayload] = loop.create_future()

        frame = self._frame(
            MessageType.ACTION, ActionPayload(message=message, step=step, actions=actions)
        )
        message_id = frame["messageId"]
        self._action_waiters[message_id] = waiter
        await self._send(frame)
        try:
            return await asyncio.wait_for(waiter, timeout=self._action_timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._action_waiters.pop(message_id, None)

    async def search_knowledge(
        self, query: str, tag: str | None, limit: int, step: int | None = None
    ) -> KnowledgeSearchResultPayload | KnowledgeRequestFailed | None:
        """Ask the project's knowledge base a question. `None` means no answer came.

        Three outcomes, kept apart as three types, because the agent should act
        differently on each: a payload (which may carry an empty `results`, and
        that is a real answer), a refusal, or silence. Returned rather than
        raised, for the same reason `look` returns a boolean — a knowledge lookup
        is a side errand to the verdict, and none of these is a reason to stop
        the run.

        Nothing about the game is touched here: no action goes out, so no scene
        comes back, so no caller ends up with a scene view to append.
        """
        return await self._request(
            MessageType.KNOWLEDGE_SEARCH,
            KnowledgeSearchPayload(query=query, tag=tag, limit=limit, step=step),
            KNOWLEDGE_SEARCH_TIMEOUT_SECONDS,
        )

    async def expand_knowledge(
        self, knowledge_id: str, depth: int, include_similar: bool, step: int | None = None
    ) -> KnowledgeExpandResultPayload | KnowledgeRequestFailed | None:
        """Walk the knowledge graph out from one entry. `None` means no answer came.

        Same three outcomes as `search_knowledge`, and the same argument for
        returning rather than raising. The timeout is the search's, because the
        work on the far side is the same shape — a couple of indexed queries.
        """
        return await self._request(
            MessageType.KNOWLEDGE_EXPAND,
            KnowledgeExpandPayload(
                knowledge_id=knowledge_id,
                depth=depth,
                include_similar=include_similar,
                step=step,
            ),
            KNOWLEDGE_SEARCH_TIMEOUT_SECONDS,
        )

    async def write_knowledge(
        self, message_type: MessageType, payload
    ) -> KnowledgeWriteResultPayload | KnowledgeRequestFailed | None:
        """Write to the project's knowledge base, and wait for the verdict.

        Three outcomes, as with a search, but the third one means something
        different here and getting it wrong is expensive:

        - a payload — Orchestration stored it, and for a create the payload
          carries the new entry's id
        - `KnowledgeRequestFailed` — it refused. The write did NOT happen
        - `None` — **no answer came, which is not the same as no write.**
          Orchestration performs the write and skips the reply when the run has
          no Agent session, drops frames it cannot route without answering them,
          and an Orchestration older than ARTEL-331 never answers at all. Every
          one of those looks identical from here.

        A caller that reports `None` as a failure makes the model write the same
        fact again, which is the duplicate this whole contract exists to stop.
        `app/agents/qa/tools.py` says "sent, not confirmed, do not send it again"
        for exactly that reason.
        """
        return await self._request(message_type, payload, self._write_timeout)

    async def _request(self, message_type: MessageType, payload, timeout: float) -> Any:
        """Send one frame and park until its answer arrives. `None` means none did.

        The answer's type follows the request's — callers annotate what they
        expect, because what lands in the future is whatever the matching
        `on_*` handler validated. That handler is chosen by the inbound frame's
        type, so a mismatch here would mean Orchestration answered one request
        with another request's frame.

        The entry is removed in `finally`, so a late answer to a request already
        abandoned at its timeout finds nothing to resolve and is dropped.
        """
        self._raise_if_cancelled()
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[Any] = loop.create_future()
        frame = self._frame(message_type, payload)
        message_id = frame["messageId"]
        self._pending[message_id] = _PendingRequest(waiter, str(message_type))
        try:
            await self._send(frame)
            return await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(message_id, None)

    # --- inbound --------------------------------------------------------------

    def on_game_state(self, raw: dict) -> None:
        # 풀어 줄 future 가 없다. 에이전트가 화면을 묻지 않기 때문이다(ARTEL-516) —
        # 여기 오는 프레임은 전부 게임이 스스로 올린 것이고, `PollSceneState` 가 그
        # 출처다. 도구를 풀어 주는 것은 액션의 ACTION_RESULT 다.
        #
        # 워터마크가 있어서 늦게 도착해도 다음 렌더가 그 변화를 여전히 보고한다.
        self.scene.apply(GameState.model_validate(raw.get("payload") or {}))

    def on_pulse(self, raw: dict) -> None:
        """판독 하나를 씬 메모리 위에 얹는다.

        `on_game_state` 와 달리 풀어 줄 future 가 없다. 판독은 무엇을 물어서 오는 것이
        아니라 게임이 도는 동안 계속 도착하는 관측이고, 도구가 그것을 기다리지 않는다.
        """
        self.scene.pulse.apply(PulseReading.model_validate(raw.get("payload") or {}))
        # 액션 뒤에 다음 배치를 기다리는 쪽을 깨운다(ARTEL-516).
        self._reading_arrived.set()

    def on_screen_created(self, raw: dict) -> None:
        """orchestration 이 처음 보는 screen 을 하나 만들었다(ARTEL-595). 그 자리에서 찍는다.

        **여기서 기다리지 않는다.** 이 함수는 소켓을 읽는 쪽의 동기 콜백이라, capture 왕복을
        여기서 기다리면 그동안 pulse 도 action 결과도 채널에 못 들어온다. 백그라운드 task 로
        띄우고 곧바로 돌려준다.

        답을 correlation 으로 묶기 위해 이 frame 의 messageId 를 들고 간다. screen id 도 함께
        싣지만, correlation 이 있는 쪽이 중계 한 겹을 더 견딘다.
        """
        payload = ScreenCreatedPayload.model_validate(raw.get("payload") or {})
        if self.cancelled or self._capture_unsupported:
            return
        correlation = raw.get("messageId")
        task = asyncio.create_task(
            self.capture_new_screen(
                payload, correlation if isinstance(correlation, str) else None
            )
        )
        self._capture_tasks.add(task)
        task.add_done_callback(self._capture_tasks.discard)

    def on_action_result(self, raw: dict) -> None:
        # 검증이 라우팅보다 먼저다. 읽을 수 없는 frame 은 기다리는 것이 있든 없든
        # `deliver` 까지 올라가 "이 frame 은 못 읽었다" 로 답해야 한다 — 그 판단을 하는
        # 자리가 여기서 나가는 ValidationError 하나뿐이다.
        payload = ActionResultPayload.model_validate(raw.get("payload") or {})
        waiter = self._action_waiter_for(raw.get("correlationId"))
        if waiter is not None and not waiter.done():
            waiter.set_result(payload)

    def _action_waiter_for(self, correlation: Any) -> asyncio.Future | None:
        """이 답이 풀어 줄 future. 없으면 `None`.

        Orchestration 은 ACTION 의 messageId 를 답의 correlation 으로 되돌려 준다. 그것으로
        찾지 못하면 이미 타임아웃으로 버린 요청의 늦은 답이므로 버린다.

        correlation 이 없는 답은 그 필드를 안 싣는 옛 orchestration 이다. 기다리는 것이
        **하나뿐일 때만** 그것으로 친다 — 둘이 떠 있는데 짐작으로 고르면 도구가 자동 capture 의
        답을 자기 action 의 결과로 읽는다.
        """
        if isinstance(correlation, str):
            return self._action_waiters.get(correlation)
        if len(self._action_waiters) == 1:
            return next(iter(self._action_waiters.values()))
        return None

    def on_knowledge_search_result(self, raw: dict) -> None:
        self._resolve(raw, KnowledgeSearchResultPayload.model_validate(raw.get("payload") or {}))

    def on_knowledge_expand_result(self, raw: dict) -> None:
        self._resolve(raw, KnowledgeExpandResultPayload.model_validate(raw.get("payload") or {}))

    def on_knowledge_write_result(self, raw: dict) -> None:
        """A write's answer, checked against what was asked (ARTEL-367).

        Orchestration echoes the request type in the payload. Matching it costs a
        comparison and buys the one thing correlation alone cannot give: if the two
        ever disagree, the answer is dropped instead of being believed. Believing it
        would file the wrong id under `knowledge_seen`, and every correction after
        that would land on the wrong entry — silently, because the id is real.

        Dropped rather than surfaced as a failure. A mismatch is a protocol fault on
        the far side, not something the run can act on, and turning it into a tool
        failure would make the model rewrite a fact that may well have been stored.
        The tool times out into "cannot confirm", which is what the situation is.
        """
        payload = KnowledgeWriteResultPayload.model_validate(raw.get("payload") or {})
        correlation = raw.get("correlationId")
        pending = self._pending.get(correlation) if isinstance(correlation, str) else None
        if pending is not None and payload.type and payload.type != pending.request_type:
            logger.warning(
                "[QA] a %s answer arrived for a %s request (correlation %s); dropped",
                payload.type,
                pending.request_type,
                correlation,
            )
            return
        self._resolve(raw, payload)

    def on_error(self, raw: dict) -> bool:
        """An inbound ERROR. True when it was the answer to something we asked.

        Orchestration replies to a refused request with an ERROR carrying that
        request's correlation id, so this is how a search, an expansion or a
        write that could not run reaches the tool waiting on it. An uncorrelated
        ERROR is somebody reporting a problem rather than answering, and there is
        nothing to release — the caller decides what to do with that.

        The payload is read field by field rather than through `ErrorPayload`:
        Orchestration's failure frame carries only `message`, and that model
        requires a `code`. Validating here would drop exactly the frame that
        exists to unblock a waiting tool.
        """
        payload = raw.get("payload") or {}
        reason = payload.get("message") if isinstance(payload, dict) else None
        return self._resolve(raw, KnowledgeRequestFailed(reason=str(reason or "no reason given")))

    def _resolve(self, raw: dict, answer: Any) -> bool:
        """Hand `answer` to whatever is waiting on this frame's correlation.

        False when nothing was: an answer to a request already abandoned at its
        timeout, or an unsolicited frame. Both are dropped rather than applied to
        whatever happens to be waiting now — that mix-up is what the correlation
        key exists to prevent.
        """
        correlation = raw.get("correlationId")
        pending = self._pending.get(correlation) if isinstance(correlation, str) else None
        if pending is None or pending.waiter.done():
            return False
        pending.waiter.set_result(answer)
        return True

    def on_chat(self, raw: dict) -> None:
        payload = ChatPayload.model_validate(raw.get("payload") or {})
        self._operator_messages.append(payload.message)
        self.operator_instructions.append(payload.message)
        self._operator_arrived.set()

    def on_cancel(self) -> None:
        self.cancelled = True
        for waiter in self._action_waiters.values():
            if not waiter.done():
                waiter.cancel()
        # Same for any knowledge request in flight: left alone it would hold the
        # run open for the rest of its timeout after the operator already ended it.
        #
        # Sweeping the map covers all three kinds. Before ARTEL-332 this cancelled
        # the search only, so an expansion the operator interrupted still sat out
        # its full 20 seconds — a bug the per-kind fields made easy to miss and the
        # map makes impossible to have.
        for pending in self._pending.values():
            if not pending.waiter.done():
                pending.waiter.cancel()
        # A tool parked on the operator has no action to cancel, so wake it and
        # let it find the cancellation itself.
        self._operator_arrived.set()
        self.close()

    # --- 새 screen capture (ARTEL-595) ---------------------------------------------

    async def capture_new_screen(
        self, screen: ScreenCreatedPayload, correlation_id: str | None = None
    ) -> None:
        """새로 생긴 screen 하나를 찍어 그 주소를 orchestration 에 돌려준다(ARTEL-595).

        **도구가 아니다.** 모델이 부르지 않고 `arch.max_captures_per_run` 도 쓰지 않는다 —
        어느 screen 에 그림이 붙는지가 런의 판단에 좌우되면, 같은 빌드의 지도가 런마다 다른
        screen 에만 그림을 갖는다. 이 경로가 `QaRunState` 를 손에 쥐지 않는 것으로 그 예산과의
        관계가 구조적으로 끊긴다.

        **찍은 그림을 모델에게 보이지 않는다.** 이것은 지도가 screen 행에 붙일 그림이지 스텝의
        근거가 아니다. 컨텍스트에 밀어 넣으면 모델이 부르지도 않은 이미지가 매 턴 값을
        치르고, 지도의 사정이 런의 판단을 바꾼다.

        **어떤 실패도 밖으로 내지 않는다.** screen 행은 orchestration 이 이미 만들었고, 그림
        없는 screen 행이 screen 없는 지도보다 낫다.
        """
        try:
            async with self._capture_lock:
                if self.cancelled or self._capture_unsupported:
                    return
                await self._capture_new_screen(screen, correlation_id)
        except asyncio.CancelledError:
            # 협조적 취소는 그대로 올린다. `close` 가 끊은 것이 이 자리다.
            raise
        except QaCancelled:
            # 운영자가 런을 끊었다. 곁일이 낼 소리가 아니다.
            return
        except Exception:
            logger.warning(
                "[QA] 새 screen %s 의 capture 가 실패했다. screen 은 그대로 남는다",
                screen.screenId,
                exc_info=True,
            )

    async def _capture_new_screen(
        self, screen: ScreenCreatedPayload, correlation_id: str | None
    ) -> None:
        where = f"screen {screen.screenId}"
        if screen.sceneName:
            where = f"{where} in {screen.sceneName}"

        result = await self.dispatch_actions(
            [JsonRpcAction(id=1, method="capture_screen", params=[])],
            f"Capturing new {where}",
        )
        if result is None or not result.results:
            await self.note(
                f"The game did not answer the capture of new {where}; "
                "it stays on the map without a picture.",
                LogCategory.SYSTEM,
            )
            return

        item = result.results[0]
        if not item.success:
            # 이 action 을 모르는 빌드는 새 screen 마다 같은 거절을 답한다. 한 번 듣고 그만둔다 —
            # 도구가 모델에게 "이 런에서 다시 찍지 마라" 고 말하는 것과 같은 판단이다.
            self._capture_unsupported = True
            await self.note(
                f"New {where} could not be captured — {item.error or 'no reason given'}. "
                "No screen will be captured for the rest of this run.",
                LogCategory.SYSTEM,
            )
            return

        image = CapturedImage.model_validate(item.returnValue or {})
        if not image.url or not image.captureId:
            # 주소도 id 도 없으면 묶을 것이 없다. 반쪽짜리 frame 을 보내는 것보다 낫다.
            await self.note(
                f"The game reported a capture of new {where} with no image to bind.",
                LogCategory.SYSTEM,
            )
            return

        await self.emit(
            MessageType.SCREEN_CAPTURE,
            ScreenCapturePayload(
                message=f"Captured new {where}",
                screenId=screen.screenId,
                captureId=image.captureId,
                url=image.url,
                mimeType=image.mimeType,
            ),
            correlation_id,
        )
        # 타임라인에도 남긴다. 지도에 그림이 안 붙었을 때 사람이 "찍기는 했나" 를 볼 자리다.
        await self.note(f"Captured new {where}: {image.url}", LogCategory.OBSERVATION)

    def close(self) -> None:
        """런이 이 채널을 놓을 때 남은 곁일을 끊는다.

        끝난 try 로 frame 을 보내면 orchestration 이 그것을 거절하고, 그 거절이 소켓을 닫아
        런 전체를 실패로 만든다. 자동 capture 는 백그라운드 task 라 런이 끝나도 스스로 멈추지
        않으므로, 그 끝을 여기서 말해 준다.
        """
        # 참조는 그대로 둔다. task 를 취소만 하고 놓아 버리면 아직 시작도 못 한 것이
        # 그대로 수거돼 "Task was destroyed but it is pending" 이 뜬다. 각자 끝나면서
        # done callback 이 이 집합에서 자기를 지운다.
        for task in list(self._capture_tasks):
            task.cancel()

    # --- operator ------------------------------------------------------------

    def drain_operator_messages(self) -> list[str]:
        messages = self._operator_messages
        self._operator_messages = []
        self._operator_arrived.clear()
        return messages

    async def wait_for_operator(self, timeout_seconds: float) -> list[str]:
        """Park until the operator says something. Empty means nobody did.

        Returns rather than raises on a timeout, for the same reason `look` does:
        silence is a thing the agent decides about, not an error.
        """
        self._raise_if_cancelled()
        try:
            await asyncio.wait_for(
                self._operator_arrived.wait(),
                timeout=bounded_operator_wait(timeout_seconds),
            )
        except asyncio.TimeoutError:
            return []
        # Cancellation wakes this too, and it must not be read as an answer.
        self._raise_if_cancelled()
        return self.drain_operator_messages()

    def _raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise QaCancelled()


def bounded_operator_wait(timeout_seconds: float) -> float:
    """The wait the channel will actually make, which is what a tool must quote.

    Shared so the tool's "nobody answered within Ns" cannot name a number the
    channel never waited.
    """
    return min(max(timeout_seconds, 0.0), MAX_OPERATOR_WAIT_SECONDS)


def with_operator_messages(result: str, messages: list[str]) -> str:
    """Append what the operator said to a tool result.

    The words have to land inside the loop, and the tool result is the only place
    the model is guaranteed to read before its next decision.
    """
    if not messages:
        return result
    lines = "\n".join(f"  - {message}" for message in messages)
    return f"{result}\n\nThe operator said, and it applies from now on:\n{lines}"
