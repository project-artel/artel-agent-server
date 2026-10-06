# -*- coding: utf-8 -*-
"""저작 본선 워크플로 — 재편 플랜 Step 2.

루프(모델이 왕복을 정함)를 고정 파이프라인으로 바꾼다:

    B 묶기·순서 (모델 1회 · 케이스 전량+캐시)
      → C 문장 쓰기 (묶음별 병렬 · 각 호출은 그 묶음만)
        → D 제출 (코드 — 기존 submit 프레임 그대로, LLM 0회)

비용 원인("제출 N번 = 전체 재읽기 N번")이 구조에서 사라진다. 전량 리딩은 B 의
1회로 국한되고, C 는 규칙+게임 모양(공통 prefix, 캐시)+자기 묶음만 읽으며 병렬로
돈다. 판단의 자유(무엇을 묶고 어떤 순서로, 모호하면 묻기)는 B 안에 그대로 있다.

StateGraph 를 안 쓴 이유: 순서가 고정된 파이프라인에서 그래프 러너가 더 주는 것은
체크포인트뿐이고, 지금 그것이 필요 없다(YAGNI). 병렬은 asyncio.gather 가 `Send` 와
같은 일을 한다. 필요해지는 날 노드 함수들을 그대로 그래프에 올리면 된다.

전 건 판정(reviewed)은 모델에게 다시 묻지 않는다 — B 가 묶음에 담은 것이 곧 in
이고 나머지가 out 이다. 어제(런 47) 모델이 최종답에서 판정을 빠뜨려 턴이 한 바퀴
더 돌았던 그 자리가 결정적 조립으로 사라진다.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base import AgentContext
from app.config import get_settings
from app.agents.scenario import trace
from app.agents.scenario.progress import THINKING, WRITING
from app.llm.chat_model import CACHE_POINT
from app.prompts import load_prompt
from app.agents.scenario.cases import render_game_shape, render_test_case_list
from app.agents.scenario.schemas import (
    AgentReply,
    AuthoredStep,
    QuestionOption,
    Ref,
    ReviewedCases,
    ScenarioAgentRequest,
    ScenarioAgentResult,
    ScenarioChange,
    ScenarioPlan,
    ScenarioQuestion,
)
from app.llm.chat_model import build_chat_model
from app.llm.models import LLMModel, ReasoningConfig

if TYPE_CHECKING:
    from app.sessions.channel import ScenarioChannel

logger = logging.getLogger(__name__)

# 반려된 묶음을 다시 써 보는 횟수. 한 번이면 이유를 반영하기에 충분하고, 그 이상은
# 같은 이유로 또 막히는 것이라 그 묶음을 버리고 사실대로 말하는 쪽이 낫다.
REWRITE_PER_GROUP = 1


def _rules(prompt_agent: str) -> str:
    """노드 프롬프트를 버전 파일에서 읽는다 (`app/prompts/<agent>/v*/system.md`).

    코드 상수가 아니라 파일인 이유: 잠금(`prompts-lock.json`)이 릴리스된 버전의
    무단 수정을 막고, 판마다 어느 문구를 읽었는지가 버전 이름으로 남는다 — 루프의
    v9 가 받던 것과 같은 관리를 워크플로의 각 단계도 받는다."""
    return load_prompt(prompt_agent, "system").body


class Group(BaseModel):
    title: str = Field(description="플레이어가 한 일처럼 읽히는 여정 제목 — 사용자 언어로")
    case_ids: list[int] = Field(description="실행 순서 그대로의 케이스 id")
    # 기존 시나리오와 실질적으로 같은 여정이면 그 번호 — 새 행 대신 그 본문을 교체·확장한다.
    # "같은 내용인가"는 판단이라 모델 몫이고(코드 유사도 임계값은 만들지 않는다), 목록에
    # 없는 번호는 코드가 무시한다(유령 방어).
    scenario_id: int | None = Field(
        default=None,
        description=(
            "If this journey covers substantially the same ground as an EXISTING"
            " SCENARIO, its id — the scenario will be rewritten to this journey"
            " instead of creating a duplicate. Empty for a genuinely new journey."
        ),
    )


class Ask(BaseModel):
    """모델이 사용자에게 묻는 것 하나 — 답할 수 있는 모양으로 (ARTEL-927).

    번호는 모델이 짓지 않는다. 답이 어느 질문의 것인지 잇는 열쇠라, 겹치거나 비면 답이
    엉뚱한 질문에 붙는다 — 코드가 [_questions] 에서 붙인다.
    """

    text: str = Field(description="The question, one sentence in the user's language.")
    why: str = Field(
        default="",
        description="One short sentence: what changes depending on the answer.",
    )
    options: list[str] = Field(
        default_factory=list,
        description=(
            "2-4 short choices, each written as the user's own instruction"
            " (\"구매 실패도 넣어 줘\", \"지금처럼 둬\"). The user can always type"
            " their own answer instead, so do not add an \"other\" choice."
        ),
    )


_DETAIL_DESCRIPTION = (
    "Why you did it this way, for the user to read — how you read the request, why this"
    " order or split, what you left out and why. Plain words in the user's language."
    " Shape it for the reader: a sentence or two when that is enough, a short `- ` list"
    " for three or more points, a `| a | b |` table when comparing or mapping things."
    " Do not repeat what was saved — the result line already says that."
    " Point at a TC as [[tc:<case_id>]] and a scenario as [[ts:<scenario_id>]] — the"
    " screen turns the marker into a chip with its name. Never a bare id outside one."
)
_QUESTIONS_DESCRIPTION = (
    "What the user should decide, if anything — each one answerable, with choices."
    " Empty when there is nothing to decide; never ask for the sake of asking."
    " `text` may point with [[tc:<case_id>]] / [[ts:<scenario_id>]]; `options` never —"
    " an option comes back as the user's own words."
)


class GroupingPlan(BaseModel):
    """B 의 출력 — 묶기·순서·범위 해석까지. 문장은 C 의 일이다."""

    groups: list[Group] = Field(default_factory=list)
    # 답의 설명 칸에 그대로 실린다(ARTEL-927). 코드가 쓰는 결과 칸은 사실(몇 개를 몇
    # 스텝으로 저장)만 말할 수 있고, "왜 그렇게 나눴는지"는 판단한 쪽만 답할 수 있다.
    detail: str = Field(
        default="",
        description=_DETAIL_DESCRIPTION,
    )
    # 범위가 정말 갈릴 때는 groups 를 비우고 이것만 채운다. 저장하면서 물을 수도 있다 —
    # 확실한 부분은 쓰고 갈리는 부분만 묻는 것이 사용자의 시간을 덜 쓴다.
    questions: list[Ask] = Field(default_factory=list, description=_QUESTIONS_DESCRIPTION)


class ModifyPlan(BaseModel):
    """E 의 출력 — 고칠 시나리오 하나의 완성본. 대상을 못 가리면 questions 만 채운다."""

    scenario_id: int | None = None
    title: str = ""
    description: str = ""
    steps: list[AuthoredStep] = Field(default_factory=list)
    # **합치기의 나머지 절반.** 칸이 `scenario_id` 하나뿐이던 동안, "이 둘을 합쳐 줘" 는 한쪽에
    # 둘의 내용을 다 적는 것까지가 낼 수 있는 전부였고 원본 한쪽이 그대로 남아 같은 흐름이 두
    # 벌이 됐다. 여기에 적힌 것은 요청이지 명령이 아니다 — 지워도 되는지는 오케가 센다(흡수된
    # 쪽 케이스가 남는 쪽에 전부 있는지, QA 실행 이력이 없는지).
    absorbed_scenario_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Only when the user asked to MERGE scenarios: the ids of the OTHER existing"
            " scenarios whose content you folded into this one, so they can be removed."
            " Every case they verified must appear in your steps — otherwise leave this"
            " empty. Empty for any request that is not a merge."
        ),
    )
    detail: str = Field(default="", description=_DETAIL_DESCRIPTION)
    questions: list[Ask] = Field(default_factory=list, description=_QUESTIONS_DESCRIPTION)


def _render_current(request: ScenarioAgentRequest) -> str:
    lines: list[str] = []
    for scenario in request.current_scenarios:
        if scenario.scenario_id is None:
            continue  # 저장 전 잔상은 수정 대상이 아니다 — id 없는 것은 교체 저장이 안 된다
        lines.append(f"[id {scenario.scenario_id}] {scenario.title}")
        if scenario.description:
            lines.append(f"  {scenario.description}")
        for order, step in enumerate(scenario.steps, start=1):
            tag = f" (case {step.case_id})" if step.case_id is not None else ""
            lines.append(f"  {order}. {step.action}{tag}")
    return "\n".join(lines) if lines else "(none)"


# 프롬프트는 (prefix, tail) 두 조각이다 — prefix 는 턴·호출이 바뀌어도 같은 부분
# (규칙+게임 모양+케이스)이고, tail 이 그 호출의 고유분이다. 경계에 캐시 포인트가
# 앉는다: Step 4 재실측(run 52~54)에서 끝-경계 캐싱이 "전부 쓰고(1.25배) 아무도 안
# 읽는(0회)" 순손해 25%로 실측됐다. prefix 경계면 다음 호출·다음 턴·같은 프로젝트의
# 다음 런이 같은 prefix 를 0.1배로 되읽는다.


def _modify_prompt(request: ScenarioAgentRequest, feedback: str = "") -> tuple[str, str]:
    shape = render_game_shape(
        request.test_case_list, request.entry_scene, request.scene_edges, request.starting_values
    )
    cases = render_test_case_list(request.test_case_list)
    prefix = (
        f"{_rules('scenario_modify').format(locale=request.locale.value)}\n\n=== GAME SHAPE ===\n{shape}\n\n"
        f"=== CASES ===\n{cases}"
    )
    feedback_tail = f"\n\n=== REVIEWER FEEDBACK (fix exactly this) ===\n{feedback}" if feedback else ""
    tail = (
        f"=== CURRENT SCENARIOS ===\n{_render_current(request)}\n\n"
        f"=== USER REQUEST ===\n{request.user_input}{feedback_tail}\n\nAnswer via the schema."
    )
    return prefix, tail


def _listed(names: list[str], limit: int = 3) -> str:
    """사람이 읽을 목록. 길면 앞 몇 개만 세어 말한다 — 스무 줄짜리 문장은 안 읽힌다."""
    if len(names) <= limit:
        return " · ".join(names)
    return " · ".join(names[:limit]) + f" 외 {len(names) - limit}개"


def _plain_title(title: str) -> str:
    """목록에 쓸 제목. 모델이 제목에 섞는 `Canvas/continue` 같은 코드 표시는 글자로만 남긴다."""
    return title.replace("`", "").strip()


def _change_of(action: str, title: str, scenario_id: int | None = None) -> ScenarioChange:
    return ScenarioChange(action=action, title=_plain_title(title), scenario_id=scenario_id)


def _summary(changes: list[ScenarioChange], ko: bool) -> str:
    """결과 칸의 한 줄 — 몇 건을 생성·수정·삭제했는지(ARTEL-936). 스텝 수는 세지 않는다.

    0건인 종류는 문장에서 뺀다. "0건을 삭제했습니다" 는 읽는 사람이 무엇이 지워졌나 찾게 만든다.
    """
    counts = [
        (sum(1 for c in changes if c.action == action), verb)
        for action, verb in (
            ("created", "생성" if ko else "created"),
            ("updated", "수정" if ko else "updated"),
            ("removed", "삭제" if ko else "removed"),
        )
    ]
    counts = [(n, verb) for n, verb in counts if n]
    if not counts:
        return "이번에는 저장한 시나리오가 없어요." if ko else "Nothing was saved this time."
    if ko:
        (first, verb), rest = counts[0], counts[1:]
        parts = [f"총 {first}건의 시나리오를 {verb}"] + [f"{n}건을 {v}" for n, v in rest]
        return "하고 ".join(parts) + "했습니다."
    words = [f"{verb} {n}" for n, verb in counts]
    return ("Scenarios " + ", ".join(words) + ".").capitalize()


def _bold(names: list[str]) -> str:
    """이름을 화면의 강조로. 따옴표로 감싸지 않는다 — 제목 안의 따옴표와 섞이고, 화면은
    굵게 그리는 법을 이미 안다(ARTEL-927)."""
    return " · ".join(f"**{name}**" for name in names)


def _questions(asks: list[Ask]) -> list[ScenarioQuestion]:
    """모델이 낸 질문에 번호를 붙여 답할 수 있는 모양으로.

    번호가 겹치면 답이 엉뚱한 질문에 붙는다. 그래서 모델이 아니라 여기서, 매번 새로 짓는다.
    빈 문장은 묻지 않는다 — 답할 수 없는 것을 내미는 일이다.
    """
    asked: list[ScenarioQuestion] = []
    for ask in asks:
        text = ask.text.strip()
        if not text:
            continue
        asked.append(ScenarioQuestion(
            id=f"agent:{uuid.uuid4().hex[:12]}",
            text=text,
            why=ask.why.strip() or None,
            options=[
                QuestionOption(id=f"o{index}", label=label.strip())
                for index, label in enumerate(ask.options, start=1)
                if label.strip()
            ],
        ))
    return asked


# `[[tc:153]]` / `[[ts:615]]` — 모델과 코드가 TC·TS 를 가리키는 표식(ARTEL-931).
_MARKER = re.compile(r"\[\[(tc|ts):(\d+)\]\]")


def _tc(case_id: int) -> str:
    return f"[[tc:{case_id}]]"


def _ref_of(request: ScenarioAgentRequest, kind: str, ref_id: int) -> Ref | None:
    """표식이 가리키는 것. 이 프로젝트의 TC·이 런의 TS 가 아니면 None — 지어낸 번호다."""
    if kind == "tc":
        case = next((c for c in request.test_case_list if c.id == ref_id), None)
        if case is None:
            return None
        detail = "\n".join(
            part for part in (
                f"사전조건: {case.precondition}" if case.precondition else "",
                f"기대값: {case.expected_value}" if case.expected_value else "",
            ) if part
        )
        return Ref(
            kind="tc", id=ref_id,
            label=f"{case.scene} — {case.step}" if case.scene else case.step,
            detail=detail or None,
        )
    scenario = next(
        (s for s in request.current_scenarios if s.scenario_id == ref_id), None
    )
    if scenario is None:
        return None
    return Ref(kind="ts", id=ref_id, label=scenario.title, detail=scenario.description or None)


def _checked(text: str, request: ScenarioAgentRequest, found: dict[tuple[str, int], Ref]) -> str:
    """아는 번호의 표식만 남긴다. 모르는 번호는 지운다 — 누를 곳이 없는 칩이 된다."""

    def keep(match: re.Match) -> str:
        key = (match.group(1), int(match.group(2)))
        ref = found.get(key) or _ref_of(request, *key)
        if ref is None:
            return ""
        found[key] = ref
        return match.group(0)

    return re.sub(r"[ \t]{2,}", " ", _MARKER.sub(keep, text)).strip()


def _spoken(text: str, found: dict[tuple[str, int], Ref], prefix: bool) -> str:
    """표식을 이름 글자로. 대화 기록에는 `@TC 이름`, 보기에는 이름만 — 보기는 누르면
    사용자의 말로 돌아오므로 표식도 `@` 도 남기지 않는다."""

    def name(match: re.Match) -> str:
        ref = found.get((match.group(1), int(match.group(2))))
        if ref is None:
            return ""
        return f"@{ref.kind.upper()} {ref.label}" if prefix else ref.label

    return _MARKER.sub(name, text)


def _answer(
    request: ScenarioAgentRequest,
    result: str,
    detail: str = "",
    questions: list[ScenarioQuestion] | None = None,
    reviewed: ReviewedCases | None = None,
    changes: list[ScenarioChange] | None = None,
) -> ScenarioAgentResult:
    """세 칸 답을 만든다. `message` 는 세 칸을 이은 글 — Redis 대화 기록과 구형 화면이
    그것만 읽으므로, 물은 것까지 들어 있어야 다음 턴이 "그거"를 풀 수 있다.

    표식(ARTEL-931)은 여기서 한 번에 검사한다. 화면이 읽는 칸(결과·설명·질문 문장)에는
    표식을 남기고 `refs` 로 이름을 붙이며, 사람이 읽는 글(`message`·보기)에는 이름을 쓴다.
    """
    found: dict[tuple[str, int], Ref] = {}
    result = _checked(result, request, found)
    detail = _checked(detail, request, found)
    asked = [
        question.model_copy(update={
            "text": _checked(question.text, request, found),
            "why": _checked(question.why, request, found) if question.why else question.why,
        })
        for question in questions or []
    ]
    asked = [
        question.model_copy(update={"options": [
            option.model_copy(update={"label": _spoken(_checked(option.label, request, found), found, prefix=False)})
            for option in question.options
        ]})
        for question in asked
    ]
    changes = changes or []
    listed = "\n".join(f"- {change.title}" for change in changes)
    message = "\n".join(
        _spoken(part, found, prefix=True)
        for part in (result, listed, detail, *(q.text for q in asked)) if part
    )
    return ScenarioAgentResult(
        message=message,
        scenarios=[],
        reviewed=reviewed,
        reply=AgentReply(result=result, detail=detail, changes=changes),
        questions=asked,
        refs=list(found.values()),
    )


def _existing_scenarios(request: ScenarioAgentRequest) -> str:
    """B 가 겹침을 판단할 재료 — 기존 시나리오의 번호·제목·케이스 구성만.

    스텝 본문은 싣지 않는다: 겹치는지 보는 데는 무엇을 검증하는 여정인지(케이스 구성)면
    충분하고, 본문까지 실으면 턴마다 변하는 tail 이 무거워진다."""
    lines = [
        f"[id {s.scenario_id}] {s.title} — cases: "
        + ", ".join(str(step.case_id) for step in s.steps if step.case_id is not None)
        for s in request.current_scenarios
        if s.scenario_id is not None
    ]
    return "\n".join(lines) if lines else "(none)"


def _grouping_prompt(
    request: ScenarioAgentRequest, findings: list[str] | None = None
) -> tuple[str, str]:
    shape = render_game_shape(
        request.test_case_list, request.entry_scene, request.scene_edges, request.starting_values
    )
    cases = render_test_case_list(request.test_case_list)
    prefix = f"{_rules('scenario_grouping')}\n\n=== GAME SHAPE ===\n{shape}\n\n=== CASES ===\n{cases}"
    findings_tail = (
        "\n\n=== ORDER CHECK FINDINGS (your previous grouping breaks these — move the"
        " offending case to a journey whose state allows it, or reorder) ===\n"
        + "\n".join(findings)
        if findings
        else ""
    )
    tail = (
        f"=== EXISTING SCENARIOS (this run) ===\n{_existing_scenarios(request)}\n\n"
        f"=== USER REQUEST ===\n{request.user_input}{findings_tail}\n\nAnswer via the schema."
    )
    return prefix, tail


def _writer_prompt(request: ScenarioAgentRequest, group: Group, feedback: str = "") -> tuple[str, str]:
    # **공통 prefix 를 앞에, 묶음을 뒤에.** 규칙과 게임 모양은 worker 끼리 같아서 캐시가
    # 한 번 쓰고 나머지가 읽는다. 묶음 상세가 그 뒤에 온다.
    shape = render_game_shape(
        request.test_case_list, request.entry_scene, request.scene_edges, request.starting_values
    )
    by_id = {case.id: case for case in request.test_case_list}
    picked = [by_id[i] for i in group.case_ids if i in by_id]
    cases = render_test_case_list(picked)
    feedback_tail = f"\n\n=== REVIEWER FEEDBACK (fix exactly this) ===\n{feedback}" if feedback else ""
    prefix = f"{_rules('scenario_writer').format(locale=request.locale.value)}\n\n=== GAME SHAPE ===\n{shape}"
    tail = (
        f"=== JOURNEY: {group.title} ===\n{cases}\n\n"
        f"=== USER REQUEST (context) ===\n{request.user_input}{feedback_tail}\n\nAnswer via the schema."
    )
    return prefix, tail


class _Writer(BaseModel):
    """C 의 출력 — 시나리오 하나. 제목은 B 가 정했으므로 설명과 스텝만 채운다."""

    description: str = Field(default="")
    steps: list[AuthoredStep] = Field(default_factory=list)


async def _score_groups(request: ScenarioAgentRequest, groups: list[Group]) -> list[str] | None:
    """묶기·순서를 오케의 실제 잣대로 검증한다 — B 미니 루프의 눈.

    오케 채점 창구(`POST /internal/experiment/authoring/score`)는 reconcile 의
    검수-만(save=false) 경로를 그대로 빌린 것이라, 여기서 나온 어긋남은 저장 때
    나올 어긋남과 같은 규칙이다. run 53 실측에서 이 부류(상태 조건이 안 맞는
    케이스 배치)가 판 요약에 기록만 되고 지나갔다 — 배치를 고칠 수 있는 유일한
    자리인 B 에서 미리 잡는다.

    반환: 어긋남 문장 목록(비면 깨끗함), None = 검증을 못 했음(스코프 없음·창구
    다운). 검증 실패가 턴을 죽이면 안 되므로 예외는 삼키고 None 을 낸다.
    """
    settings = get_settings()
    if (
        not settings.orchestration_base_url
        or request.run_id is None
        or request.project_id is None
        or request.app_user_id is None
    ):
        return None
    body = {
        "runId": request.run_id,
        "projectId": request.project_id,
        "appUserId": request.app_user_id,
        "groups": [{"title": g.title, "caseIds": g.case_ids} for g in groups],
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            answer = await client.post(
                f"{settings.orchestration_base_url}/internal/experiment/authoring/score",
                json=body,
            )
            answer.raise_for_status()
            return list(answer.json().get("contradicted") or [])
    except Exception as error:  # noqa: BLE001 — 검증 불가가 저작 불가가 되면 안 된다
        logger.warning("[scenario] group scoring unavailable: %s", error)
        return None


async def _call(model: LLMModel, reasoning: ReasoningConfig | None, schema, prompt: tuple[str, str]):
    """공통 prefix 는 system + 캐시 경계로, 고유분은 human 으로.

    `cache_prompt=True`(끝-경계)를 안 쓰는 이유: 단발 호출에서는 끝-경계가 전부 쓰고
    아무도 안 읽는다(run 52~54 실측 — 읽기 0, 입력의 99%가 1.25배 쓰기). 경계를
    prefix 뒤에 직접 놓으면 같은 prefix 의 다음 호출이 0.1배로 되읽는다. 1,024 token
    미만 prefix 는 경계가 조용히 무시된다 — 손해 없음.
    """
    prefix, tail = prompt
    chat = build_chat_model(model, reasoning)
    chain = chat.with_structured_output(schema, method="json_schema")
    messages = [
        SystemMessage(content=[{"type": "text", "text": prefix}, CACHE_POINT]),
        HumanMessage(content=tail),
    ]
    return await chain.ainvoke(messages)


async def run_authoring_workflow(
    request: ScenarioAgentRequest,
    context: AgentContext,
    channel: "ScenarioChannel",
) -> ScenarioAgentResult:
    run_id = request.run_id
    known_ids = {case.id for case in request.test_case_list}

    # ── B: 묶기·순서 (전량은 여기 1회) ─────────────────────────────────────────
    # 노드 경계 보고 — 모델 사이의 침묵이 "멎었다"와 구분되게. 오케는 모르는 stage 를
    # 버리므로 기존 wire 값(thinking/writing)을 그대로 쓴다(배포 결합 없음).
    await channel.report(THINKING)
    plan: GroupingPlan = await _call(
        request.model, request.reasoning, GroupingPlan, _grouping_prompt(request)
    )
    # 유령 id 방어 — 하네스 실측은 0/30 이었지만, 지어낸 번호가 D 까지 가면 반려다.
    known_scenario_ids = {
        s.scenario_id for s in request.current_scenarios if s.scenario_id is not None
    }
    groups = [
        Group(
            title=g.title,
            case_ids=[i for i in g.case_ids if i in known_ids],
            scenario_id=g.scenario_id if g.scenario_id in known_scenario_ids else None,
        )
        for g in plan.groups
    ]
    groups = [g for g in groups if g.case_ids]
    trace.record(
        run_id, "워크플로 B — 묶기·순서",
        f"묶음 {len(groups)}개"
        + "".join(f" · 질문: {q.text}" for q in plan.questions)
        + (f"\n{plan.detail}" if plan.detail else "")
        + "".join(
            f"\n  · {g.title} — {len(g.case_ids)}케이스"
            + (f" (기존 id {g.scenario_id} 에 잇기)" if g.scenario_id is not None else "")
            for g in groups
        ),
    )
    ko = request.locale.value == "ko"
    if plan.questions and not groups:
        return _answer(
            request,
            "먼저 확인할 게 있어서 아직 저장하지 않았어요."
            if ko else "Nothing saved yet — I need to check something first.",
            plan.detail, _questions(plan.questions),
        )
    if not groups:
        return _answer(
            request,
            "요청에 맞는 TC를 찾지 못해서 아무것도 저장하지 않았어요."
            if ko else "No test case matched the request, so nothing was saved.",
            plan.detail,
        )

    # ── B 미니 루프: 걷기 검증 (코드 잣대 → 어긋나면 B 1회 재작성) ────────────────
    # 상태 조건이 안 맞는 케이스 배치(run 53 의 그 부류)는 문장을 쓰기 전, 배치를
    # 고칠 수 있는 유일한 자리에서 잡는다. 재작성은 한 번 — prefix 캐시 덕에 재호출
    # 입력은 0.1배라, 비용은 사실상 출력분뿐이다. 그래도 남으면 기록하고 진행한다
    # (막지 않는다 — 저장 뒤 판 요약에도 같은 잣대가 다시 적힌다).
    findings = await _score_groups(request, groups)
    if findings:
        retried_plan = await _call(
            request.model, request.reasoning, GroupingPlan,
            _grouping_prompt(request, findings=findings),
        )
        retried_groups = [
            Group(
                title=g.title,
                case_ids=[i for i in g.case_ids if i in known_ids],
                scenario_id=g.scenario_id if g.scenario_id in known_scenario_ids else None,
            )
            for g in retried_plan.groups
        ]
        retried_groups = [g for g in retried_groups if g.case_ids]
        if retried_groups:
            groups = retried_groups
            if retried_plan.detail or retried_plan.questions:
                plan = plan.model_copy(update={
                    "detail": retried_plan.detail or plan.detail,
                    "questions": retried_plan.questions or plan.questions,
                })
        remaining = await _score_groups(request, groups)
        trace.record(
            run_id, "워크플로 B — 걷기 검증",
            f"어긋남 {len(findings)}건 → 재작성 1회 → 남은 어긋남 "
            f"{'검증불가' if remaining is None else len(remaining)}건"
            + "".join(f"\n  · {f}" for f in findings),
        )
    elif findings is not None:
        trace.record(run_id, "워크플로 B — 걷기 검증", "어긋남 0건")

    # ── C: 문장 쓰기 (묶음별 병렬 — 각 호출은 규칙+모양(캐시)+자기 묶음만) ────────
    async def write(group: Group, feedback: str = "") -> ScenarioPlan | None:
        await channel.report(WRITING)
        try:
            out: _Writer = await _call(
                request.model, request.reasoning, _Writer, _writer_prompt(request, group, feedback)
            )
        except Exception as error:  # noqa: BLE001 — worker 하나가 판을 죽이면 안 된다
            logger.warning("[scenario] writer failed for %s: %s", group.title, error)
            return None
        return ScenarioPlan(
            scenario_id=group.scenario_id,
            title=group.title,
            description=out.description,
            steps=out.steps,
        )

    def _unplaced(group: Group, plan_: ScenarioPlan | None) -> list[int]:
        if plan_ is None:
            return list(group.case_ids)
        placed = {step.case_id for step in plan_.steps if step.case_id is not None}
        return [i for i in group.case_ids if i not in placed]

    async def write_covering(group: Group) -> ScenarioPlan | None:
        """C 한 판 + 미탑재 보수 1회.

        B 가 묶은 케이스가 스텝에 안 실리면 턴끝 검수가 "판정했으나 안 담음"으로 막고
        구 루프 되돌리기(전량 프롬프트)가 불려 나간다 — 라이브 1턴 실측(run 47,
        2026-09-09)에서 그 꼬리가 판 비용의 3/4였다. 같은 worker 에게 6.7k 짜리 보수
        한 번을 시키는 쪽이 구조적으로 싸다. 그래도 남으면 사실대로 out 으로 보낸다.
        """
        scenario = await write(group)
        missing = _unplaced(group, scenario)
        if scenario is not None and missing:
            repaired = await write(
                group,
                feedback=(
                    f"Cases {missing} belong to this journey but no step carries their"
                    " case_id. Add the missing action step(s) for exactly these cases,"
                    " keeping every existing step as it is."
                ),
            )
            if repaired is not None:
                scenario = repaired
        return scenario

    written = await asyncio.gather(*(write_covering(g) for g in groups))
    trace.record(
        run_id, "워크플로 C — 문장",
        "\n".join(
            f"  · {g.title} — "
            + (f"스텝 {len(p.steps)}" if p else "실패")
            + (f" · 미탑재 {miss}" if (miss := _unplaced(g, p)) and p else "")
            for g, p in zip(groups, written)
        ),
    )

    # ── D: 제출 (코드 — 기존 프레임 그대로, 묶음 순서대로 직렬 방출) ──────────────
    saved: list[str] = []
    # 바뀐 시나리오(ARTEL-936). **저쪽이 저장한 대로** 센다 — 하나를 냈는데 둘로 나뉘어 저장되거나,
    # 같은 제목이라 새로 만들지 않고 기존 것을 고쳤을 수 있다. 옛 서버는 그 목록을 안 보내므로
    # 그때만 우리가 낸 것으로 짐작한다.
    changes: list[ScenarioChange] = []
    dropped: list[str] = []
    accepted_plans: list[ScenarioPlan] = []
    for group, scenario in zip(groups, written):
        if scenario is None:
            dropped.append(group.title)
            continue
        answer = await channel.submit_scenario(scenario.model_dump(by_alias=True))
        if answer is not None and not answer.accepted and answer.detail:
            # 반려 라우팅: 그 묶음 worker 만 이유를 들고 한 번 다시 쓴다. B 복귀는 없다 —
            # 묶기·순서가 틀렸다는 신호가 아니라 문장·근거의 지적이기 때문이다.
            retried = await write(group, feedback=answer.detail)
            if retried is not None:
                scenario = retried
                answer = await channel.submit_scenario(retried.model_dump(by_alias=True))
            else:
                answer = None
        if answer is not None and answer.accepted:
            saved.append(group.title)
            changes += [
                _change_of("created" if one.created else "updated", one.title, one.scenario_id)
                for one in answer.saved
            ] or [_change_of("updated" if group.scenario_id else "created", group.title, group.scenario_id)]
            accepted_plans.append(scenario)
        else:
            dropped.append(group.title)
    trace.record(
        run_id, "워크플로 D — 제출",
        f"저장 {len(saved)} · 버림 {len(dropped)}"
        + ("".join(f"\n  버림: {t}" for t in dropped) if dropped else ""),
    )

    # ── 마무리: 판정은 저장된 스텝에서 결정적으로, 문구는 코드가 조립 ──────────────
    # B 의 선택이 아니라 **실제 제출돼 받아들여진 스텝**이 기준이다. B 가 묶었지만 C 가
    # 끝내 못 실은 케이스를 in 으로 보내면 턴끝 검수가 "판정했으나 안 담음"으로 막고
    # 구 루프가 불려 나간다(run 47 실측). 그런 케이스는 사실대로 out 이고, 문구에 밝힌다.
    covered = {
        step.case_id
        for scenario in accepted_plans
        for step in scenario.steps
        if step.case_id is not None and step.case_id in known_ids
    }
    grouped = {i for g in groups for i in g.case_ids}
    unplaced = sorted(grouped - covered)
    reviewed = ReviewedCases(
        included=sorted(covered), excluded=sorted(known_ids - covered)
    )
    # 답은 세 칸이다(ARTEL-927). **결과는 코드가 센 사실만**(몇 건을 생성·수정했는지와 그 목록,
    # ARTEL-936), **설명은 판단한 모델이**(`plan.detail`), **질문은 고를 수 있게.** 코드가 아는
    # 미반영분(저장 못 한 시나리오, 자리를 못 찾은 TC)도 설명에 사과문으로 섞지 않고 질문으로
    # 낸다 — 사용자가 정할 일이기 때문이다. 어느 칸에도 번호는 없다.
    result = _summary(changes, ko)

    asks = list(plan.questions)
    if dropped:
        asks.append(Ask(
            text=(
                f"실행 확인을 통과하지 못해 저장하지 못한 시나리오가 있어요: {_bold(dropped)}. 다시 써 볼까요?"
                if ko else
                f"These did not pass the run check and were not saved: {_bold(dropped)}. Try again?"
            ),
            options=["다시 써 줘", "그냥 둬"] if ko else ["Write them again", "Leave them out"],
        ))
    if unplaced:
        # 맨 이름이 아니라 표식으로 — 화면이 이름 칩으로 그리고 누르면 그 TC 의 내용을 보인다.
        names = _listed([_tc(i) for i in unplaced if i in known_ids])
        asks.append(Ask(
            text=(
                f"이 흐름에 넣을 자리가 없던 TC가 있어요: {names}. 따로 시나리오로 만들까요?"
                if ko else
                f"No place in this flow for: {names}. Make a separate scenario for them?"
            ),
            options=(
                ["따로 시나리오로 만들어 줘", "그냥 둬"] if ko
                else ["Make a separate scenario", "Leave them out"]
            ),
        ))
    # scenarios 는 비운다 — 하나씩 제출 계약에서 저장은 이미 끝났고, 결과 봉투에 다시
    # 실으면 두 벌이 된다(런 12 의 그 사고). 턴끝 검수는 reviewed 로 커버리지만 본다.
    return _answer(request, result, plan.detail, _questions(asks), reviewed, changes)


async def run_modify_workflow(
    request: ScenarioAgentRequest,
    context: AgentContext,
    channel: "ScenarioChannel",
) -> ScenarioAgentResult:
    """E — 기존 시나리오 수정 (재편 플랜 Step 3).

    오케가 매 턴 실어 주는 `current_scenarios`(id·제목·스텝)가 재료의 전부라 새 조회가
    필요 없고, `scenario_id` 를 실어 내면 reconcile 이 본문을 통째로 교체한다. 모델
    1회 + 반려 시 1회 — 신규 저작(B→C→D)에 태우면 통째 재작성이 되던 그 갈래다.

    reviewed 는 내지 않는다 — 수정은 전 건 판정 턴이 아니고, 오케도 reviewed 가 없으면
    되돌리기를 태우지 않는다(`TestScenarioAgentService` 738행의 그 분기).
    """
    run_id = request.run_id
    known_scenario_ids = {
        scenario.scenario_id
        for scenario in request.current_scenarios
        if scenario.scenario_id is not None
    }

    async def edit(feedback: str = "") -> ModifyPlan | None:
        await channel.report(WRITING)
        try:
            return await _call(
                request.model, request.reasoning, ModifyPlan, _modify_prompt(request, feedback)
            )
        except Exception as error:  # noqa: BLE001 — 수정 실패가 세션을 죽이면 안 된다
            logger.warning("[scenario] modify call failed: %s", error)
            return None

    ko = request.locale.value == "ko"
    plan = await edit()
    trace.record(
        run_id, "워크플로 E — 수정",
        "호출 실패" if plan is None else (
            f"대상 id {plan.scenario_id} · 스텝 {len(plan.steps)}"
            + "".join(f" · 질문: {q.text}" for q in plan.questions)
            + (f"\n{plan.detail}" if plan.detail else "")
        ),
    )
    if plan is None:
        message = (
            "수정 요청을 처리하지 못했습니다. 잠시 뒤 다시 시도해 주세요."
            if ko else "The modify request could not be processed. Please try again."
        )
        return ScenarioAgentResult(message=message, scenarios=[])
    if plan.questions and not plan.steps:
        return _answer(
            request,
            "먼저 확인할 게 있어서 아직 아무것도 고치지 않았어요."
            if ko else "Nothing changed yet — I need to check something first.",
            plan.detail, _questions(plan.questions),
        )
    if plan.scenario_id not in known_scenario_ids or not plan.steps:
        # 대상을 못 가리키면 고치지 않는다 — 짐작으로 남의 시나리오를 갈아끼우는 것이
        # 최악이다. 어느 것인지 사용자에게 묻고, 고를 것은 이 런의 시나리오 제목이다.
        titles = [s.title for s in request.current_scenarios if s.scenario_id is not None]
        return _answer(
            request,
            "어느 시나리오를 고칠지 확실하지 않아서 모두 그대로 두었어요."
            if ko else "I wasn't sure which scenario you meant, so everything is as it was.",
            questions=_questions([Ask(
                text="어느 시나리오를 고칠까요?" if ko else "Which scenario should I change?",
                options=[f"{title} 고쳐 줘" if ko else f"Change {title}" for title in titles],
            )]),
        )

    original = next(
        s for s in request.current_scenarios if s.scenario_id == plan.scenario_id
    )

    def title_of(p: ModifyPlan) -> str:
        return p.title or original.title

    def to_scenario(p: ModifyPlan) -> ScenarioPlan:
        return ScenarioPlan(
            scenario_id=p.scenario_id,
            title=p.title or original.title,
            description=p.description or original.description,
            steps=p.steps,
        )

    # 합치기에서 걷어낼 것들. 대상 자신과 모르는 번호는 뺀다 — 지우는 일에 유령 번호가
    # 닿으면 안 된다. 실제로 지울지는 오케가 한 번 더 센다.
    def absorbed_of(p: ModifyPlan) -> list[int]:
        return [
            i for i in p.absorbed_scenario_ids
            if i in known_scenario_ids and i != p.scenario_id
        ]

    def _body(steps: list) -> list[tuple]:
        """본문을 비교할 수 있는 모양으로. 사람이 읽는 것과 기계가 읽는 것을 함께 본다 —
        문장만 같고 근거가 바뀐 것도 고친 것이고, 그 반대도 마찬가지다."""
        return [
            (
                (step.action or "").strip(),
                step.case_id,
                (step.input or "").strip(),
                step.step_source,
                (step.step_unknown_reason or "").strip(),
            )
            for step in steps
        ]

    def unchanged(p: ModifyPlan) -> bool:
        """**아무것도 안 고쳤나.** 여기가 정정이 조용히 삼켜지던 자리다.

        실측(run 75): 사용자가 "스토리가 끝나면 알아서 맵으로 이동함 이걸 적용해서 고쳐줘"
        라고 했는데 모델은 본문을 그대로 돌려주고 답만 "바로잡았습니다" 라고 썼다. 사용자는
        고쳐진 줄 알았고, 저장된 것은 그대로였다 — 말한 것이 사라진 것을 알 방법이 없었다.

        프롬프트로는 막을 수 없다. 안 고치고도 고쳤다고 말하는 것을 막는 유일한 방법은
        **낸 것과 원본을 코드가 비교하는 것**이다. 같으면 성공이라 말하지 않는다.
        """
        if absorbed_of(p):
            return False  # 걷어낼 것을 지목했으면 이 턴이 한 일이 있다
        if (p.title or original.title) != original.title:
            return False
        if (p.description or original.description) != original.description:
            return False
        return _body(p.steps) == _body(original.steps)

    # **안 고쳤으면 한 번 더 시킨다.** 요청을 받고 본문을 그대로 돌려준 것은 답이 아니다 —
    # 못 하겠으면 못 한다고 말해야 하고, 그 말은 `question` 에 적는 것이 계약이다.
    if unchanged(plan):
        retried = await edit(
            feedback=(
                "You returned the scenario unchanged. The user asked for a change, so"
                " returning the same body is not an answer. Apply what they asked — or,"
                " if it truly cannot be applied (no case covers it, it contradicts the"
                " journey's start, it is already true), leave steps empty and say that"
                " in `detail`, naming the part you could not do and why — and put what the"
                " user could decide in `questions`."
            )
        )
        if retried is not None and retried.scenario_id == plan.scenario_id:
            plan = retried
        # 두 번째도 그대로면 저장하지 않는다. 같은 것을 다시 저장하면 사용자는 고쳐진 줄 안다.
        if unchanged(plan):
            trace.record(run_id, "워크플로 E — 그대로", "요청을 받고 본문이 안 바뀌었다 — 저장하지 않음")
            # 모델이 묻지 않았으면 코드가 묻는다 — 어디를 어떻게 바꿀지 짚어 달라고. 고를 것이
            # 없는 질문이라 선택지 없이 직접 입력만 받는다.
            asks = plan.questions or [Ask(
                text=(
                    "어느 스텝을 어떻게 바꿀지 짚어 주시겠어요?"
                    if ko else "Which step should change, and to what?"
                ),
                why=(
                    "짚어 주신 자리를 그대로 고칠게요."
                    if ko else "I will change exactly the place you point at."
                ),
            )]
            return _answer(
                request,
                f"말씀하신 내용을 반영하지 못해서 저장하지 않았어요. {_bold([title_of(plan)])} 본문은 그대로예요."
                if ko else
                f"I could not apply that, so nothing was saved. {_bold([title_of(plan)])} is unchanged.",
                plan.detail, _questions(asks),
            )

    answer = await channel.submit_scenario(
        to_scenario(plan).model_dump(by_alias=True), absorbed_of(plan)
    )
    if answer is not None and not answer.accepted and answer.detail:
        retried = await edit(feedback=answer.detail)
        if retried is not None and retried.scenario_id == plan.scenario_id and retried.steps:
            plan = retried
            answer = await channel.submit_scenario(
                to_scenario(plan).model_dump(by_alias=True), absorbed_of(plan)
            )
        else:
            answer = None
    saved = answer is not None and answer.accepted
    trace.record(
        run_id, "워크플로 E — 제출",
        f"{'교체 저장' if saved else '버림'} · id {plan.scenario_id} · 낸 스텝 {len(plan.steps)}"
        + (f" · 저장된 스텝 {answer.steps}" if saved and answer is not None else "")
        + (f" · 흡수 {answer.absorbed}" if saved and answer and answer.absorbed else "")
        + (f" · 남김 {answer.kept}" if saved and answer and answer.kept else ""),
    )
    title = plan.title or original.title
    if saved and answer is not None:
        # 무엇이 바뀌었는지는 저쪽이 센 것만 말한다(ARTEL-936). 고친 것은 저장 목록에서, 합치면서
        # 걷어낸 것은 `absorbed` 에서 — 지울지 말지는 저쪽이 판정했다.
        changes = [
            _change_of("created" if one.created else "updated", one.title, one.scenario_id)
            for one in answer.saved
        ] or [_change_of("updated", title, plan.scenario_id)]
        changes += [_change_of("removed", gone) for gone in answer.absorbed]
        result = _summary(changes, ko)
        # 남긴 이유는 저쪽 문장을 그대로 옮긴다 — 왜 못 지웠는지는 센 쪽만 안다.
        detail = "\n".join(part for part in (plan.detail.strip(), *answer.kept) if part)
        return _answer(request, result, detail, _questions(plan.questions), changes=changes)
    # 저장되지 않은 수정의 설명은 싣지 않는다 — 일어나지 않은 변경을 설명하게 된다.
    return _answer(
        request,
        f"고친 내용이 실행 확인을 통과하지 못해서 저장하지 않았어요. {_bold([title])} 본문은 그대로예요."
        if ko else
        f"My edit did not pass the run check, so nothing was saved. {_bold([title])} is unchanged.",
        questions=_questions([Ask(
            text="다시 고쳐 볼까요?" if ko else "Try the edit again?",
            options=["다시 고쳐 줘", "그냥 둬"] if ko else ["Try again", "Leave it"],
        )]),
    )
