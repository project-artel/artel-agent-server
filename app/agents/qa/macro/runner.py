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

**상한은 세지 않는다.** statement 총수 128 과 호출 깊이 3 은 `register_macro` 가 저장
시점에 정적으로 판정해, 넘는 macro 를 아예 등록하지 않는다. 그래서 여기에는 상한에
걸려 멈추는 경로도 그 전용 코드도 없다. 저장 때 잡는 것이 런타임에 잡는 것보다 언제나
낫다.

**세는 것만 편다, 실행은 안 편다.** helper 호출은 펼치지 않고 호출로 돈다. `step N of M`
은 `def` 마다 1부터 세고, payload 가 어느 `def` 의 몇 번인지와 호출 사슬을 함께 싣는다.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agents.qa.macro.binding import (
    FoundObject,
    LateSelector,
    MacroMemories,
    MacroScope,
    evaluate,
    resolve_find,
    resolve_target,
)
from app.agents.qa.macro.errors import (
    ACTION_REJECTED,
    REQUIRE_FAILED,
    SCENE_MISMATCH,
    MacroFailure,
)
from app.agents.qa.macro.grammar import TOOLS_BY_NAME, ToolSpec
from app.agents.qa.macro.model import (
    MacroActionStatement,
    MacroAskVerdictStatement,
    MacroAssignStatement,
    MacroDefinition,
    MacroFindValue,
    MacroFlagStatement,
    MacroFunction,
    MacroHelperCallStatement,
    MacroIfStatement,
    MacroLiteralValue,
    MacroRequireStatement,
    MacroSelectorValue,
    MacroStatement,
)
from app.qa.envelope import JsonRpcAction


class MacroHost(Protocol):
    """runner 가 바깥에 대고 하는 일 둘.

    `ToolContext` 를 받지 않는다. `app.agents.qa.tools.tool_context` 를 import 하면
    `tools/__init__.py` 가 먼저 돌고 그것이 `macro_tools` 를 import 하므로 순환이 된다.
    그리고 이 모양이면 runner 테스트가 가짜 channel 없이 돈다.
    """

    async def run(
        self, actions: list[JsonRpcAction], summary: str, step: int
    ) -> str: ...

    def memories(self) -> MacroMemories: ...


@dataclass(frozen=True)
class MacroPlace:
    """`def` 이름과 그 안의 statement 번호. 세 목록과 두 거둠이 전부 이것을 든다.

    `total` 이 함께 드는 이유는 번호 하나만으로는 어디까지 왔는지가 안 읽히기 때문이다.
    `step N of M` 의 M 이고, `def` 마다 다르다.
    """

    function: str
    number: int
    total: int

    def __str__(self) -> str:
        return f"{self.function} step {self.number} of {self.total}"


@dataclass(frozen=True)
class MacroFlagged:
    """`flag` 가 남긴 한 줄. agent 가 무시해도 아무 일도 일어나지 않는다."""

    place: MacroPlace
    message: str
    observed: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MacroVerdictRequest:
    """`ask_verdict` 가 세운 의무. 그 step 에 판정이 필요하다는 말이다.

    `expected` 는 macro 가 적은 기대이고 `observed` 는 runner 가 읽은 값이다. 둘이 짝이
    되어야 agent 가 판정할 수 있다 — 에러 payload 가 `expected` 와 `observed` 로 하는
    일과 같은 기계다.
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
    # `run` 이 돌려준 문장들. action 하나에 한 줄이다.
    outcomes: list[str] = field(default_factory=list)
    # 멈춘 이유. 끝까지 갔으면 `None`.
    failure: MacroFailure | None = None
    # 멈춘 자리와 그 자리까지의 호출 사슬.
    stopped_at: MacroPlace | None = None
    chain: list[MacroPlace] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.failure is None


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
        if isinstance(statement, MacroIfStatement):
            for inner in (statement.body, statement.orelse):
                nested = numbered(inner, next_number)
                flat.extend(nested)
                next_number += len(nested)
    return flat


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

    def place(self, number: int) -> MacroPlace:
        return MacroPlace(
            function=self.function.name, number=number, total=self.total
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
                        step=statement.step,
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
                f"{statement.text} is false. {statement.remedy}",
                {"expected": statement.text, "observed": _printable(observed)},
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
        # `summary` 에 statement 텍스트를 넣어 `ACTION` frame 마다 그것이 남게 한다.
        self.result.outcomes.append(
            await self.host.run(actions, statement.text, self.step)
        )
        return True


def _spanned(statements: tuple[MacroStatement, ...], frame: _Frame) -> list[int]:
    """그 몸통이 차지하는 번호 전부. 중첩된 `if` 안쪽까지 내려간다.

    가지 않은 분기를 `skipped` 에 적을 때 그 안의 번호를 하나도 빠뜨리면 안 된다 —
    빠뜨린 번호는 `pending` 으로 새어 나가 "아직 안 나갔다" 로 읽힌다.
    """
    numbers: list[int] = []
    for statement in statements:
        numbers.append(frame.number_of(statement))
        if isinstance(statement, MacroIfStatement):
            numbers.extend(_spanned(statement.body, frame))
            numbers.extend(_spanned(statement.orelse, frame))
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
):
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


def _aim(parameter, found: FoundObject) -> list[Any]:
    """묶인 기록을 그 tool 이 받는 조준값으로.

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
