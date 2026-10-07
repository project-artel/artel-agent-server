"""성공한 action 순서를 macro 로 적어 두고 다시 부르는 tool 다섯.

문구는 `app/prompts/qa_run/v19/tool_<name>.md` 다섯 파일에 있고, 문법과 예시와 수명주기
전체는 `skill_macro.md` 에 있다. 다섯 설명이 각각 500자 상한 아래라 매 호출마다 나가는
글은 2.5KB 가 안 되고, 13KB 짜리 본문은 모델이 `load_skill("macro")` 를 부를 때만 간다.
`app/agents/qa/macro/reference.py` 는 그 파일이 `grammar.py` 와 맞는지 재는 기준이다.

수명주기가 네 걸음이다. `write_macro` 로 초안을 쓰고, `read_macro` 로 읽고,
`edit_macro` 로 고치고, `register_macro` 로 부를 수 있게 한다. `run_macro` 는 등록된
것만 부른다. 초안은 런의 상태(`QaRunState.macros`)에 살고 `content_map` 에 안 쓰인다.

**등록된 것은 런을 넘어 산다** (ARTEL-921). `register_macro` 가 `MACRO_REGISTER` 로
`content_map` 에 적고, `read_macro` 와 `run_macro` 는 이 런이 모르는 이름을 만나면
`MACRO_READ` 로 저쪽에 묻는다 — 지난 런이 등록한 것을 이번 런이 부르는 길이 그것이다.

**다섯 중 무엇이 터져도 런은 안 죽는다.** tool 본문이 전부
`_answers_instead_of_raising` 아래에 있어, 새는 예외가 모델이 읽는 문장이 된다. macro
가 멈추는 것은 정상 동작이고 — 수명주기 네 걸음이 있는 이유가 agent 가 실패를 읽고
고치는 것이다 — 그 실패가 런을 죽이면 고칠 기회 자체가 안 온다.

**쓰기가 실패해도 런은 계속 간다.** 저쪽 거절도, 답 없음(`None`)도, 새는 예외도 전부
모델이 읽는 문장으로 바뀌고, 등록은 이 런의 `MacroBook` 에 그대로 남아 `run_macro` 가
부를 수 있다. `capability_tools.py` 의 `_write_capability` 가 선례이고, 특히 `None` 을
실패로 옮기지 않는 이유도 같다 — 이 프레임을 모르는 구버전 orchestration 은 라우터에서
프레임을 떨어뜨리고 거절이 안 돌아오는데, 그때 "안 됐다" 고 하면 모델이 같은 정의를
계속 다시 보낸다.
"""

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.qa.macro.binding import LateSelector, MacroMemories
from app.agents.qa.macro.book import RegisteredMacro
from app.agents.qa.macro.errors import MacroFailure, MacroRejection
from app.agents.qa.macro.grammar import DECLARABLE_TYPE_NAMES, MacroType
from app.agents.qa.macro.model import MacroDefinition, MacroParameter
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.macro.session import MacroSession
from app.agents.qa.macro.runner import (
    MacroPlace,
    MacroRunResult,
    run_macro as execute_macro,
)
from app.agents.qa.tools.action_tools import parse_target
from app.agents.qa.tools.state import QaRunState
from app.agents.qa.tools.tool_context import ToolContext
from app.prompts import load_tool_description
from app.qa.acting import ActionOutcome
from app.qa.channel import KnowledgeRequestFailed, QaCancelled, with_operator_messages
from app.qa.envelope import MacroReadPayload, MacroRegisterPayload


def _answers_instead_of_raising(
    body: Callable[..., Awaitable[str]],
) -> Callable[..., Awaitable[str]]:
    """tool 본문에서 새는 예외를 모델이 읽는 문장으로 바꾼다.

    **어느 경우에도 런이 안 죽는다.** `capability_tools._write_capability` 가 같은
    규율을 같은 이유로 적어 두었다 — macro tool 하나가 터졌다고 시나리오가 멈추면 이
    다섯은 런이 지는 위험이지 보태는 것이 아니다.

    종전에는 guard 가 orchestration 왕복 둘(`_store`·`_stored`)에만 있었고 tool 본문은
    맨몸이었다. 그래서 런 하나가 실제로 여기서 죽었다 — `COMPARISON_REJECTED` 로 멈춘
    macro 의 결과를 그리다 `'str' object has no attribute 'items'` 가 tool 밖으로 나갔고,
    agent 는 아무것도 못 하고 런이 끝났다.

    **macro 가 멈추는 것은 정상 동작이다.** tool 다섯이 있는 이유가 agent 가 그 실패를
    읽고 `edit_macro` 로 고쳐 다시 등록하는 것인데, 실패가 런을 죽이면 고칠 기회 자체가
    안 온다.

    `QaCancelled` 는 통과시킨다. operator 가 런을 끝낸 것이라 문장으로 바꿀 일이 아니다.
    """

    @wraps(body)
    async def answering(*arguments: Any, **keywords: Any) -> str:
        try:
            return await body(*arguments, **keywords)
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - macro tool 이 런을 끝내면 안 된다
            return (
                f"`{body.__name__}` hit an error inside the server and could not "
                f"finish: {error!r}. This is a defect in the server, not in the game "
                "and not in your macro, so nothing about the game follows from it. "
                "Anything the macro already sent has been sent and cannot be taken "
                "back. Observe the scene to see where you are, and carry on without "
                "this call."
            )

    return answering


def build_macro_tools(ctx: ToolContext) -> list[BaseTool]:
    # 아래 tool 이 closure 로 잡는 것. 다시 bind 하는 이유는 `tool_context.py` 에 있다.
    channel, state = ctx.channel, ctx.state

    class _Host:
        """runner 가 바깥에 대고 하는 일 둘을 `ToolContext` 에 잇는다.

        runner 가 `ToolContext` 를 직접 받지 않는 이유는 `app/agents/qa/macro/runner.py`
        에 적혀 있다 — 그쪽이 `tools` 를 import 하면 순환이 된다. 그 잇는 자리가 여기
        하나다.

        `ctx.run` 이 아니라 `ctx.act` 다. runner 는 돌아온 값을 **보고** 멈출지 정하는데
        `run` 은 모델이 읽는 문장만 내므로, 그것으로는 문장을 다시 파싱하는 수밖에 없다.
        """

        def __init__(self, session: MacroSession) -> None:
            self.session = session

        async def run(self, actions, summary: str, step: int) -> ActionOutcome:
            return await ctx.act(actions, summary, step)

        def memories(self) -> MacroMemories:
            return MacroMemories(scene=channel.scene)

        async def checkpoint(self, result: MacroRunResult) -> bool:
            # 턴을 돌려주는 자리다. `run_macro` 나 `resume_macro` 가 `settle` 로 이것을
            # 알아채고 답을 돌려준 뒤, agent 가 `resume_macro` 를 부를 때까지 여기 선다.
            return await self.session.pause(result)

    async def _drive(session: MacroSession) -> str:
        """macro 가 멈춰 서거나 끝날 때까지 기다리고, 그 자리를 글로 낸다.

        멈췄으면 그 session 을 런의 상태에 둔다. `resume_macro` 가 거기서 찾는다.
        """
        result, paused = await session.settle()
        state.paused_macro = session if paused else None
        return ctx.answer(_render(result), channel.drain_operator_messages())

    def _standing_screen() -> str:
        """등록하는 순간 agent 가 서 있는 `screen.id`. 모르면 빈 문자열.

        **`scene` 이름이 아니라 `screen.id` 다.** 저쪽의 `MACRO_REGISTER.screens` 는
        `screen.id` 를 JSON 문자열로 받고(`CAPABILITY_VERDICT.screen_id` 와 같은 규약),
        이 build 의 `content_map` 에 없는 값을 통째로 거절한다 —
        `references screens outside this build's content map`.

        출처는 지도가 이 런에 대해 마지막으로 한 말 하나다(`ScreenMap.verdict`,
        ARTEL-668). 그 판정이 지금 서 있는 `scene` 의 것이 아니면 안 쓴다 — 게임은
        `Battle` 과 `Battle 2` 를 둘 다 가질 수 있고, 옆 `scene` 의 화면 번호를 이
        macro 에 달면 그 관계는 거짓이다. `ScreenMap.render` 가 이름을 정확히 맞대는
        것과 같은 판단이다.

        빈 문자열이 정상이다. `scene` 이 아직 안 굳었거나 `SCREEN_SETTLED` 가 한 장도
        안 온 빌드에서는 끝까지 비어 있고, 그때 macro 는 관계 없이 등록된다.
        """
        verdict = channel.scene.screen_map.verdict
        standing = (channel.scene.scene or channel.scene.pulse.scene or "").strip()
        if verdict is None or not standing or verdict.scene != standing:
            return ""
        return verdict.screen_id.strip()

    def _named_screens_problem(named: list[str]) -> str | None:
        """agent 가 지목한 `screens` 가 `screen.id` 가 아니면 무엇을 적어야 하는지.

        저쪽도 거절하지만 그 거절은 **등록 전체를 버린다** — `screens` 하나가 숫자가
        아니면 macro 행도 안 적힌다. 여기서 먼저 막는 것은 왕복 하나를 아끼려는 것이
        아니라, `scene` 이름을 적는 흔한 실수 때문에 멀쩡한 정의가 저장되지 않는 것을
        막으려는 것이다.
        """
        wrong = [one for one in named if not one.isdigit()]
        if not wrong:
            return None
        return (
            f"`screens` takes screen ids, and {', '.join(wrong)} "
            f"{'is not one' if len(wrong) == 1 else 'are not'}, so nothing was "
            "registered. A screen id is the number in the `content map: you are on "
            "screen <id>` line of your scene view — not a scene name. Leave `screens` "
            "out to relate the macro only to the screen you are standing on."
        )

    async def _store(definition: MacroDefinition, screens: tuple[str, ...]) -> str:
        """등록된 정의를 `content_map` 에 적고, 답을 모델이 읽는 문장으로 옮긴다.

        **어느 경우에도 런이 안 죽는다.** 저쪽은 거절을 값으로 돌려주고, 그래도 새는
        예외는 여기서 문장으로 바뀐다 — `capability_tools._write_capability` 와 같은
        규율이다.

        `None` 을 실패로 옮기지 않는다. 이 프레임을 모르는 구버전 orchestration 은
        라우터에서 프레임을 떨어뜨리고 그 거절이 이 소켓으로 안 돌아오는데, 그때
        "안 됐다" 고 하면 모델이 같은 정의를 계속 다시 보낸다.

        어느 문장이 돌아와도 이 런의 `MacroBook` 에는 이미 등록돼 있다. 부르는 쪽이
        그것을 먼저 하는 이유는 저쪽이 뭐라고 답하든 `run_macro` 가 이번 런 안에서는
        이 macro 를 부를 수 있어야 하기 때문이다.
        """
        payload = MacroRegisterPayload(
            name=definition.name,
            source=definition.source,
            definition=definition.model_dump(mode="json"),
            parameters=[one.name for one in definition.entry.parameters],
            screens=list(screens),
        )
        try:
            answer = await channel.register_macro(payload)
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - 지도를 적다 런이 끝나면 안 된다
            return (
                f"It could not be sent to the content map — {error}. It is registered "
                "for this run only, so it will be gone when the run ends."
            )

        if isinstance(answer, KnowledgeRequestFailed):
            return (
                f"The content map refused to store it — {answer.reason}. It is "
                "registered for this run only, so it will be gone when the run ends. "
                "This says nothing about the game; carry on with the step."
            )
        if answer is None:
            return (
                "It was sent to the content map, which did not answer, so whether it "
                "is stored beyond this run cannot be confirmed. Do not register it "
                "again for that reason — it is callable either way."
            )
        where = (
            ", ".join(answer.screen_ids)
            if answer.screen_ids
            else "no screen yet"
        )
        kept = "stored in the content map" if answer.created else "updated in place"
        return (
            f"It is {kept}, so later runs can call it too. Screens related to it: "
            f"{where}."
        )

    async def _stored(macro_name: str) -> tuple[RegisteredMacro | None, str]:
        """이 런이 모르는 이름을 `content_map` 에 묻고, 찾으면 이 런의 책에 들인다.

        `read_macro` 와 `run_macro` 가 같은 이 경로를 쓴다. 지난 런이 등록한 macro 를
        이번 런이 보는 길이 이것 하나다.

        **돌아온 `source` 를 다시 파싱한다.** `definition` tree 를 그대로 모델로 읽지
        않는 이유는 `macro/model.py` 가 정의를 만드는 길을
        `macro_definition_from_source` 하나로 못박았기 때문이고, 다시 파싱하면 저장된
        뒤에 좁아진 허용 목록도 실행 **전에** 걸린다 — 실행 한복판에서 처음 거절되면
        이미 나간 action 을 되돌릴 수 없다.

        실패 셋이 전부 문장이다. 예외도 여기서 멈춘다 — macro 를 하나 못 찾은 것이 런을
        끝낼 이유가 아니다.
        """
        try:
            answer = await channel.read_macro(MacroReadPayload(name=macro_name))
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - 조회 하나로 런이 끝나면 안 된다
            return None, f"The content map could not be asked — {error}."

        if isinstance(answer, KnowledgeRequestFailed):
            return None, f"The content map does not have it — {answer.reason}."
        if answer is None:
            return None, (
                "The content map did not answer, so whether it has one by that name "
                "is unknown here."
            )

        definition, problem = _parsed(answer.name or macro_name, answer.source)
        if definition is None:
            return None, (
                f"The content map has a macro called {macro_name}, but it no longer "
                f"parses, so it cannot be called: {problem}"
            )
        return state.macros.adopt(definition, tuple(answer.screen_ids)), ""

    def _parsed(name: str, source: str) -> tuple[MacroDefinition | None, str]:
        """파싱해 보고 결과를 말한다. 등록하지도, 실행하지도 않는다."""
        try:
            return macro_definition_from_source(name, source), ""
        except MacroRejection as rejection:
            return None, rejection.render()

    @tool(description=load_tool_description("write_macro").body)
    @_answers_instead_of_raising
    async def write_macro(step: int, thought: str, name: str, source: str) -> str:
        # What the agent reads is `qa_run/<version>/tool_write_macro.md`, not this.
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

    @tool(description=load_tool_description("edit_macro").body)
    @_answers_instead_of_raising
    async def edit_macro(
        step: int, thought: str, name: str, old_text: str, new_text: str
    ) -> str:
        # What the agent reads is `qa_run/<version>/tool_edit_macro.md`, not this.
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

    @tool(description=load_tool_description("read_macro").body)
    @_answers_instead_of_raising
    async def read_macro(step: int, thought: str, name: str) -> str:
        # What the agent reads is `qa_run/<version>/tool_read_macro.md`, not this.
        #
        # 이 런이 모르는 이름이면 `content_map` 에 묻는다. 그것이 지난 런이 등록한
        # macro 를 이번 런이 읽는 유일한 길이다(ARTEL-921).
        macro_name = (name or "").strip()
        if not macro_name:
            return "`name` must say which macro to read, so nothing was looked up."

        source = state.macros.source(macro_name)
        if source is None:
            found, problem = await _stored(macro_name)
            messages = channel.drain_operator_messages()
            if found is None:
                known = ", ".join(sorted(_known(state))) or "none yet"
                return with_operator_messages(
                    f"There is no macro called {macro_name} in this run. {problem} "
                    f"This run knows: {known}.",
                    messages,
                )
            state.macros.remember_read(macro_name)
            return with_operator_messages(
                f"{macro_name} (registered in the content map by an earlier run):"
                f"\n\n{found.definition.source}",
                messages,
            )

        state.macros.remember_read(macro_name)
        where = (
            "draft" if state.macros.draft(macro_name) is not None else "registered"
        )
        return f"{macro_name} ({where}):\n\n{source}"

    @tool(description=load_tool_description("register_macro").body)
    @_answers_instead_of_raising
    async def register_macro(
        step: int, thought: str, name: str, screens: list[str] = []
    ) -> str:
        # What the agent reads is `qa_run/<version>/tool_register_macro.md`, not this.
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

        named = [one.strip() for one in (screens or []) if one and one.strip()]
        problem = _named_screens_problem(named)
        if problem is not None:
            # 초안은 그대로다. 거절은 아무 자취도 남기지 않는다.
            return problem

        standing = _standing_screen()
        relations = tuple([standing] if standing else []) + tuple(named)
        # **저쪽에 보내기 전에 이 런의 책에 넣는다.** 저쪽이 거절하든 답이 없든
        # `run_macro` 는 이번 런 안에서 이 macro 를 부를 수 있어야 한다.
        registered = book.register(definition, relations)

        lines = [f"{macro_name} is registered. {_signature(definition)}"]
        if not registered.screens:
            # 빈 관계는 아직 어디서 쓸지 모른다는 뜻이고, 서 있는 `screen` 을 모르는
            # 경우와 맞는다.
            lines.append(
                "It is related to no screen, because the run does not yet know which "
                "one it is standing on. Register it again from a settled screen, or "
                "name screens yourself, to record where it is used."
            )
        # 이번에 지목한 것만이 아니라 **이 런이 아는 관계 전부**를 보낸다. 앞선 쓰기가
        # 저쪽에 안 닿았으면(구버전 orchestration·타임아웃·거절) 그때 지목한 `screen` 이
        # 저쪽에 없고, 이번에 이번 것만 보내면 그것이 영영 빠진다 — 관계를 빼는 길이 v1
        # 에 없다고 적어 둔 것과 어긋난다.
        lines.append(await _store(definition, registered.screens))
        lines.append(
            "Nothing has run. `run_macro` is what calls it."
        )
        return with_operator_messages(
            "\n".join(lines), channel.drain_operator_messages()
        )

    @tool(description=load_tool_description("run_macro").body)
    @_answers_instead_of_raising
    async def run_macro(
        step: int, thought: str, name: str, arguments: dict[str, Any] = {}
    ) -> str:
        # What the agent reads is `qa_run/<version>/tool_run_macro.md`, not this.
        #
        # 위 `screens` 와 같은 이유로 빈 사전 리터럴이고, 읽기만 한다.
        #
        # 이슈는 이 인자를 `args` 라고 적는데 그 이름을 쓸 수 없다. langchain 이 함수
        # 서명에서 argument schema 를 떠낼 때 pydantic 이 `args` 를 내부 이름과
        # 부딪히는 것으로 보고 `v__args` 로 바꾸므로, 호출이 통째로 깨진다.
        macro_name = (name or "").strip()
        if not macro_name:
            return "`name` must say which macro to call, so nothing was sent to the game."
        if state.paused_macro is not None:
            # 둘을 같이 두면 게임을 모는 쪽이 둘이 된다. 멈춘 것을 먼저 정리하게 한다.
            paused = state.paused_macro
            return (
                f"{paused.name} is paused at a checkpoint, so nothing was sent. Call "
                "`resume_macro` first — `proceed: true` carries it on from where it "
                "stopped, `proceed: false` drops it without sending anything else."
            )

        registered = state.macros.registered(macro_name)
        if registered is None and state.macros.draft(macro_name) is not None:
            return (
                f"{macro_name} is only a draft, so it cannot be called. "
                "`register_macro` first."
            )
        problem = ""
        if registered is None:
            # 이 런이 등록하지 않은 이름이다. 지난 런이 `content_map` 에 등록해 둔 것일
            # 수 있고, `read_macro` 와 같은 경로로 가져온다(ARTEL-921).
            registered, problem = await _stored(macro_name)
        if registered is None:
            known = ", ".join(sorted(state.macros.registrations)) or "none yet"
            # 게임에 아무것도 안 보냈으므로 화면을 안 싣는다. `_stored` 가 왕복 하나를
            # 기다렸을 수 있어 그동안 온 operator 의 말만 붙인다.
            return with_operator_messages(
                f"There is no registered macro called {macro_name}. {problem} "
                f"Registered in this run: {known}.",
                channel.drain_operator_messages(),
            )

        definition = registered.definition
        bound, problem = _bind_arguments(definition, arguments or {})
        if problem:
            # 게임에 아무것도 보내기 전이다. 첫 statement 를 처리하기 전에 끝난다.
            return problem

        session = MacroSession(macro_name, step)
        session.start(execute_macro(_Host(session), definition, bound, step))
        return await _drive(session)

    @tool(description=load_tool_description("resume_macro").body)
    @_answers_instead_of_raising
    async def resume_macro(step: int, thought: str, proceed: bool = True) -> str:
        # What the agent reads is `qa_run/<version>/tool_resume_macro.md`, not this.
        session = state.paused_macro
        if session is None:
            return (
                "No macro is paused at a checkpoint, so there is nothing to resume. "
                "`run_macro` starts one."
            )
        if step != session.step:
            # macro 하나가 시나리오 step 하나에 속한다. 이어 도는 action 도 그 step 에
            # 적히므로, 다른 step 으로 이으라는 말은 받지 않는다.
            return (
                f"{session.name} was called for scenario step {session.step}, so it "
                f"resumes as step {session.step}, not {step}. Nothing was sent."
            )
        state.paused_macro = None
        session.resume(proceed)
        return await _drive(session)

    # `@tool` 이 이름을 함수에서 가져가므로 tool 이 자기 이름과 어긋난 문자열 아래
    # 등록될 일이 없다. 그래서 runner 의 같은 이름 함수를 `execute_macro` 로 받는다 —
    # 뒤에서 `.name` 을 고치면 langchain 이 함수에서 떠낸 argument schema 와 어긋난다.
    return [write_macro, edit_macro, read_macro, register_macro, run_macro, resume_macro]


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
    if result.paused_at is not None:
        # 실패가 아니다. 저자가 "여기서 한 번 보고 가라" 고 찍은 자리다.
        reason = f" — {result.paused_reason}" if result.paused_reason else ""
        lines.append(
            f"{result.name} PAUSED at a checkpoint, {result.paused_at}{reason}. This is "
            "not a failure: the macro's author asked you to look here before it goes "
            "on. Nothing more is sent until you call `resume_macro` — `proceed: true` "
            "carries it on from this line with everything it bound, `proceed: false` "
            "stops it here."
        )
    elif result.abandoned_at is not None:
        lines.append(
            f"{result.name} stopped at the checkpoint {result.abandoned_at}, as you "
            "asked. This is not a failure, and nothing after the checkpoint was sent."
        )
    elif result.failure is None:
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
        # 두 칸을 따로 그린다. 종전에는 둘 다 `observed` 였는데 `runner` 쪽은 dict 를,
        # `binding` 쪽은 문자열 하나를 실어서, `COMPARISON_REJECTED` 가 나는 순간 여기
        # `.items()` 가 터지고 **런이 통째로 죽었다.** 뜻이 다르니 이름이 다르다 —
        # `observed` 는 조건이 읽은 호출들이고, `arrived` 는 비교에 도착한 값 하나다.
        #
        # 그래도 `isinstance` 를 둔다. `payload` 는 코드마다 싣는 것이 달라 사전이고,
        # 여기서 한 번 더 막는 값이 다음에 같은 자리에서 런을 죽이는 것을 막는다.
        observed = failure.payload.get("observed")
        if isinstance(observed, dict) and observed:
            lines.append(
                "What it read there: "
                + ", ".join(f"{name} = {shown}" for name, shown in observed.items())
            )
        arrived = failure.payload.get("arrived")
        if arrived:
            lines.append(f"The value that arrived there: {arrived}")

    lines.append(_places("Reached the game", result.applied))
    not_yet = "Not sent yet" if result.paused_at is not None else "Did NOT reach the game"
    lines.append(_places(not_yet, result.pending))
    lines.append(_places("Skipped by an `if` or a loop that ran no passes", result.skipped))
    if any(place.passes for place in result.applied + result.pending):
        # 반복이 있으면 번호는 적힌 줄이고, 몇 회째인지는 괄호에 있다. `pending` 은 적힌
        # 줄을 한 번씩만 든다 — 반복의 남은 회는 거기 따로 안 적힌다.
        lines.append(
            f"{result.executed} statement(s) ran in all. A step number is the written "
            "line; `(pass N)` says which turn of the loop around it. A loop's remaining "
            "passes are not listed one by one."
        )

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
    if len(result.verdict_requests) > 1:
        # 반복 안의 `ask_verdict` 는 회마다 선다. 판정할 step 은 하나다 — macro 를 부른
        # step 이다. 회마다 따로 판정하라는 뜻으로 읽히면 같은 step 을 여러 번 보고한다.
        lines.append(
            f"These {len(result.verdict_requests)} requests are all about scenario step "
            f"{result.verdict_requests[0].step}: read them together and answer that step "
            "with one `report_step`."
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
