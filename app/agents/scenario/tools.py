"""The tools the scenario authoring agent drives its search with.

Two tools, handed over on different conditions. `search_test_cases` It mirrors the QA agent's `search_knowledge`
wrapper (`app/agents/qa/tools/knowledge_read_tools.py`) — a per-turn budget, and the
three search
outcomes formatted for the model to act on — over the scenario session's own
channel rather than the QA envelope.

Since ARTEL-319 a session that received the project's test case list gets NO tools:
the cases are already in the prompt, so a search could only return what the agent
is holding, and offering it would spend turns re-finding them. The tool is handed
over only when the test case list is empty — an orchestration that does not send one, or
a non-member session — which is also the rollback path if the test case list is turned
off upstream. Neither this wrapper nor the embedding search behind it is dead
code; it is the branch that carries those sessions.

`list_uncovered_cases` is handed over always, and it is not a search. It answers
which cases no scenario has reached yet — a fact about the project's state, not
about the cases themselves, so holding the whole case list does not answer it.
It is fetched rather than pushed because the answer shrinks as authoring covers
cases: a value sent at session open is wrong by the second turn, and re-sending it
every turn either bloats the turn message or, in the system prompt, throws the
cached case list away. A tool call pays only when someone asks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, tool
from pydantic import ValidationError

from app.agents.scenario.cases import (
    FIND_PATH_DESCRIPTION,
    LIST_UNCOVERED_DESCRIPTION,
    MAX_SEARCHES_PER_RUN,
    RESULT_LIMIT,
    SEARCH_TEST_CASES_DESCRIPTION,
    SUBMIT_SCENARIO_DESCRIPTION,
    TestCaseSearchState,
    render_results,
)
from app.agents.scenario.schemas import ScenarioPlan

if TYPE_CHECKING:
    # Type-only: see app/agents/scenario/cases.py for why app.sessions is not
    # imported at module load.
    from app.sessions.channel import ScenarioChannel


def build_tools(
    channel: ScenarioChannel,
    state: TestCaseSearchState,
    *,
    has_test_case_list: bool = False,
) -> list[BaseTool]:
    """The turn's tools.

    `search_test_cases` is withheld when the agent already holds the test case list.
    Taking it away rather than telling the prompt not to use it is deliberate: a tool
    in reach gets called, and every call it makes there would spend a turn re-finding
    cases already in its context.

    `list_uncovered_cases` is always present. It answers a question the list cannot —
    what has been covered so far — and that answer changes while the session runs.
    """
    # Imported here, not at module load, to keep the agent layer free of an
    # app.sessions import at import time (that would cycle through the service).
    from app.sessions.channel import TestCaseSearchFailed

    @tool(description=SUBMIT_SCENARIO_DESCRIPTION)
    async def submit_scenario(
        title: str,
        description: str,
        steps: list[dict],
        scenario_id: int | None = None,
    ) -> str:
        # agent 가 읽는 것은 SUBMIT_SCENARIO_DESCRIPTION 이지 이 주석이 아니다.
        try:
            plan = ScenarioPlan(
                scenario_id=scenario_id, title=title, description=description, steps=steps
            )
        except ValidationError as err:
            # 시나리오를 거절한 것이 아니라 인자가 틀렸다고 말한다. 저쪽은 이것을 본 적이
            # 없고, "모양을 고쳐라"와 "시나리오를 고쳐라"는 다른 일이다.
            return f"The arguments did not fit the step contract — {err.error_count()} problem(s): {err}"
        answer = await channel.submit_scenario(plan.model_dump(mode="json", by_alias=True))
        if answer is None:
            return (
                "Nobody answered, so this scenario is not kept. Send it again; if it "
                "keeps failing, say so in `message` rather than writing the rest as if "
                "it had been kept."
            )
        if answer.accepted:
            # **"Write the next one" 이라고만 답하지 않는다.** 그 문장은 전량을 청한 요청을
            # 전제로 쓴 것인데, 좁은 요청에서도 똑같이 다음을 재촉했다 — 실측(런 14)에서 청한
            # 하나를 첫 번째로 낸 뒤 마흔둘을 더 썼고, 뒤로 갈수록 제목이 시나리오가 아니라
            # 전제의 곱집합이 됐다(`CompareTag(Me) damage>0 …`). 끝낼 자리를 도구가 주지
            # 않으면 모델은 멈출 근거를 찾지 못한다.
            return (
                f"Kept ({answer.written} so far). Done when every case you judged `in` for this "
                "request sits in some scenario — then stop and answer. Anything past that is "
                "coverage nobody asked for."
            )
        return (
            f"Not kept — {answer.detail or 'no reason given'}. Fix this scenario and send "
            "it again. The ones already kept are untouched; do not resend them."
        )

    @tool(description=LIST_UNCOVERED_DESCRIPTION)
    async def list_uncovered_cases() -> str:
        # What the agent reads is LIST_UNCOVERED_DESCRIPTION, not this.
        #
        # 한 턴에 한 번만 답한다. 턴이 끝나야 저장되므로 두 번째 물음은 같은 것을 돌려주고,
        # 그 왕복은 공짜가 아니다. 두 번 묻는 값이 얼마였는지는
        # `TestCaseSearchState.coverage` 에 적었다.
        if state.coverage is not None:
            return f"{state.coverage} (asked already this turn — this does not change until it ends)"
        answer = await channel.fetch_uncovered()
        if answer is None:
            # 이것만은 안 들고 있는다. 조회는 다음에 될 수 있고, 실패한 조회는 들고 있을
            # 답이 아니다.
            return (
                "Coverage could not be read just now. Say so rather than guessing a "
                "number — a made-up count is worse than no count."
            )
        if not answer.ids:
            state.coverage = (
                "Every case in this project is already carried by some scenario. "
                "That is not a reason to stop: write what was asked for, reusing "
                "whatever cases it needs."
            )
        else:
            by_scene = ", ".join(f"{s.scene} {s.count}" for s in answer.scenes)
            state.coverage = (
                f"{len(answer.ids)} cases are not carried by any scenario yet "
                f"({by_scene}). Ids: {', '.join(str(i) for i in answer.ids)}. "
                "Their wording is in the case list you already hold — quote it rather "
                "than describing the ids."
            )
        return state.coverage

    @tool(description=FIND_PATH_DESCRIPTION)
    async def find_path(from_case_id: int, to_case_id: int) -> str:
        # What the agent reads is FIND_PATH_DESCRIPTION, not this.
        answer = await channel.fetch_path(from_case_id, to_case_id)
        if answer is None:
            return (
                "The route lookup did not answer in time. Do not invent the steps in "
                "between — say in `message` that you could not check the route."
            )
        if answer.ordering == "REVERSED":
            reversed_note = (
                "\nORDER — the other way round they chain directly: the second case's declared "
                "state leads into the first's. This direction costs the bridge steps above. Games "
                "are not always linear and going back may be exactly what was asked for — swap "
                "them only if the request does not depend on this direction."
            )
        elif answer.ordering == "CHAINED":
            # 말해 주는 이유는, 잠자코 있는 것이 서로 다른 두 가지를 뜻했기 때문이다. 적어
            # 보내는 것이 `REVERSED` 뿐이라 "이 순서가 맞다"와 "순서에 대해 말할 것이 없다"가
            # agent 에게는 똑같은 빈 문자열로 닿았고, 이미 들은 것을 확인하려고 같은 두
            # 케이스를 다시 물었다.
            reversed_note = (
                "\nORDER — this order is right: the first case's declared state leads into the "
                "second's. Nothing to reconsider here."
            )
        else:
            reversed_note = (
                "\nORDER — nothing can be said either way: the two cases name no state in common, "
                "so neither order is implied. Asking again will not change this answer."
            )
        if answer.result == "NOT_REQUIRED":
            return (
                "NOT_REQUIRED — nothing goes in between. The two cases follow directly."
                + reversed_note
            )
        if answer.result == "KNOWN":
            lines = "\n".join(
                f"  {i}. {action}"
                + (f"   [input: {answer.inputs[i - 1]}]" if i <= len(answer.inputs) else "")
                for i, action in enumerate(answer.actions, 1)
            )
            return (
                "KNOWN — write each line below as its own bridge step (case_id null), in order:\n"
                f"{lines}{reversed_note}"
            )
        blocked = answer.blocked_by or "unknown"
        if answer.playable:
            # "길이 없다"와 같은 답이 아니다. 그 화면에 서 있으면 값이 저절로 바뀌므로
            # 플레이하면 지나간다 — 무엇을 누르냐고 사용자에게 물으면 있지도 않은 버튼을
            # 물어보는 셈이다.
            return (
                f"PLAYABLE — no operation can be instructed for {blocked}, but a person "
                f"gets through it. {answer.note} Write a bridge step (case_id null) that "
                "says what has to happen there, and do not invent a button to press."
                f"{reversed_note}"
            )
        return (
            f"UNKNOWN — the route is not in the scene spec. Blocking: {blocked}. "
            f"{answer.note} Do not invent steps. Say so in `message`, name what is blocking, "
            f"and ask the user how it is done.{reversed_note}"
        )

    if has_test_case_list:
        # **케이스 목록을 받은 세션은 `find_path` 를 안 쓴다**(ARTEL-772). 목록이 씬의
        # 출구와 그 화면에서 무엇을 누르는지를 케이스마다 싣고 오므로, 사이에 무엇이
        # 들어가는지는 이미 손에 있다. 실측 한 판에서 모델은 14가지를 120번 물었고 그중
        # 13가지는 흐름이 이미 답을 적어 보낸 것이었다 — 왕복이 새로 알려 준 것은 없었다.
        # 도구를 두고 부르지 말라고 적는 것으로는 안 멎는다. 손이 닿으면 부른다.
        return [submit_scenario, list_uncovered_cases]

    @tool(description=SEARCH_TEST_CASES_DESCRIPTION.format(limit=MAX_SEARCHES_PER_RUN))
    async def search_test_cases(query: str, category: str | None = None) -> str:
        # What the agent reads is SEARCH_TEST_CASES_DESCRIPTION, not this.
        if state.searches_attempted >= MAX_SEARCHES_PER_RUN:
            return (
                f"You have used all {MAX_SEARCHES_PER_RUN} case searches this turn. "
                "Build the scenarios from the cases you have already found."
            )

        state.searches_attempted += 1
        answer = await channel.search_test_cases(
            query, (category or "").strip() or None, RESULT_LIMIT
        )
        remaining = MAX_SEARCHES_PER_RUN - state.searches_attempted

        if answer is None:
            return (
                "The case search did not answer in time. Build the scenarios from "
                f"the cases you already have. {remaining} search(es) left."
            )
        if isinstance(answer, TestCaseSearchFailed):
            # Said plainly, with what to do instead: a failed lookup is a side
            # errand that failed, not a reason to invent cases.
            return (
                f"The case search could not run — {answer.reason}. Build the "
                "scenarios from the cases you already have, and note what is "
                f"missing. {remaining} search(es) left."
            )
        return render_results(answer, remaining)

    return [submit_scenario, list_uncovered_cases, search_test_cases, find_path]
