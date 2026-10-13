"""macro 정의 하나를 batch 여러 개로 쪼개 `run` 을 반복 호출한다.

`require()` 는 화면을 다시 읽어야 하므로 batch 하나로 한 번에 보낼 수 없다. 그리고
**같은 batch 안의 action 은 실패한 action 에서 멈추지 않고 끝까지 간다**
(`ArtelManager.ExecuteActionRequest` 의 `foreach`). 그래서 action statement 하나를
batch 하나에 대응시켜야 "이 statement 는 안 나갔다" 는 말이 참이 된다 — 한 batch 에
둘을 담으면 뒤엣것을 보낸 뒤에야 실패를 알게 되어 `pending` 이 거짓말이 된다.

`require` 가 실패했을 때 이미 게임에 나간 action 은 되돌릴 수 없으므로, 어디까지 갔는지를
정확히 보고한다. 그것이 `applied`·`pending`·`skipped` 세 목록이다.

- `applied` — **게임에 나간** action statement. `require`·대입·`if`·`flag`·
  `ask_verdict` 는 게임에 아무것도 안 보내므로 여기 절대 안 든다. 예외 없음.
- `pending` — 실패 지점 뒤에 있어 아직 안 나간 것.
- `skipped` — `if` 의 조건이 거짓으로 판정되어 가지 않은 분기 안의 것. 실패해서 못
  나간 것과 조건이 거짓이라 원래 안 가는 것은 다른 얘기이고, 구분하지 않으면 읽는 쪽이
  매번 추측한다. 실패 지점 뒤에 있어 `if` 가 아직 판정되지 않은 분기의 statement 는
  `skipped` 가 아니라 `pending` 이다.

**저자가 적은 실패만 잡지 않는다.** `require` 는 저자가 미리 상상한 실패만 잡는다.
그 밖에 runner 가 스스로 멈추는 자리가 넷 더 있고, 넷 다 **이미 손에 있는 값**으로
판단해 model 턴을 안 쓴다 — macro 의 존재 이유가 그 턴을 안 쓰는 것이다.

- 누름이 닿은 것이 하나도 없다 → `REQUIRE_FAILED`. click 계열 뒤의 암묵적 `require` 다
- 사람이 마우스를 쥐고 있다 → `ACTION_REJECTED`. 게임도 macro 도 아닌 환경 문제다
- 화면이 연달아 `STILL_SCREENS_BEFORE_STOP` 번 그대로다 → `SCREEN_UNCHANGED`
- operator 가 말을 걸었다 → `OPERATOR_INTERRUPTED`

판단에 쓰는 값은 `MacroHost.run` 이 내는 `ActionOutcome` 이다. 문장이 아니라 **문장이
되기 전의 데이터**를 받는다.

**상한의 반은 여기서 센다.** 호출 깊이 3 과 적힌 길이 128 은 `register_macro` 가 저장
시점에 판정한다. 반복(ARTEL-948)은 몇 번 돌지 저장 때 모르므로, 실행된 양은 여기서
센다 — 반복 하나당 `MAX_LOOP_PASSES` 회, macro 전체에서 `MAX_EXECUTED_STATEMENTS` 개.
넘으면 `LOOP_LIMIT` 로 멈춘다. 앞서 이 자리는 "저장 때 잡는 것이 언제나 낫다" 였는데,
반복이 들어오면 그 근거가 성립하지 않는다 — 셀 수가 없다. 그 대가로 상한에 걸린
macro 는 이미 나간 action 을 되돌리지 못한다.

**`checkpoint` 에서 턴을 돌려준다(ARTEL-949).** runner 는 그 자리에서 `MacroHost.checkpoint`
를 기다린다. host 는 거기까지의 결과를 agent 에게 보내고, agent 가 잇겠다고 할 때까지
돌아오지 않는다. 묶인 이름·호출 사슬·반복의 위치가 전부 이 coroutine 안에 그대로 살아
있으므로, 이어 돌면 처음이 아니라 그 줄부터 돈다. 멈춰 있는 동안 아무것도 안 보내므로
게임을 모는 쪽은 언제나 하나다.

**세는 것만 편다, 실행은 안 편다.** helper 호출은 펼치지 않고 호출로 돈다. `step N of M`
은 `def` 마다 1부터 세고, payload 가 어느 `def` 의 몇 번인지와 호출 사슬을 함께 싣는다.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agents.qa.macro.binding import (
    FoundObject,
    LateSelector,
    MacroMemories,
    MacroScope,
    evaluate,
    read_to_bind,
    resolve_find,
    resolve_find_all,
    resolve_target,
)
from app.agents.qa.macro.errors import (
    ACTION_REJECTED,
    OPERATOR_INTERRUPTED,
    REQUIRE_FAILED,
    SCENE_MISMATCH,
    SCREEN_UNCHANGED,
    LOOP_LIMIT,
    MacroFailure,
)
from app.agents.qa.macro.grammar import (
    MAX_EXECUTED_STATEMENTS,
    MAX_LOOP_PASSES,
    TOOLS_BY_NAME,
    ToolParameter,
    ToolSpec,
)
from app.agents.qa.macro.model import (
    MacroActionStatement,
    MacroAskVerdictStatement,
    MacroAssignStatement,
    MacroCheckpointStatement,
    MacroDefinition,
    MacroFindValue,
    MacroFlagStatement,
    MacroForStatement,
    MacroFunction,
    MacroHelperCallStatement,
    MacroIfStatement,
    MacroLiteralValue,
    MacroReadValue,
    MacroParameter,
    MacroRequireStatement,
    MacroSelectorValue,
    MacroStatement,
    MacroWhileStatement,
)
from app.qa.acting import ActionOutcome, PressLanding, ScreenChange
from app.qa.envelope import JsonRpcAction

# 화면이 연달아 이만큼 그대로면 멈춘다.
#
# 한 번은 정상이다. `ToolContext.act` 는 action 마다 다음 `pulse` 를 1.5초까지 기다리고
# (`READING_WAIT_SECONDS`), SDK 는 움직인 것이 없으면 `pulse` 를 아예 안 낸다. 그래서
# "그대로" 는 1.5초 동안 아무것도 안 바뀌었다는 말인데, 그렇게 되는 정상적인 action 이
# 여럿 있다 — `hold_mouse_button`·`hold_key`·`move_pointer`·`set_input_axis` 는 설계상
# 화면을 안 바꾸고, 클릭의 효과가 1.5초 뒤에 시작하는 animation 이어도 마찬가지다.
#
# 둘도 정상이다. `hold_key` 다음 `move_pointer` 처럼 화면을 안 바꾸는 statement 둘이
# 붙어 있는 macro 를 사람이 평범하게 쓴다.
#
# 셋은 아니다. statement 상한이 128 이므로 안 멈추면 아무 반응 없는 게임에 128 개를 다
# 쏟아붓는다. 셋에서 끊으면 헛도는 시간이 4.5초를 넘지 않고, 위의 정상 둘은 그대로 통과한다.
#
# **연달아** 다. 화면이 움직이면 0 으로 돌아간다 — 움직였다 안 움직였다 하는 macro 는
# 어디론가 가고 있는 macro 다. 움직였는지 모르는 batch 는 세지도 지우지도 않는다.
STILL_SCREENS_BEFORE_STOP = 3


class MacroHost(Protocol):
    """runner 가 바깥에 대고 하는 일 둘.

    `ToolContext` 를 받지 않는다. `app.agents.qa.tools.tool_context` 를 import 하면
    `tools/__init__.py` 가 먼저 돌고 그것이 `macro_tools` 를 import 하므로 순환이 된다.
    그리고 이 모양이면 runner 테스트가 가짜 channel 없이 돈다.

    `run` 이 문장이 아니라 `ActionOutcome` 을 돌려주는 이유는 **runner 가 돌아온 값을
    봐야 하기 때문**이다. macro 는 batch 사이에 model 턴을 안 쓰는 것이 존재 이유라
    문장을 읽어 줄 쪽이 없고, runner 가 문장을 정규식으로 긁으면 문구 한 줄 고칠 때마다
    runner 가 조용히 안 멈춘다(ARTEL-777 이 표현을 프로토콜에 싣지 말라고 적은 것과 같은
    이유). 그래서 host 가 문장과 그 앞의 데이터를 함께 낸다.
    """

    async def run(
        self, actions: list[JsonRpcAction], summary: str, step: int
    ) -> ActionOutcome: ...

    def memories(self) -> MacroMemories: ...

    async def checkpoint(self, result: "MacroRunResult") -> bool:
        """`checkpoint` 에서 턴을 돌려주고, agent 가 정할 때까지 기다린다.

        `result` 는 거기까지의 장부이고 `paused_at` 이 찬 채로 온다. 이으면 `True`, 그만두면
        `False` 다. 그만두면 runner 는 아무것도 더 안 보낸다.
        """
        ...


@dataclass(frozen=True)
class MacroPlace:
    """`def` 이름과 그 안의 statement 번호. 세 목록과 두 거둠이 전부 이것을 든다.

    `total` 이 함께 드는 이유는 번호 하나만으로는 어디까지 왔는지가 안 읽히기 때문이다.
    `step N of M` 의 M 이고, `def` 마다 다르다.
    """

    function: str
    number: int
    total: int
    # 감싼 반복마다 몇 회째인가. 바깥이 앞이고, 반복 밖이면 빈 tuple 이다.
    #
    # `number` 와 `total` 은 **적힌** 위치로 둔다. agent 가 고치는 것은 적힌 줄이라, 실행된
    # 순번은 줄을 못 가리킨다. 반복 안의 statement 는 여러 번 돌므로 그 중 몇 번째인지를
    # 여기 붙인다. 실행된 총수는 `MacroRunResult.executed` 가 따로 든다.
    passes: tuple[int, ...] = ()

    def __str__(self) -> str:
        written = f"{self.function} step {self.number} of {self.total}"
        if not self.passes:
            return written
        if len(self.passes) == 1:
            return f"{written} (pass {self.passes[0]})"
        return f"{written} (passes {', '.join(str(one) for one in self.passes)})"


@dataclass(frozen=True)
class MacroFlagged:
    """`flag` 가 남긴 한 줄. agent 가 무시해도 아무 일도 일어나지 않는다."""

    place: MacroPlace
    message: str
    observed: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MacroVerdictRequest:
    """`ask_verdict` 가 세운 의무. 이 macro 를 부른 step 에 판정이 필요하다는 말이다.

    `expected` 는 macro 가 적은 기대이고 `observed` 는 runner 가 읽은 값이다. 둘이 짝이
    되어야 agent 가 판정할 수 있다 — 에러 payload 가 `expected` 와 `observed` 두 칸으로
    하는 일과 같다.
    """

    place: MacroPlace
    step: int
    expected: str
    observed: dict[str, Any] = field(default_factory=dict)


@dataclass
class MacroRunResult:
    name: str
    # 어느 `def` 의 몇 번까지 갔나. 게임에 나간 action statement 만.
    applied: list[MacroPlace] = field(default_factory=list)
    pending: list[MacroPlace] = field(default_factory=list)
    skipped: list[MacroPlace] = field(default_factory=list)
    flags: list[MacroFlagged] = field(default_factory=list)
    verdict_requests: list[MacroVerdictRequest] = field(default_factory=list)
    # `run` 이 돌려준 문장들. action 하나에 하나이고, 화면은 안 붙어 있다 — 화면은
    # `run_macro` 의 답 끝에 한 번만 붙는다.
    outcomes: list[str] = field(default_factory=list)
    # 멈춘 이유. 끝까지 갔으면 `None`.
    failure: MacroFailure | None = None
    # 멈춘 자리와 그 자리까지의 호출 사슬.
    stopped_at: MacroPlace | None = None
    chain: list[MacroPlace] = field(default_factory=list)
    # 실행된 statement 수. 반복이 있으면 적힌 수와 다르다.
    executed: int = 0
    # `checkpoint` 에서 턴을 돌려준 자리와 그 이유. 이어 돌면 다시 비운다.
    paused_at: MacroPlace | None = None
    paused_reason: str = ""
    # agent 가 `checkpoint` 에서 그만두기로 한 자리. 실패가 아니다.
    abandoned_at: MacroPlace | None = None

    @property
    def passed(self) -> bool:
        """끝까지 갔나. 실패해도, `checkpoint` 에서 그만둬도 아니다."""
        return self.failure is None and self.abandoned_at is None


# --- 번호 ---------------------------------------------------------------------


def numbered(statements: tuple[MacroStatement, ...], start: int = 1) -> list[tuple[int, MacroStatement]]:
    """`def` 하나의 statement 를 깊이 우선으로 번호 매긴 평면 목록.

    번호가 곧 적힌 순서이고, 그래서 "실패 지점 뒤" 가 "번호가 더 큰 것" 과 같아진다.
    분기 안쪽도 센다 — `if` 가 statement 총수를 바꾸지 않는다는 말이 이 세기다.
    """
    flat: list[tuple[int, MacroStatement]] = []
    next_number = start
    for statement in statements:
        number = next_number
        next_number += 1
        flat.append((number, statement))
        for inner in _bodies(statement):
            nested = numbered(inner, next_number)
            flat.extend(nested)
            next_number += len(nested)
    return flat


def _bodies(statement: MacroStatement) -> tuple[tuple[MacroStatement, ...], ...]:
    """statement 가 품은 몸통들. 번호를 매기고 범위를 잴 때 같은 순서로 내려간다."""
    if isinstance(statement, MacroIfStatement):
        return (statement.body, statement.orelse)
    if isinstance(statement, (MacroForStatement, MacroWhileStatement)):
        return (statement.body,)
    return ()


class _Frame:
    """한 `def` 가 도는 동안의 장부. 번호를 한 번만 매기고 들고 있는다."""

    def __init__(self, function: MacroFunction, scope: MacroScope) -> None:
        self.function = function
        self.scope = scope
        self.flat = numbered(function.statements)
        self.total = len(self.flat)
        # statement 하나하나의 번호. 같은 글의 statement 둘이 서로 다른 객체라 `id` 로
        # 가른다 — 내용으로 가르면 `click(card)` 가 두 번 적힌 macro 에서 둘이 겹친다.
        self.numbers = {id(statement): number for number, statement in self.flat}
        # 이 몸통에서 조건이 거짓이라 안 간 번호들.
        self.skipped: set[int] = set()
        # 지금 처리 중인 번호. `pending` 의 경계가 이 값이다.
        self.at = 0
        # 지금 안에 서 있는 반복마다, 그 몸통이 차지하는 번호와 지금 몇 회째인가. 바깥이
        # 앞이다.
        self.loops: list[tuple[set[int], int]] = []

    def place(self, number: int) -> MacroPlace:
        """번호 하나의 자리. 그 번호를 품은 반복의 회만 붙인다.

        지금 서 있는 회를 번호와 상관없이 붙이면, 멈춘 자리 뒤의 `pending` 이 반복 **밖**
        의 줄인데도 `(pass 2)` 를 단다 — 그 줄은 몇 회째의 것이 아니다.
        """
        return MacroPlace(
            function=self.function.name,
            number=number,
            total=self.total,
            passes=tuple(turn for span, turn in self.loops if number in span),
        )

    def number_of(self, statement: MacroStatement) -> int:
        return self.numbers[id(statement)]

    def pending(self) -> list[MacroPlace]:
        """이 몸통에서 아직 안 나간 번호. 가지 않기로 정해진 것은 뺀다.

        번호가 적힌 순서라, "실패 지점 뒤" 가 "번호가 더 큰 것" 과 같아진다. 실패 지점
        뒤에 있어 `if` 가 아직 판정되지 않은 분기의 statement 도 여기 든다 — 그것은
        `skipped` 가 아니다.
        """
        return [
            self.place(number)
            for number, _statement in self.flat
            if number > self.at and number not in self.skipped
        ]


# --- 실행 ---------------------------------------------------------------------


async def run_macro(
    host: MacroHost,
    definition: MacroDefinition,
    arguments: dict[str, Any],
    step: int,
) -> MacroRunResult:
    """등록된 정의 하나를 지금의 scene 에 대고 실행한다.

    `arguments` 는 이미 검사된 것만 온다. 선언 타입 검사와 좌표·`#` id 검사는 tool
    표면(`macro-tools`)이 **첫 statement 를 처리하기 전에** 끝낸다 — 곧 `run` 을 처음
    부르기 전이다.
    """
    runner = _Runner(host, definition, step)
    await runner.walk(definition.entry, arguments)
    return runner.result


class _Runner:
    def __init__(self, host: MacroHost, definition: MacroDefinition, step: int) -> None:
        self.host = host
        self.definition = definition
        self.step = step
        self.result = MacroRunResult(name=definition.name)
        self.frames: list[_Frame] = []
        # 감싼 `if` 조건이 읽은 값. `flag` 와 `ask_verdict` 의 `observed` 가 이것이다.
        self.observed: list[dict[str, Any]] = []
        # 화면이 연달아 몇 번 그대로였나. 화면이 움직이면 0 으로 돌아가고, 움직였는지
        # 모르는 batch 는 세지도 지우지도 않는다 — 까닭은 `_went_wrong` 에 있다.
        self.still_screens = 0

    # -- 바깥 --

    def memories(self) -> MacroMemories:
        return self.host.memories()

    def chain(self) -> list[MacroPlace]:
        """지금 선 자리까지의 호출 사슬. 바깥 frame 이 앞이다."""
        return [frame.place(frame.at) for frame in self.frames]

    def surrounding(self) -> dict[str, Any]:
        """감싼 `if` 조건 전부가 읽은 값. 없으면 빈 사전이고 그래도 statement 는 선다."""
        merged: dict[str, Any] = {}
        for layer in self.observed:
            merged.update(layer)
        return merged

    def pending(self) -> list[MacroPlace]:
        """안쪽 frame 의 남은 것 먼저, 그 다음 그것을 부른 frame 의 남은 것."""
        remaining: list[MacroPlace] = []
        for frame in reversed(self.frames):
            remaining.extend(frame.pending())
        return remaining

    def stop(self, failure: MacroFailure, place: MacroPlace) -> None:
        """멈춘 자리와 그때까지의 장부를 결과에 싣는다.

        `applied` 가 비어 있으면 `SCENE_MISMATCH`, 하나라도 나갔으면 `REQUIRE_FAILED`
        로 가른다. 들어선 자리가 틀린 것과 하다가 어긋난 것은 agent 가 할 일이 다르다.
        """
        self.result.failure = failure
        self.result.stopped_at = place
        self.result.chain = self.chain()
        self.result.pending = self.pending()
        # `require` 가 실패해 멈춘 경우에는 그 앞에서 거둔 것을 payload 에 같이 싣는다.
        failure.payload.update(
            {
                "macro": self.definition.name,
                "stopped_at": str(place),
                "chain": [str(one) for one in self.result.chain],
                "applied": [str(one) for one in self.result.applied],
                "pending": [str(one) for one in self.result.pending],
                "skipped": [str(one) for one in self.result.skipped],
                "flags": [one.message for one in self.result.flags],
                "verdict_requests": [one.expected for one in self.result.verdict_requests],
                "executed": self.result.executed,
            }
        )

    # -- 몸통 --

    async def walk(self, function: MacroFunction, arguments: dict[str, Any]) -> bool:
        """`def` 하나를 돈다. 끝까지 가면 `True`, 멈추면 `False`."""
        frame = _Frame(function=function, scope=MacroScope(arguments))
        self.frames.append(frame)
        try:
            return await self._statements(frame, function.statements)
        finally:
            self.frames.pop()

    async def _statements(
        self, frame: _Frame, statements: tuple[MacroStatement, ...]
    ) -> bool:
        for statement in statements:
            number = frame.number_of(statement)
            frame.at = number
            if not await self._statement(frame, number, statement):
                return False
        return True

    async def _statement(
        self, frame: _Frame, number: int, statement: MacroStatement
    ) -> bool:
        place = frame.place(number)
        try:
            self.result.executed += 1
            if self.result.executed > MAX_EXECUTED_STATEMENTS:
                raise MacroFailure(
                    LOOP_LIMIT,
                    f"this macro has run {MAX_EXECUTED_STATEMENTS} statements, the most "
                    "one call may run, and was about to run more. Nothing after this "
                    "was sent. Loops inside loops multiply — check that each loop "
                    "really ends.",
                    {"executed": MAX_EXECUTED_STATEMENTS},
                )
            if isinstance(statement, MacroForStatement):
                return await self._for(frame, place, statement)
            if isinstance(statement, MacroWhileStatement):
                return await self._while(frame, place, statement)
            if isinstance(statement, MacroCheckpointStatement):
                return await self._checkpoint(frame, place, statement)
            if isinstance(statement, MacroAssignStatement):
                self._assign(frame, statement)
                return True
            if isinstance(statement, MacroRequireStatement):
                return await self._require(frame, place, statement)
            if isinstance(statement, MacroIfStatement):
                return await self._branch(frame, place, statement)
            if isinstance(statement, MacroFlagStatement):
                self.result.flags.append(
                    MacroFlagged(
                        place=place,
                        message=statement.message,
                        observed=self.surrounding(),
                    )
                )
                return True
            if isinstance(statement, MacroAskVerdictStatement):
                self.result.verdict_requests.append(
                    MacroVerdictRequest(
                        place=place,
                        # 이 macro 를 부른 `run_macro` 의 step 이다. macro 원문에는 step 이
                        # 없다 — 있으면 호출의 step 과 어긋날 수 있다.
                        step=self.step,
                        expected=statement.expected,
                        observed=self.surrounding(),
                    )
                )
                return True
            if isinstance(statement, MacroHelperCallStatement):
                return await self._helper(frame, statement)
            return await self._action(frame, place, statement)
        except MacroFailure as failure:
            self.stop(failure, place)
            return False

    def _assign(self, frame: _Frame, statement: MacroAssignStatement) -> None:
        """게임에 아무것도 안 보낸다. 그래서 `applied` 에 절대 안 든다."""
        value = statement.value
        if isinstance(value, MacroFindValue):
            frame.scope.bind(
                statement.name, resolve_find(self.memories(), frame.scope, value)
            )
            return
        if isinstance(value, MacroSelectorValue):
            # 그 자리에서 풀지 않는다. 문자열에 이름만 붙이고 late binding 을 둔다.
            frame.scope.bind(statement.name, LateSelector(selector=value.selector))
            return
        if isinstance(value, MacroReadValue):
            # 지금 읽고 다시 읽지 않는다. 이유는 `read_to_bind` 에 있다.
            frame.scope.bind(
                statement.name,
                read_to_bind(
                    self.memories(),
                    frame.scope,
                    value.call,
                    statement.declared_type,
                    statement.name,
                ),
            )
            return
        assert isinstance(value, MacroLiteralValue)
        frame.scope.bind(statement.name, value.literal.value)

    async def _require(
        self, frame: _Frame, place: MacroPlace, statement: MacroRequireStatement
    ) -> bool:
        """`ctx.run` 이 이미 기다리는 다음 pulse 를 관측 자리로 쓴다.

        따로 관측하지 않는다. `ToolContext.run` 이 행위 뒤의 판독을 기다리고 돌아오므로,
        그 다음 statement 가 읽는 memory 는 이미 그 행위의 결과다.
        """
        observed: dict[str, Any] = {}
        if evaluate(self.memories(), frame.scope, statement.condition, observed):
            return True

        # `applied` 가 비어 있으면 들어선 자리가 틀렸다는 말이다. 하나라도 나갔으면
        # 하다가 어긋난 것이고, agent 가 할 일이 다르다.
        code = REQUIRE_FAILED if self.result.applied else SCENE_MISMATCH
        self.stop(
            MacroFailure(
                code,
                f"{statement.source} is false. {statement.remedy}",
                {"expected": statement.source, "observed": _printable(observed)},
            ),
            place,
        )
        return False

    async def _branch(
        self, frame: _Frame, place: MacroPlace, statement: MacroIfStatement
    ) -> bool:
        observed: dict[str, Any] = {}
        taken = evaluate(self.memories(), frame.scope, statement.condition, observed)
        going, skipping = (
            (statement.body, statement.orelse)
            if taken
            else (statement.orelse, statement.body)
        )
        # 가지 않기로 정해진 쪽은 `skipped` 다. `pending` 이 아니다 — 실패해서 못 나간
        # 것과 조건이 거짓이라 원래 안 가는 것은 다른 얘기이고, 구분하지 않으면 읽는
        # 쪽이 매번 추측한다. 중첩된 분기 안쪽까지 평면으로 센다.
        for skipped_number in _spanned(skipping, frame):
            frame.skipped.add(skipped_number)
            self.result.skipped.append(frame.place(skipped_number))

        self.observed.append(observed)
        try:
            return await self._statements(frame, going)
        finally:
            self.observed.pop()

    async def _for(
        self, frame: _Frame, place: MacroPlace, statement: MacroForStatement
    ) -> bool:
        """도는 대상을 들어설 때 한 번 정하고, 각 회에 이름을 다시 bind 한다.

        `find_all` 을 회마다 다시 풀지 않는다. 몸통이 손패를 바꿀 때마다 도는 대상이
        바뀌면 한 장을 두 번 내거나 한 장도 안 낼 수 있다.
        """
        iterable = statement.iterable
        if iterable.kind == "find_all":
            items: list[Any] = resolve_find_all(
                self.memories(), frame.scope, iterable.find
            )
            what = f"find_all matched {len(items)} objects"
        else:
            count = (
                iterable.count.value
                if iterable.count is not None
                else frame.scope.value(iterable.name)
            )
            # Python 의 `range` 와 같게 음수는 0 회다.
            items = list(range(max(int(count), 0)))
            what = f"range was asked for {len(items)} passes"
        if len(items) > MAX_LOOP_PASSES:
            raise MacroFailure(
                LOOP_LIMIT,
                f"`{statement.source}` would turn {len(items)} times ({what}), and a "
                f"loop turns at most {MAX_LOOP_PASSES}. Nothing in the loop was sent. "
                "Narrow it with `under=` or `label=`, or split the work.",
                {"passes": len(items), "limit": MAX_LOOP_PASSES},
            )
        return await self._passes(
            frame, statement, ((statement.name, item) for item in items), None
        )

    async def _while(
        self, frame: _Frame, place: MacroPlace, statement: MacroWhileStatement
    ) -> bool:
        """매 회 전에 조건을 다시 읽는다. 상한 회수를 돌고도 참이면 멈춘다."""
        return await self._passes(frame, statement, None, statement)

    async def _passes(
        self,
        frame: _Frame,
        loop: MacroForStatement | MacroWhileStatement,
        bindings: Iterator[tuple[str, Any]] | None,
        guard: MacroWhileStatement | None,
    ) -> bool:
        """`for` 와 `while` 이 같이 쓰는 회의 장부.

        회가 바뀔 때마다 이 몸통 안의 `skipped` 표시를 지운다. 지난 회에 거짓이던 `if`
        가 이번 회에는 아직 판정되지 않았으므로, 남겨 두면 실패 지점 뒤의 그 분기가
        `pending` 에서 빠져 "원래 안 가는 것" 으로 읽힌다.
        """
        loop_number = frame.number_of(loop)
        span = set(_spanned(loop.body, frame))
        frame.loops.append((span, 0))
        passes = 0
        try:
            while True:
                observed: dict[str, Any] = {}
                if guard is not None:
                    if not evaluate(self.memories(), frame.scope, guard.condition, observed):
                        break
                    if passes == MAX_LOOP_PASSES:
                        frame.at = loop_number
                        raise MacroFailure(
                            LOOP_LIMIT,
                            f"`{guard.source}` was still true after {MAX_LOOP_PASSES} "
                            "passes, the most a loop may turn. Nothing after this was "
                            "sent. Either the game never made the condition false, or "
                            "the condition is not the one that changes — observe the "
                            "screen to tell which.",
                            {
                                "passes": MAX_LOOP_PASSES,
                                "observed": _printable(observed),
                            },
                        )
                else:
                    assert bindings is not None
                    taken = next(bindings, None)
                    if taken is None:
                        break
                    frame.scope.bind(*taken)
                passes += 1
                frame.loops[-1] = (span, passes)
                frame.skipped -= span
                self.observed.append(observed)
                try:
                    if not await self._statements(frame, loop.body):
                        return False
                finally:
                    self.observed.pop()
        finally:
            frame.loops.pop()
        frame.at = loop_number
        if passes == 0:
            # 한 회도 안 돈 몸통은 `if` 의 거짓인 가지와 같다. 실패가 아니고, 원래 안 간다.
            for number in sorted(span):
                frame.skipped.add(number)
                self.result.skipped.append(frame.place(number))
        return True

    async def _checkpoint(
        self, frame: _Frame, place: MacroPlace, statement: MacroCheckpointStatement
    ) -> bool:
        """턴을 돌려주고 agent 의 답을 기다린다. 실패가 아니다.

        기다리는 동안의 `pending` 은 이 자리 뒤의 것이다. 이어 돌면 비운다 — 끝났을 때의
        `pending` 은 끝난 자리가 정한다.
        """
        self.result.paused_at = place
        self.result.paused_reason = statement.reason
        self.result.pending = self.pending()
        go_on = await self.host.checkpoint(self.result)
        self.result.paused_at = None
        self.result.paused_reason = ""
        if go_on:
            self.result.pending = []
            return True
        self.result.abandoned_at = place
        self.result.chain = self.chain()
        return False

    async def _helper(
        self, frame: _Frame, statement: MacroHelperCallStatement
    ) -> bool:
        """펼치지 않고 호출로 돈다. payload 가 호출 사슬을 싣는 이유가 이것이다."""
        helper = self.definition.function(statement.callee)
        if helper is None:  # pragma: no cover - parser 가 없는 이름을 거절한다
            raise MacroFailure(
                ACTION_REJECTED, f"`{statement.callee}` is not a `def` in this macro."
            )
        arguments = {
            parameter.name: value
            for parameter, value in _helper_arguments(helper, statement, frame, self)
        }
        return await self.walk(helper, arguments)

    async def _action(
        self, frame: _Frame, place: MacroPlace, statement: MacroActionStatement
    ) -> bool:
        spec = TOOLS_BY_NAME[statement.callee]
        values = _argument_values(spec, statement, frame, self)
        actions = _actions(spec, values)

        # 여기까지 오면 게임에 나간다. 번호를 먼저 적는 이유는 `applied` 의 뜻이
        # "나갔다" 이기 때문이다 — 돌려받은 문장이 실패라고 해도 그 action 은 나갔다.
        self.result.applied.append(place)
        # `summary` 에 저자가 쓴 원문 줄을 넣어 `ACTION` frame 마다 그것이 남게 한다.
        # `ast.unparse` 로 되찍지 않는다 — 그러면 timeline 에 남는 것이 agent 가 쓴
        # 글자가 아니게 되고, 자기가 쓴 줄을 못 알아보는 것이 제일 비싼 혼선이다.
        outcome = await self.host.run(actions, statement.source, self.step)
        self.result.outcomes.append(outcome.text)

        # **돌아온 값을 본다.** 종전에는 여기서 무조건 `True` 였고, 그래서 클릭이 허공에
        # 떨어져도 사람이 마우스를 쥐어도 macro 가 끝까지 갔다 — 저자가 `require` 로 미리
        # 상상한 실패만 잡혔다.
        failure = self._went_wrong(statement, outcome)
        if failure is None:
            return True
        self.stop(failure, place)
        return False

    def _went_wrong(
        self, statement: MacroActionStatement, outcome: ActionOutcome
    ) -> MacroFailure | None:
        """나간 action 하나의 결과를 보고, 여기서 멈춰야 하는지. 아니면 `None`.

        **순서가 뜻을 갖는다.** operator 가 먼저다 — 사람이 말을 걸었으면 게임이 뭐라고
        했든 그 말이 먼저 읽혀야 한다. 그 다음이 누름이 닿은 자리이고, 원인을 이름으로
        댄다. 화면이 안 움직인 것이 마지막이다 — 셋 중 가장 약한 근거이고, 혼자서는
        못 서서 횟수를 세야 한다.
        """
        if outcome.operator_messages:
            return _operator_spoke(outcome.operator_messages)
        if PressLanding.held_by_person in outcome.landings:
            # 하나만 있어도 멈춘다. 사람이 마우스를 쥔 것은 batch 전체에 걸리는 일이지
            # 이 누름 하나의 사정이 아니다.
            return MacroFailure(
                ACTION_REJECTED,
                f"`{statement.source}` did not reach the game: a person is holding the "
                "mouse, so the virtual pointer went nowhere. Nothing after this was "
                "sent. This is the machine, not the game and not the macro — take your "
                "hand off the mouse and call the macro again.",
            )
        if outcome.landings and all(
            landing is PressLanding.reached_nothing for landing in outcome.landings
        ):
            # 설계 때 "click 계열 뒤에 닿았는가를 암묵적 `require` 로 붙인다" 고 한 자리다.
            #
            # **batch 의 누름이 하나도 못 닿았을 때만** 멈춘다. `click` 은 `mouse_down`
            # 과 `mouse_up` 둘을 내는데, 누르자마자 사라지는 카드를 누르면 뒤엣것이 빈
            # 자리에 떨어진다 — 그것까지 실패로 치면 성공한 클릭에서 멈춘다. 전부 비었을
            # 때는 그런 사정이 없고, 포인터 밑에 처음부터 아무것도 없었다는 뜻이다.
            return MacroFailure(
                REQUIRE_FAILED,
                f"`{statement.source}` pressed empty space: nothing was under the "
                "pointer, so the game received no press at all. Nothing after this was "
                "sent. Observe the scene and check the macro is aiming at something "
                "this screen actually shows.",
            )
        if outcome.screen is ScreenChange.moved:
            self.still_screens = 0
            return None
        if outcome.screen is ScreenChange.unknown:
            # **세지도 않고 지우지도 않는다.** `unknown` 은 게임이 결과를 아예 안 줬거나
            # (`dispatch_actions` 의 타임아웃이다) 화면을 한 번도 안 보낸 빌드라는 뜻이다.
            #
            # 세면 화면을 안 보내는 빌드에서 멀쩡한 macro 가 전부 멈춘다. 0 으로
            # 지우면 더 나쁘다 — 멈춘 게임은 간헐적으로 타임아웃을 내므로 실제 궤적이
            # `still`·`unknown`·`still`·`unknown` 이 되고, 지우는 구현은 그 사이에서
            # 수를 계속 잃어 반응 없는 게임에 statement 를 끝까지 다 쏟아붓는다.
            return None
        self.still_screens += 1
        if self.still_screens < STILL_SCREENS_BEFORE_STOP:
            return None
        return MacroFailure(
            SCREEN_UNCHANGED,
            f"{self.still_screens} actions in a row left the screen exactly as it was, "
            f"the last of them `{statement.source}`. Nothing after this was sent. "
            "Either the game stopped responding or this macro is somewhere it was not "
            "written for — observe the scene and decide which, because a screen frozen "
            "under real input is a defect worth reporting.",
        )


def _operator_spoke(messages: tuple[str, ...]) -> MacroFailure:
    """operator 가 말을 걸었다. **무슨 말이든 멈춘다.**

    말의 내용을 가리지 않는다. "멈춰" 만 골라내려면 멈추라는 말의 목록을 가지고 있어야
    하는데, operator 에게 명령어 목록을 알려 준 적이 없어서 그 목록은 맞히는 쪽보다
    틀리는 쪽이 흔하다. 그리고 틀리는 방향이 나쁘다 — 목록에 없는 말로 그만하라고 한
    사람은 macro 가 남은 statement 를 다 쏟아붓는 동안 자기 말이 무시되는 것을 본다.

    **그대로 흘려보내는 것과 다른 점은 언제 읽히느냐 하나다.** `with_operator_messages`
    는 여기서도 그대로 돌아 그 말을 결과 끝에 붙이므로 버려지는 말이 없다. 다른 것은
    agent 가 그 말을 **남은 action 이 전부 게임에 나간 뒤**가 아니라 **지금** 읽는다는
    것뿐이다. model 턴은 안 쓴다 — macro step 하나가 batch 하나라 그 사이에 끼어들 자리가
    이미 있고, 여기서 하는 일은 그 자리에서 멈추는 것이 전부다.

    헛멈춤의 값은 model 턴 하나다. 놓친 멈춤의 값은 사람이 그만하라고 한 뒤에 남은
    macro 가 전부 게임에 떨어지는 것이다.
    """
    said = " / ".join(messages)
    return MacroFailure(
        OPERATOR_INTERRUPTED,
        f"the operator spoke while the macro was running: {said}. It stopped here "
        "rather than sending the rest, so you read that before the game moves any "
        "further. Neither the game nor the macro did anything wrong — decide what the "
        "operator wants, and call the macro again if it still applies.",
    )


def _spanned(statements: tuple[MacroStatement, ...], frame: _Frame) -> list[int]:
    """그 몸통이 차지하는 번호 전부. 중첩된 `if` 안쪽까지 내려간다.

    가지 않은 분기를 `skipped` 에 적을 때 그 안의 번호를 하나도 빠뜨리면 안 된다 —
    빠뜨린 번호는 `pending` 으로 새어 나가 "아직 안 나갔다" 로 읽힌다.
    """
    numbers: list[int] = []
    for statement in statements:
        numbers.append(frame.number_of(statement))
        for inner in _bodies(statement):
            numbers.extend(_spanned(inner, frame))
    return sorted(numbers)


# --- 인자 ---------------------------------------------------------------------


def _argument_values(
    spec: ToolSpec, statement: MacroActionStatement, frame: _Frame, runner: "_Runner"
) -> dict[str, Any]:
    """적힌 인자를 지금의 값으로, 안 적힌 자리는 표의 기본값으로.

    기본값이 저장된 JSON 에 없는 이유는 그것이 저자가 적은 것이 아니기 때문이다. 표가
    유일한 출처라, 기본값을 바꾸면 이미 저장된 macro 도 함께 따라온다.
    """
    values: dict[str, Any] = {
        parameter.name: parameter.default
        for parameter in spec.parameters
        if not parameter.required
    }
    for argument in statement.arguments:
        if argument.kind == "target":
            values[argument.parameter] = resolve_target(
                runner.memories(), frame.scope, argument.target
            )
        elif argument.kind == "literal":
            values[argument.parameter] = argument.literal.value
        else:
            values[argument.parameter] = frame.scope.value(argument.name)
    return values


def _helper_arguments(
    helper: MacroFunction,
    statement: MacroHelperCallStatement,
    frame: _Frame,
    runner: "_Runner",
) -> Iterator[tuple[MacroParameter, Any]]:
    """helper 의 parameter 마다 (parameter, 값). 안 적힌 자리는 `def` 줄의 기본값."""
    written = {argument.parameter: argument for argument in statement.arguments}
    for parameter in helper.parameters:
        argument = written.get(parameter.name)
        if argument is None:
            yield parameter, None if parameter.default is None else parameter.default.value
            continue
        if argument.kind == "target":
            # helper 의 `object` parameter 다. 부르는 자리에서 풀어 기록으로 넘긴다 —
            # 그러면 helper 안에서 다시 푸는 일이 없고, 사라진 객체는 부르는 자리에서
            # `STALE_BINDING` 이 난다.
            yield parameter, resolve_target(
                runner.memories(), frame.scope, argument.target
            )
        elif argument.kind == "literal":
            yield parameter, argument.literal.value
        else:
            yield parameter, frame.scope.value(argument.name)


def _actions(spec: ToolSpec, values: dict[str, Any]) -> list[JsonRpcAction]:
    """한 statement 를 `JsonRpcAction` 배치 하나로. 표가 그 조립을 든다."""
    actions: list[JsonRpcAction] = []
    for index, step in enumerate(spec.steps, start=1):
        params: list[Any] = []
        for name in step.arguments:
            parameter = spec.parameter(name)
            value = values.get(name)
            if parameter is not None and parameter.is_target:
                params.extend(_aim(parameter, value))
            else:
                params.append(value)
        actions.append(JsonRpcAction(id=index, method=step.method, params=params))
    return actions


def _aim(parameter: ToolParameter, found: FoundObject) -> list[Any]:
    """bind 된 기록을 그 tool 이 받는 `target` 값으로.

    `enter_text` 는 `target_id: int` 를 받으므로 기록에서 `id` 를 꺼낸다. `id` 는
    `int | None` 이라 `None` 이면 넣을 값이 없고, 게임에 아무것도 보내기 전에 거절한다.

    나머지는 `selector` 를 쓴다. SDK 가 action 이 실제로 도는 순간에 푸므로, 관측과
    호출 사이에 움직인 대상도 지금 있는 자리에서 맞는다. `selector` 가 없는 기록만
    `id` 로 떨어진다.
    """
    record = found.record
    if parameter.as_instance_id:
        if record.id is None:
            raise MacroFailure(
                ACTION_REJECTED,
                f"`{parameter.name}` needs the object's instance id and the pulse record "
                f"for {found.key} does not carry one, so nothing was sent to the game.",
                {"object": found.key},
            )
        return [record.id]
    if record.selector:
        return [record.selector]
    if record.id is not None:
        return [record.id]
    raise MacroFailure(
        ACTION_REJECTED,
        f"the pulse record for {found.key} carries neither a selector nor an instance "
        "id, so there is nothing to aim at and nothing was sent to the game.",
        {"object": found.key},
    )


def _printable(observed: dict[str, Any]) -> dict[str, str]:
    """payload 에 싣는 모양. 값을 그대로 두면 JSON 으로 안 나가는 것이 섞인다."""
    return {name: repr(value) for name, value in observed.items()}
