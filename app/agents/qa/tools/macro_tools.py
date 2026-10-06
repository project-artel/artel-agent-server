"""성공한 조작 묶음을 macro 로 적어 두고 다시 부르는 도구 다섯.

문구는 `app/agents/qa/macro/descriptions.py` 가 들고 있다 — `screen.py` 와
`capability.py` 의 선례다.

수명주기가 네 걸음이다. `write_macro` 로 초안을 쓰고, `read_macro` 로 읽고,
`edit_macro` 로 고치고, `register_macro` 로 부를 수 있게 한다. `run_macro` 는 등록된
것만 부른다. 초안은 런의 상태(`QaRunState.macros`)에 살고 `content_map` 에 안 쓰인다.

**이 PR 에서는 등록된 것도 런의 상태에 산다.** `content_map` 에 적을 frame 이
`artel-orchestration-server` 에 아직 없다. 그 자리는 `MacroBook.register` 하나다.
"""

from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.qa.macro.binding import LateSelector, MacroMemories
from app.agents.qa.macro.descriptions import (
    EDIT_MACRO_DESCRIPTION,
    READ_MACRO_DESCRIPTION,
    REGISTER_MACRO_DESCRIPTION,
    RUN_MACRO_DESCRIPTION,
    WRITE_MACRO_DESCRIPTION,
)
from app.agents.qa.macro.errors import MacroFailure, MacroRejection
from app.agents.qa.macro.grammar import DECLARABLE_TYPE_NAMES, MacroType
from app.agents.qa.macro.model import MacroDefinition, MacroParameter
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.macro.runner import (
    MacroPlace,
    MacroRunResult,
    run_macro as execute_macro,
)
from app.agents.qa.tools.action_tools import parse_target
from app.agents.qa.tools.state import QaRunState
from app.agents.qa.tools.tool_context import ToolContext


def build_macro_tools(ctx: ToolContext) -> list[BaseTool]:
    # 아래 tool 이 closure 로 잡는 것. 되묶는 이유는 `tool_context.py` 에 있다.
    channel, state = ctx.channel, ctx.state

    class _Host:
        """runner 가 바깥에 대고 하는 일 둘을 `ToolContext` 에 잇는다.

        runner 가 `ToolContext` 를 직접 받지 않는 이유는 `app/agents/qa/macro/runner.py`
        에 적혀 있다 — 그쪽이 `tools` 를 import 하면 순환이 된다. 그 잇는 자리가 여기
        하나다.
        """

        async def run(self, actions, summary: str, step: int) -> str:
            return await ctx.run(actions, summary, step)

        def memories(self) -> MacroMemories:
            return MacroMemories(scene=channel.scene)

    host = _Host()

    def _standing_screen() -> str:
        """등록하는 순간 agent 가 서 있는 `screen`. 없으면 빈 문자열.

        `_standing_scene` 과 같은 두 자리를 본다. `GAME_STATE` 없이 `pulse` 만 오는
        게임에서는 `SceneMemory.scene` 이 끝까지 비어 있다.

        `screen` 을 무엇으로 지목하는지 — 곧 식별자의 모양 — 는 `ARTEL-919` 가 스키마와
        함께 정한다. 그것이 정해지기 전에 모양을 못 박지 않으려고, 여기서는 지금 서 있는
        scene 이름을 그대로 쓰고 해석하지 않는다.
        """
        return (channel.scene.scene or channel.scene.pulse.scene or "").strip()

    def _parsed(name: str, source: str) -> tuple[MacroDefinition | None, str]:
        """파싱해 보고 결과를 말한다. 등록하지도, 실행하지도 않는다."""
        try:
            return macro_definition_from_source(name, source), ""
        except MacroRejection as rejection:
            return None, rejection.render()

    @tool(description=WRITE_MACRO_DESCRIPTION)
    async def write_macro(step: int, thought: str, name: str, source: str) -> str:
        # What the agent reads is WRITE_MACRO_DESCRIPTION, not this.
        #
        # 화면을 안 돌려준다. 이 호출은 게임을 안 건드리므로 화면을 실으면 에이전트가
        # 이미 들고 있는 것을 문맥에 한 번 더 사는 것이다 — 지식 tool 들과 같은 판단
        # (ARTEL-180).
        macro_name = (name or "").strip()
        if not macro_name:
            return "`name` must say what to call this macro, so nothing was written."

        definition, problem = _parsed(macro_name, source or "")
        if definition is None:
            # 거절된 초안도 저장하지 않는다. 저장하면 `read_macro` 가 부를 수 없는 글을
            # 초안이라고 돌려주고, agent 는 그것을 고치면 된다고 읽는다.
            return (
                f"{macro_name} was not written, because it does not parse: {problem}"
            )

        state.macros.write(macro_name, source)
        # 쓴 것은 읽은 것이다. 방금 자기가 적은 글을 고치려고 `read_macro` 를 한 번 더
        # 부르게 하는 것은 왕복을 하나 버리는 일이다.
        state.macros.remember_read(macro_name)
        return _draft_report(macro_name, definition)

    @tool(description=EDIT_MACRO_DESCRIPTION)
    async def edit_macro(
        step: int, thought: str, name: str, old_text: str, new_text: str
    ) -> str:
        # What the agent reads is EDIT_MACRO_DESCRIPTION, not this.
        macro_name = (name or "").strip()
        book = state.macros

        # **아무것도 안 고칠 수 있는 경우를 전부 먼저 본다.** 등록된 것을 초안으로
        # 복사하는 것은 상태를 바꾸는 일이라, 거절로 끝날 호출이 그것을 하고 나면
        # `read_macro` 가 등록된 것과 같은 글을 초안이라고 돌려준다 — agent 는 그것을
        # 아직 등록 안 된 것으로 읽는다. 거절은 아무 자취도 남기지 않는다.
        draft = book.source(macro_name)
        if draft is None:
            # 없는 이름을 먼저 말한다. 읽었는지를 먼저 보면, 이름을 잘못 적은 호출이
            # "읽지 않았다" 는 답을 받고 엉뚱한 것을 고치러 간다.
            return (
                f"There is no macro called {macro_name}, as a draft or registered, so "
                "nothing was changed. `write_macro` is where a new one starts."
            )
        if not book.was_read(macro_name):
            # 고치려면 먼저 읽어야 한다. 안 읽고 고치는 것은 무엇을 지우는지 모르고
            # 지우는 것이다.
            return (
                f"This run has not read {macro_name} yet, so nothing was changed. Call "
                "`read_macro` first — changing text you have not seen is changing text "
                "you cannot check."
            )
        if not old_text:
            return (
                "`old_text` must be the text to replace, so nothing was changed. To "
                "replace the whole macro, use `write_macro`."
            )

        # 줄 번호가 아니라 문자열 치환이다. 모델은 줄 번호를 틀리고, 틀린 줄 번호는
        # 조용히 엉뚱한 줄을 고친다. 그래서 유일하지 않으면 시끄럽게 실패한다.
        found = draft.count(old_text)
        if found == 0:
            return (
                f"`old_text` does not appear in {macro_name}, so nothing was changed. "
                "Read it again with `read_macro` and copy the text exactly."
            )
        if found > 1:
            return (
                f"`old_text` appears {found} times in {macro_name}, so nothing was "
                "changed — there is no way to tell which one you meant. Include enough "
                "of the surrounding lines to make it unique."
            )

        changed = draft.replace(old_text, new_text, 1)
        definition, problem = _parsed(macro_name, changed)
        if definition is None:
            return (
                f"{macro_name} was not changed, because the result does not parse: "
                f"{problem}"
            )

        # 여기까지 와서야 상태를 바꾼다. 등록된 것만 있었으면 이 자리에서 초안이 생기고,
        # 등록된 행은 `register_macro` 가 성공할 때까지 그대로다.
        book.write(macro_name, changed)
        return _draft_report(macro_name, definition)

    @tool(description=READ_MACRO_DESCRIPTION)
    async def read_macro(step: int, thought: str, name: str) -> str:
        # What the agent reads is READ_MACRO_DESCRIPTION, not this.
        macro_name = (name or "").strip()
        source = state.macros.source(macro_name)
        if source is None:
            known = ", ".join(sorted(_known(state))) or "none yet"
            return (
                f"There is no macro called {macro_name}. This run knows: {known}."
            )

        state.macros.remember_read(macro_name)
        where = (
            "draft" if state.macros.draft(macro_name) is not None else "registered"
        )
        return f"{macro_name} ({where}):\n\n{source}"

    @tool(description=REGISTER_MACRO_DESCRIPTION)
    async def register_macro(
        step: int, thought: str, name: str, screens: list[str] = []
    ) -> str:
        # What the agent reads is REGISTER_MACRO_DESCRIPTION, not this.
        #
        # 빈 목록 기본값은 `report_step` 의 `used_knowledge_ids` 와 같은 이유로 리터럴로
        # 적는다 — 모델이 채우는 schema 에서 optional array 는 그냥 빼면 되지만,
        # nullable 은 `null` 을 보내게 하고 그것이 "없다" 인지 "모른다" 인지가 또
        # 문제가 된다. 아래에서 읽기만 하고 고치지 않는다.
        macro_name = (name or "").strip()
        book = state.macros
        draft = book.draft(macro_name)
        if draft is None:
            if book.registered(macro_name) is not None:
                return (
                    f"{macro_name} is already registered and has no draft, so there is "
                    "nothing to register. `read_macro` then `edit_macro` is how you "
                    "change it."
                )
            return (
                f"There is no draft called {macro_name}, so nothing was registered. "
                "`write_macro` is where a draft starts."
            )

        definition, problem = _parsed(macro_name, draft)
        if definition is None:
            # 아무것도 등록하지 않는다. 저장된 뒤에 허용 목록이 좁아진 정의가 실행
            # 한복판에서 처음 거절되는 것을 막는 것이 이 검사의 자리다.
            return (
                f"{macro_name} was not registered: {problem}\n\nThe draft is unchanged, "
                "so `edit_macro` can fix it."
            )

        standing = _standing_screen()
        named = [one.strip() for one in (screens or []) if one and one.strip()]
        relations = tuple([standing] if standing else []) + tuple(named)
        registered = book.register(definition, relations)

        lines = [f"{macro_name} is registered. {_signature(definition)}"]
        if registered.screens:
            lines.append(
                "Related to: " + ", ".join(registered.screens) + "."
            )
        else:
            # 빈 관계는 아직 어디서 쓸지 모른다는 뜻이고, 서 있는 `screen` 을 모르는
            # 경우와 맞는다.
            lines.append(
                "It is related to no screen, because the run does not yet know which "
                "one it is standing on. Register it again from a settled screen, or "
                "name screens yourself, to record where it is used."
            )
        lines.append(
            "Nothing has run. `run_macro` is what calls it."
        )
        return "\n".join(lines)

    @tool(description=RUN_MACRO_DESCRIPTION)
    async def run_macro(
        step: int, thought: str, name: str, arguments: dict[str, Any] = {}
    ) -> str:
        # What the agent reads is RUN_MACRO_DESCRIPTION, not this.
        #
        # 위 `screens` 와 같은 이유로 빈 사전 리터럴이고, 읽기만 한다.
        #
        # 이슈는 이 인자를 `args` 라고 적는데 그 이름을 쓸 수 없다. langchain 이 함수
        # 서명에서 argument schema 를 떠낼 때 pydantic 이 `args` 를 내부 이름과
        # 부딪히는 것으로 보고 `v__args` 로 바꾸므로, 호출이 통째로 깨진다.
        macro_name = (name or "").strip()
        registered = state.macros.registered(macro_name)
        if registered is None:
            if state.macros.draft(macro_name) is not None:
                return (
                    f"{macro_name} is only a draft, so it cannot be called. "
                    "`register_macro` first."
                )
            known = ", ".join(sorted(state.macros.registrations)) or "none yet"
            return (
                f"There is no registered macro called {macro_name}. Registered in this "
                f"run: {known}."
            )

        definition = registered.definition
        bound, problem = _bind_arguments(definition, arguments or {})
        if problem:
            # 게임에 아무것도 보내기 전이다. 첫 statement 를 처리하기 전에 끝난다.
            return problem

        result = await execute_macro(host, definition, bound, step)
        return ctx.answer(_render(result), channel.drain_operator_messages())

    # `@tool` 이 이름을 함수에서 가져가므로 tool 이 자기 이름과 어긋난 문자열 아래
    # 등록될 일이 없다. 그래서 runner 의 같은 이름 함수를 `execute_macro` 로 받는다 —
    # 뒤에서 `.name` 을 고치면 langchain 이 함수에서 떠낸 argument schema 와 어긋난다.
    return [write_macro, edit_macro, read_macro, register_macro, run_macro]


def _known(state: QaRunState) -> set[str]:
    return set(state.macros.drafts) | set(state.macros.registrations)


def _signature(definition: MacroDefinition) -> str:
    """진입점의 parameter 를 한 줄로. `run_macro` 가 무엇을 물을지 미리 말해 준다."""
    if not definition.entry.parameters:
        return f"`{definition.name}` takes no arguments."
    written = ", ".join(_parameter(one) for one in definition.entry.parameters)
    return f"`{definition.name}` takes: {written}."


def _parameter(parameter: MacroParameter) -> str:
    text = f"{parameter.name}: {parameter.declared_type.value}"
    if parameter.default is not None:
        return f"{text} = {parameter.default.value!r}"
    return text


def _draft_report(name: str, definition: MacroDefinition) -> str:
    """초안이 무엇을 하겠다는 것인지. 등록하지 않았다는 것을 분명히 말한다."""
    statements = len(definition.entry.statements)
    helpers = ", ".join(helper.name for helper in definition.helpers)
    lines = [
        f"{name} is a draft and parses. {_signature(definition)}",
        f"Its entry point has {statements} statement(s)"
        + (f", and it has helpers: {helpers}." if helpers else "."),
        "Nothing is registered and nothing has run. `register_macro` is what makes it "
        "callable.",
    ]
    return "\n".join(lines)


# --- 호출 인자 검사 -------------------------------------------------------------


def _bind_arguments(
    definition: MacroDefinition, given: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """선언된 parameter 타입에 맞춰 호출 인자를 푼다. 안 맞으면 문장을 돌려준다.

    runner 의 입구에서, 첫 statement 를 처리하기 전에 끝난다 — 곧 `ctx.run` 을 처음
    부르기 전이다. 통과한 인자만 runner 가 받는다.
    """
    parameters = {one.name: one for one in definition.entry.parameters}
    unknown = [name for name in given if name not in parameters]
    if unknown:
        wanted = ", ".join(_parameter(one) for one in definition.entry.parameters)
        return {}, (
            f"{definition.name} has no parameter called {', '.join(sorted(unknown))}, "
            f"so nothing was sent to the game. It takes: {wanted or 'no arguments'}."
        )

    bound: dict[str, Any] = {}
    for parameter in definition.entry.parameters:
        if parameter.name not in given:
            if parameter.default is None:
                return {}, (
                    f"{definition.name} needs `{parameter.name}` "
                    f"({parameter.declared_type.value}), so nothing was sent to the "
                    "game."
                )
            bound[parameter.name] = parameter.default.value
            continue
        value, problem = _checked(definition.name, parameter, given[parameter.name])
        if problem:
            return {}, problem
        bound[parameter.name] = value
    return bound, ""


def _checked(
    macro: str, parameter: MacroParameter, value: Any
) -> tuple[Any, str]:
    """인자 하나가 그 선언 타입으로 받을 수 있는 모양인가."""
    declared = parameter.declared_type
    where = f"`{parameter.name}` of {macro}"

    if declared is MacroType.object_:
        return _aim(macro, parameter, value)

    # bool 을 먼저 가린다. Python 에서 `True` 는 int 이기도 하므로, 뒤에 보면 `int` 자리에
    # `True` 가 들어간다.
    if declared is MacroType.bool_:
        if isinstance(value, bool):
            return value, ""
        return None, _mismatch(where, declared, value)
    if isinstance(value, bool):
        return None, _mismatch(where, declared, value)
    if declared is MacroType.int_:
        if isinstance(value, int):
            return value, ""
        return None, _mismatch(where, declared, value)
    if declared is MacroType.float_:
        # int 를 `float` 자리에 받는다. 저장 시점 검사와 같은 규칙이고, wire 가 `3.0` 을
        # `3` 으로 보내는 마당에 `3.0` 을 요구하면 부르는 쪽을 괴롭히기만 한다.
        if isinstance(value, (int, float)):
            return float(value), ""
        return None, _mismatch(where, declared, value)
    if declared is MacroType.string:
        if isinstance(value, str):
            return value, ""
        return None, _mismatch(where, declared, value)
    return None, _mismatch(where, declared, value)  # pragma: no cover


def _aim(macro: str, parameter: MacroParameter, value: Any) -> tuple[Any, str]:
    """`object` parameter 는 selector 하나를 받는다.

    문법이 막는 것은 저장된 정의 안의 좌표와 id 뿐이고, 호출 인자는 별도로 검사해야
    한다. 판별은 `parse_target` 에 맡긴다 — 그 함수가 세 모양을 가리는 유일한 자리이고,
    여기서 정규식을 한 벌 더 쓰면 두 판단이 어긋날 자리가 생긴다. selector 로 읽히는
    것만 통과시킨다.
    """
    where = f"`{parameter.name}` of {macro}"
    if not isinstance(value, str):
        return None, (
            f"{where} names an object, so it takes a selector written as a string. "
            f"{value!r} is not one, and nothing was sent to the game."
        )
    try:
        parsed = parse_target(value)
    except ValueError as error:
        return None, f"{where} is not a selector — {error} Nothing was sent to the game."

    if len(parsed) != 1 or not isinstance(parsed[0], str):
        return None, (
            f"{where} is {value!r}, which reads as a screen point or an instance id. A "
            "macro aims with a selector only: both of those go stale between the run "
            "that wrote the macro and this one, which is why the macro's own syntax has "
            "no way to write them either. Nothing was sent to the game."
        )
    return LateSelector(selector=parsed[0]), ""


def _mismatch(where: str, declared: MacroType, value: Any) -> str:
    return (
        f"{where} is declared `{declared.value}` but was given {value!r}, so nothing "
        f"was sent to the game. The types a macro parameter can have are: "
        f"{DECLARABLE_TYPE_NAMES}."
    )


# --- 결과 ---------------------------------------------------------------------


def _render(result: MacroRunResult) -> str:
    """단계별 결과. 어디까지 갔는지가 이 글의 중심이다."""
    lines: list[str] = []
    if result.failure is None:
        lines.append(
            f"{result.name} ran to the end. "
            f"{len(result.applied)} action(s) reached the game."
        )
    else:
        failure = result.failure
        lines.append(f"{result.name} stopped — {failure.code}: {failure.reason}")
        if result.stopped_at is not None:
            lines.append(f"It stopped at {result.stopped_at}.")
        if len(result.chain) > 1:
            lines.append("Call chain: " + " → ".join(str(one) for one in result.chain))
        observed = failure.payload.get("observed")
        if observed:
            lines.append(
                "What it read there: "
                + ", ".join(f"{name} = {shown}" for name, shown in observed.items())
            )

    lines.append(_places("Reached the game", result.applied))
    lines.append(_places("Did NOT reach the game", result.pending))
    lines.append(_places("Skipped by an `if`", result.skipped))

    if result.outcomes:
        lines.append("What the game said:")
        lines.extend(result.outcomes)

    for flagged in result.flags:
        lines.append(
            f"FLAG at {flagged.place}: {flagged.message}{_observed(flagged.observed)}"
        )
    for asked in result.verdict_requests:
        # 보고하지 않는다. 판정은 시나리오·앞 step·operator 의 말·읽은 knowledge 를 쥔
        # QA agent 의 몫이고, macro 는 그것들을 쥐고 있지 않다.
        lines.append(
            f"NEEDS A VERDICT — scenario step {asked.step}, asked at {asked.place}. "
            f"Expected: {asked.expected}.{_observed(asked.observed)} Judge it yourself "
            "and call `report_step`."
        )
    return "\n".join(line for line in lines if line)


def _places(what: str, places: list[MacroPlace]) -> str:
    if not places:
        return ""
    return f"{what}: " + ", ".join(str(one) for one in places)


def _observed(observed: dict[str, Any]) -> str:
    """감싼 `if` 조건이 읽은 값. 없으면 빈 문장이고, 그래도 항목은 선다."""
    if not observed:
        return ""
    shown = ", ".join(f"{name} = {value!r}" for name, value in observed.items())
    return f" It read: {shown}."
