"""무엇을 하나의 screen 으로 볼지 정하는 규칙을 고치는 도구 둘.

규칙의 문구와 렌더는 `app/agents/qa/screen.py` 가 들고 있다.
"""

from langchain_core.tools import BaseTool, tool

from app.agents.qa.screen import (
    EXCLUDE_SCREEN_SELECTOR_DESCRIPTION,
    INCLUDE_SCREEN_SELECTOR_DESCRIPTION,
    MAX_PATTERN_LENGTH,
    SCREEN_SELECTOR_MATCHES,
    UNCONFIRMED_RULE,
    render_rule_result,
)
from app.agents.qa.tools.tool_context import ToolContext
from app.qa.channel import KnowledgeRequestFailed, QaCancelled, with_operator_messages
from app.qa.envelope import ScreenSelectorEntry, ScreenSelectorRulePayload


def build_screen_selector_tools(ctx: ToolContext) -> list[BaseTool]:
    # ctx 가 든 것을 여기서 되묶는다. 아래 tool 은 `build_tools` 한 함수 안에 있던 것을
    # 그대로 옮긴 것이라, 이 줄이 있어야 본문이 한 글자도 바뀌지 않는다. 읽는 쪽에는
    # 아래 tool 이 무엇을 closure 로 잡는지 먼저 말해 주는 머리말이기도 하다.
    channel = ctx.channel

    async def _write_screen_selector_rule(
        match: str, pattern: str, reason: str, screen_defining: bool
    ) -> str:
        """두 tool 이 지나는 한 자리 (ARTEL-657).

        둘은 `screen_defining` 하나만 다르고, 저장되는 것도 같은 표의 같은 행 모양이다.
        가르면 거절 처리와 결과 문장이 두 벌이 되고, 언제나 한쪽만 고쳐진다.

        **`scene` 을 인자로 받지 않는다.** 목록은 `scene` 단위이고, agent 는 지금 서 있는
        `scene` 에 대해서만 근거를 갖는다 — 인자로 받으면 그 규칙이 모델의 성실함에 걸리고,
        여기서 채우면 구조로 걸린다.

        정규식인지를 여기서 판별하지 않는다. `.*` 는 어떤 selector 와도 글자 그대로 같지
        않으므로 저쪽의 "이 `scene` 에서 본 적 없다" 검사에 그대로 걸리고, 메타문자로
        걸러 보려던 판은 실측 selector 에 `Card(Clone)[37]` 처럼 괄호가 들어 있어 멀쩡한
        항목을 거절했다. `match` 의 의미(index 지우기, 마디 경계)를 여기서 또 구현하는 것도
        같은 이유로 안 한다 — 그 규칙은 이미 Kotlin 과 SQL 에 두 벌 있고, 세 번째 벌이
        어긋나는 순간 화면이 조용히 갈린다.
        """
        scene = (channel.scene.scene or channel.scene.pulse.scene or "").strip()
        if not scene:
            return (
                "The run has not been told which scene it is standing on yet, so "
                "nothing was changed. Observe the scene first."
            )

        kind = (match or "").strip().lower()
        if kind not in SCREEN_SELECTOR_MATCHES:
            return (
                f"{match!r} is not one of {', '.join(SCREEN_SELECTOR_MATCHES)}, so "
                "nothing was changed. Pick one and call this again."
            )

        target = (pattern or "").strip()
        if not target:
            return "`pattern` must name a selector, so nothing was changed."
        if len(target) > MAX_PATTERN_LENGTH:
            return (
                f"`pattern` is longer than {MAX_PATTERN_LENGTH} characters, so "
                "nothing was changed."
            )

        why = (reason or "").strip()
        if not why:
            # 저쪽도 거절한다. 여기서 먼저 거절하는 것은 왕복 하나를 아끼기 위해서이고,
            # 그보다 이 거절이 고칠 수 있는 것이기 때문이다 — 무엇을 봤는지 쓰면 된다.
            return (
                "`reason` is required, so nothing was changed. Write what you saw in "
                "one sentence — an entry nobody can retrace is one nobody can later "
                "decide to remove — and call this again."
            )

        try:
            answer = await channel.write_screen_selector_rule(
                ScreenSelectorRulePayload(
                    scene=scene,
                    entries=[
                        ScreenSelectorEntry(
                            match=kind,
                            pattern=target,
                            screen_defining=screen_defining,
                            reason=why,
                        )
                    ],
                )
            )
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - 지도를 고치다 런이 끝나면 안 된다
            return f"The change could not be sent — {error}. Nothing was changed."

        messages = channel.drain_operator_messages()
        if isinstance(answer, KnowledgeRequestFailed):
            return with_operator_messages(
                f"The content map refused the change — {answer.reason}. Nothing was "
                "changed.",
                messages,
            )
        if answer is None:
            return with_operator_messages(UNCONFIRMED_RULE, messages)
        return with_operator_messages(render_rule_result(answer), messages)

    @tool(description=INCLUDE_SCREEN_SELECTOR_DESCRIPTION)
    async def include_screen_selector(
        step: int, thought: str, match: str, pattern: str, reason: str
    ) -> str:
        # What the agent reads is INCLUDE_SCREEN_SELECTOR_DESCRIPTION, not this.
        #
        # 화면을 안 돌려준다. 이 호출은 게임을 건드리지 않으므로 화면을 실으면 에이전트가
        # 이미 들고 있는 것을 문맥에 한 번 더 사는 것이다 — 지식 tool 들과 같은 판단이다
        # (ARTEL-180). 무엇이 달라졌는지는 다음 관측의 `content map:` 줄에서 보인다.
        return await _write_screen_selector_rule(
            match, pattern, reason, screen_defining=True
        )

    @tool(description=EXCLUDE_SCREEN_SELECTOR_DESCRIPTION)
    async def exclude_screen_selector(
        step: int, thought: str, match: str, pattern: str, reason: str
    ) -> str:
        # What the agent reads is EXCLUDE_SCREEN_SELECTOR_DESCRIPTION, not this.
        return await _write_screen_selector_rule(
            match, pattern, reason, screen_defining=False
        )

    return [include_screen_selector, exclude_screen_selector]
