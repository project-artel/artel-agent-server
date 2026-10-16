"""질문 갈래 — 서비스 전반을 리소스 도구로 읽으며 답한다 (ARTEL-1086, POC).

저작이 아닌 질문("이 TC 는 이전 버전 거야?", "이 TC 왜 실패했어?", "어제 QA 어땠어?")은 케이스 목록만으로 답할 수
없다. 답이 버전 · 판정 · QA 실행 기록에 흩어져 있기 때문이다. 질문마다 도구를 따로 만들지 않고, 엔터티를 따라가는
세 도구를 준다 — `find_resource`(말로 찾기) · `get_resource`(하나 자세히) · `related_resources`(관계 따라가기).
무엇을 읽을지는 모델이 고르고, 범위(프로젝트 · TR 버전)는 Orchestration 이 세션에서 정한다.

답은 다른 갈래와 같은 두 칸이다 — 한 줄 결론(`result`)과 근거(`detail`). TC · TS 는 도구가 준 표식(`[[tc:N]]`)으로
가리키고, 도구 결과에 없던 표식은 지운다(지어낸 번호가 칩이 되지 않게).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel, Field

from app.agents.base import AgentContext
from app.agents.scenario import trace
from app.agents.scenario.schemas import AgentReply, Ref, ScenarioAgentRequest, ScenarioAgentResult
from app.llm.chat_model import build_chat_model

if TYPE_CHECKING:
    from app.sessions.channel import ScenarioChannel

logger = logging.getLogger(__name__)

_MARKER = re.compile(r"\[\[(tc|ts|tr|qa|issue|build):(-?\d+)\]\]")
# 대화 기록은 표식을 "@TC 이름" 으로 적는다. 모델이 그것을 보고 답에 "@TC" 를 따라 쓰면 칩이 아니라 글자로 남는다.
_SPOKEN_PREFIX = re.compile(r"@(?:TC|TS|TR|QA|ISSUE|BUILD)\s*", re.IGNORECASE)

# 한 질문의 천장. 관계를 몇 번 따라가면 답이 나오는 것이 정상이고, 그보다 길면 헛돌고 있는 것이다.
# 실측(TR 99, "최근 QA 결과 요약")에서 28 걸음 한도에 걸려 턴이 죽었다 — 걸음은 도구 호출의 두 배 남짓에 답 도구가
# 더해진다. 넉넉히 두고, 넘으면 죽지 않고 읽은 데까지로 답한다.
MAX_TOOL_CALLS = 20


class QuestionAnswer(BaseModel):
    """질문 답의 두 칸."""

    result: str = Field(description="질문에 대한 한 줄 결론. 사용자가 가장 먼저 읽는다.")
    detail: str = Field(
        default="",
        description="결론의 근거. 도구에서 읽은 사실만, 짧은 마크다운으로. TC · TS 는 도구가 준 ref 표식 그대로.",
    )


_SYSTEM_KO = """너는 게임 QA 서비스 Artel 의 도우미다. 사용자의 질문에 서비스 데이터를 읽어 답한다.

## 도구
- find_resource(kind, text): 말로 찾는다. kind = case · scenario · test_run · qa_run · build · issue.
  case 는 뜻으로도 찾으니 사용자가 한 말을 그대로 넣어도 된다.
- get_resource(kind, id): 하나를 자세히 읽는다. kind = case · scenario · test_run · qa_run · qa_try · build.
  "이 테스트 런" 은 get_resource(kind="test_run") 에 id 를 주지 않으면 읽힌다.
- related_resources(kind, id, relation): 관계를 따라간다. 결과의 `relations` 에 있는 이름만 쓴다.
  - case: versions(빌드별 내용 · 판정 · 이 TR 버전과 다른 칸), verdicts(QA 판정 · 실패 이유 · 앞서 막힌 스텝), scenarios
  - scenario: cases, test_runs / test_run: scenarios, qa_runs / qa_run: tries / qa_try: issues

## 범위
- 이 대화는 한 프로젝트의 한 테스트 런(TR) 안에서 열렸다. **TC 는 기본적으로 이 TR 의 게임 버전 기준으로만 말한다.**
  find_resource(case) 도 기본은 이 버전 안에서만 찾는다.
- 사용자의 의도가 이번 버전이 아닐 때만 다른 버전을 본다 — "1.0 에서는", "예전 버전", "어느 버전 거야", "이번 버전에도
  있어?"처럼 버전을 견주거나 다른 버전을 가리킬 때. 그때 find_resource(case, all_versions=true) 로 넓히고, 답에 그 케이스가
  어느 버전 것인지 밝힌다. 버전을 말하지 않은 질문에 다른 버전 케이스를 섞지 않는다.
- 다른 프로젝트의 것은 볼 수 없다. 사용자가 요청해도 이 프로젝트 밖의 것은 볼 수 없다고 답한다.

## 답하는 법
- 추측하지 말고 도구로 확인한 것만 말한다. 찾지 못했으면 찾지 못했다고 말한다.
- 사용자가 말하는 "TC 3" 같은 번호는 화면의 시나리오 안 순번이지 케이스 id 가 아니다. 그 번호로 get 하지 않는다.
  시나리오를 get 하면 스텝마다 `tc_no`(화면의 TC 번호)와 `case_ref` 가 있다 — 어느 시나리오인지 알면 그것으로 짚고,
  모르면 내용으로 찾거나 어느 시나리오인지 되묻는다.
- id 는 도구 결과에서 얻은 것만 쓴다.
- 사용자가 TC 번호를 몰라도 된다 — 말한 내용으로 find_resource 를 먼저 부른다.
- "지금까지 · 전체 중에서" 처럼 여러 실행을 견주는 질문은 이 테스트 런이 아니라 find_resource(qa_run) 로 프로젝트 전체를 본다.
  qa_run 결과의 `failed_verifications` 가 실패한 검증 수다.
- 케이스에 `version_note` 가 있으면 그 말대로 답한다(어느 버전에도 없는 케이스를 "이전 버전" 이라 하지 않는다).
- 최신 빌드는 `latest_game_version: true` 인 것이다. editor 빌드는 게임 버전이 아니다.
- find 결과가 여럿이면 질문에 맞는 것을 모두 확인한다. 하나만 보고 "없다" 고 단정하지 않는다.
- 실패 이유를 물으면 verdicts 를 읽는다. `earlier_failed_steps` 가 있으면 앞 스텝이 막혀 확인 자체를 못 한 것일 수 있다
  — 게임 결함인지 사전조건이 안 선 것인지를 나눠 말한다.
- 무엇을 가리킬 때는 결과의 `ref` 표식을 그대로 쓴다 — 케이스 [[tc:N]], 시나리오 [[ts:N]], 테스트 런 [[tr:N]], QA 실행
  [[qa:N]], 이슈 [[issue:N]], 빌드 [[build:N]]. 화면이 이름 칩으로 그리고 누르면 그 페이지로 간다. 표식을 지어내지 않는다.
- **번호를 글로 쓰지 않는다.** "TC 411", "케이스 359", "이슈 #9" 처럼 쓰지 말고 표식만 쓴다. "@" 도 쓰지 않는다.
- 케이스를 설명할 때는 사전조건과 기대 결과를 각각 한 줄로, 줄 머리에 `사전조건:` · `기대 결과:` 를 붙여 쓴다.
- **읽기 쉽게.** 케이스 이름은 길다. 칩 뒤에 같은 이름을 되풀이하지 않는다. 케이스가 2개 이상이면 목록을 늘어놓지 말고 표로
  쓴다 — 열은 `케이스 | 사전조건 | 기대 결과`(필요하면 `판정`). **표 한 줄에는 케이스 하나만** 넣는다(한 칸에 칩 둘을 넣지
  않는다). 표 칸 안에는 `사전조건:` · `기대 결과:` 머리말을 쓰지 않는다 — 열 이름이 이미 그것이다. 묻지 않은 것을 덧붙이지 않는다.
- result 는 한 줄 결론, detail 은 근거를 짧게. 한국어 해요체로 답한다."""

_SYSTEM_EN = _SYSTEM_KO.replace("한국어 해요체로 답한다.", "Answer in English.")


async def run_question_workflow(
    request: ScenarioAgentRequest,
    context: AgentContext,
    channel: ScenarioChannel,
) -> ScenarioAgentResult:
    found: dict[tuple[str, int], Ref] = {}
    calls: list[str] = []

    async def ask(payload: dict) -> str:
        calls.append(json.dumps(payload, ensure_ascii=False))
        # 대기 중 화면에 지금 무엇을 읽는지 한 줄로 보인다(ARTEL-1086).
        await channel.report(_stage_of(payload))
        answer = await channel.query_resource(payload)
        if answer is None:
            return json.dumps({"note": "조회가 시간 안에 답하지 않았다."}, ensure_ascii=False)
        _remember(found, answer.get("items", []))
        return json.dumps(answer, ensure_ascii=False, default=str)

    @tool
    async def find_resource(kind: str, text: str = "", limit: int = 10, all_versions: bool = False) -> str:
        """Find resources by words. kind: case | scenario | test_run | qa_run | build | issue.
        Cases are searched in this test run's version only; set all_versions=true only when the user asks about another version."""
        return await ask({"op": "find", "kind": kind, "text": text, "limit": limit, "all_versions": all_versions})

    @tool
    async def get_resource(kind: str, id: int | None = None) -> str:  # noqa: A002 - the contract field is `id`
        """Read one resource in detail. kind: case | scenario | test_run | qa_run | qa_try | build.
        Omit id with kind=test_run to read the test run this conversation belongs to."""
        payload: dict = {"op": "get", "kind": kind}
        if id is not None:
            payload["id"] = id
        return await ask(payload)

    @tool
    async def related_resources(kind: str, id: int, relation: str, limit: int = 10) -> str:  # noqa: A002
        """Follow a relation listed in a result's `relations`."""
        return await ask({"op": "related", "kind": kind, "id": id, "relation": relation, "limit": limit})

    from app.agents.scenario.agent import limit_middleware

    agent = create_agent(
        model=build_chat_model(request.model, request.reasoning),
        tools=[find_resource, get_resource, related_resources],
        system_prompt=_SYSTEM_KO if request.locale.value == "ko" else _SYSTEM_EN,
        response_format=ToolStrategy(schema=QuestionAnswer),
        middleware=limit_middleware(),
    )
    messages = [*request.history, HumanMessage(request.user_input)]
    try:
        state = await agent.ainvoke(
            {"messages": messages},
            {**context.trace_config("scenario-question"), "recursion_limit": MAX_TOOL_CALLS * 2 + 6},
        )
    except GraphRecursionError:
        # 헛돈 질문이 턴을 죽이지 않게 한다. 읽은 것을 지어 붙이지 않고 다 읽지 못했다고 말한다.
        logger.warning("[scenario] question hit the step ceiling after %d resource calls", len(calls))
        state = {"structured_response": QuestionAnswer(
            result="질문에 답할 만큼 다 읽지 못했어요. 범위를 좁혀 다시 물어봐 주세요.",
            detail="예: TC 번호, 테스트 런 이름, QA 실행 번호를 함께 말해 주세요.",
        )}
    answer = state.get("structured_response")
    if not isinstance(answer, QuestionAnswer):
        last = state["messages"][-1].content if state.get("messages") else ""
        answer = QuestionAnswer(result=str(last)[:300] or "답을 만들지 못했습니다.")

    result = _checked(answer.result, found)
    detail = _checked(answer.detail, found)
    used = {key: ref for key, ref in found.items() if _MARKER_KEY(key) in result + detail}
    trace.record(
        request.run_id, "질문에 답했다 (리소스 도구)",
        f"도구 {len(calls)}번\n" + "\n".join(calls) + f"\n결론: {result}",
    )
    logger.info("[scenario] question answered with %d resource calls", len(calls))
    return ScenarioAgentResult(
        message=_spoken("\n".join(part for part in (result, detail) if part), used),
        scenarios=[],
        reply=AgentReply(result=result, detail=detail),
        refs=list(used.values()),
    )


def _remember(found: dict[tuple[str, int], Ref], item: object) -> None:
    """도구가 돌려준 표식을 이름과 함께 기억한다. 결과 안쪽(실패 스텝의 케이스 등)의 `{ref, label}` 도 찾아 들어간다.
    답의 표식은 이 안에 있는 것만 남긴다."""
    if isinstance(item, list):
        for each in item:
            _remember(found, each)
        return
    if not isinstance(item, dict):
        return
    match = _MARKER.fullmatch(str(item.get("ref") or ""))
    if match is not None and item.get("label"):
        kind, ref_id = match.group(1), int(match.group(2))
        fields = item.get("fields") or {}
        detail = fields.get("expected") if kind == "tc" else fields.get("description")
        # 위쪽(항목 자체)에서 본 이름이 안쪽 요약보다 낫다 — 먼저 본 것을 지킨다.
        found.setdefault((kind, ref_id), Ref(kind=kind, id=ref_id, label=str(item["label"]), detail=detail or None))
    for value in item.values():
        if isinstance(value, (dict, list)):
            _remember(found, value)


def _MARKER_KEY(key: tuple[str, int]) -> str:  # noqa: N802 - reads as the marker it builds
    return f"[[{key[0]}:{key[1]}]]"


def _checked(text: str, found: dict[tuple[str, int], Ref]) -> str:
    """도구에서 본 번호의 표식만 남긴다. 모르는 번호는 지운다 — 누를 곳이 없는 칩이 된다. "@TC" 같은 기록 형식이
    새어 들어왔으면 걷어 낸다."""
    text = _SPOKEN_PREFIX.sub("", text)
    return _MARKER.sub(lambda m: m.group(0) if (m.group(1), int(m.group(2))) in found else "", text).strip()


def _spoken(text: str, found: dict[tuple[str, int], Ref]) -> str:
    """대화 기록에는 표식 대신 이름을 쓴다."""
    def name(match: re.Match) -> str:
        ref = found.get((match.group(1), int(match.group(2))))
        # 질문 답의 기록은 이름만 남긴다. "@TC" 를 기록에 남기면 다음 답이 그것을 따라 쓴다.
        return ref.label if ref else ""

    return _MARKER.sub(name, text)


def _stage_of(payload: dict) -> str:
    """조회 하나를 진행 단계 이름으로. 화면은 단계마다 한 줄을 쌓는다."""
    kind, relation = payload.get("kind"), payload.get("relation")
    if relation == "versions":
        return "reading_versions"
    if relation == "verdicts":
        return "reading_verdicts"
    if kind in ("qa_run", "qa_try") or relation in ("qa_runs", "tries"):
        return "reading_qa_runs"
    if kind == "issue" or relation == "issues":
        return "reading_issues"
    if kind in ("scenario", "test_run") or relation in ("scenarios", "test_runs"):
        return "reading_scenarios"
    if kind == "build":
        return "reading_builds"
    return "reading_cases"
