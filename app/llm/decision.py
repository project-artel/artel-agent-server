"""Jev 결정 질문을 보내는 곳. 네 호출자(시나리오 router, QA macro 의 decide·ask, `pulse`
relevance, reach)가 모두 여기를 지난다.

요청 body 는 Jev 결정 API 의 모양 ``{model, state, questions}`` 이고, 응답도 Jev 의 모양
``{answers: {<질문>: {choice} | {noul}}, usage}`` 로 돌려준다. 호출자는 누가 답했는지 모른다.

누가 답하는지는 관리자가 고른 결정 model(`app/llm/model_settings.py`)이 정한다.

- null 이거나 ``typesafe/jev-1.13`` 이면 Jev 다. TypeSafe key 가 저장돼 있으면 TypeSafe API
  (``POST {typesafe_base_url}/v1/systemone``, model ``jev-1.13.0``)를 직접 부르고, 없으면
  OpenRouter 의 ``/api/alpha/decisions`` 를 부른다. 경로는 호출마다 key 를 보고 고른다.
- 다른 id 면 `build_chat_model` 의 model 이 추론을 끈 채 structured output 으로 같은 질문에
  답한다. choice 는 주어진 선택지 id 중 하나만 받고, noul 은 0 이상 1 이하만 받는다. 어긋나면
  `DecisionError` 다.

비용: OpenRouter 의 Jev 응답만 ``usage.cost`` 를 준다. TypeSafe API 는 token 수만 주고, 지금
Jev 호출의 token 을 기록하는 곳이 없으므로 비용은 비어 있다. LLM 경로도 ``usage`` 를 싣지 않는다.
그 호출의 token 과 비용은 `build_chat_model` 에 붙은 `UsageCallback` 이 orchestration 의
LLM 사용량으로 이미 보낸다.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from app.config import get_settings
from app.llm.api_key import UNAUTHORIZED, get_llm_api_key_provider
from app.llm.model_settings import get_model_settings_provider
from app.llm.models import ReasoningConfig

logger = logging.getLogger(__name__)

# 결정 model 설정 값으로서의 Jev, 그리고 OpenRouter 에서의 model id.
JEV_MODEL = "typesafe/jev-1.13"
# TypeSafe API 에서의 같은 model id.
TYPESAFE_JEV_MODEL = "jev-1.13.0"
OPENROUTER_DECISIONS_PATH = "/api/alpha/decisions"
TYPESAFE_DECISIONS_PATH = "/v1/systemone"

PROMPT_AGENT = "decision"
REASONING_OFF = ReasoningConfig(max_tokens=0)


class DecisionError(RuntimeError):
    """결정 호출이 실패했거나 답이 질문의 모양과 맞지 않는다."""


class DecisionQuestion(BaseModel):
    """질문 하나. `criteria` 의 값은 호출자마다 모양이 달라 열어 둔다(문자열이나 객체)."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["choice", "noul"]
    instructions: str
    criteria: dict[str, Any]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: dict[str, Any]
    questions: dict[str, DecisionQuestion]


def resolve_decision_model() -> str:
    """지금 결정 질문에 답할 model id. 설정이 없으면 `JEV_MODEL` 이다."""
    return get_model_settings_provider().get_decision_model() or JEV_MODEL


def is_jev(model: str | None) -> bool:
    return model is None or model == JEV_MODEL


async def decide(http: Any, body: dict) -> dict:
    """`body` 를 결정 model 에 보내고 Jev 모양의 응답 JSON 을 돌려준다.

    `http` 는 호출자가 가진 `httpx.AsyncClient` 다. Jev 경로만 쓴다. 연결을 재사용하는
    방식과 timeout 은 호출자마다 달라서(router 는 keepalive 를 넉넉히 둔다) 여기서 만들지 않는다.
    """
    model = resolve_decision_model()
    if is_jev(model):
        return await post_jev_decision(http, body)
    return await answer_with_chat_model(model, body)


# ── Jev ─────────────────────────────────────────────────────────────────────


async def post_jev_decision(http: Any, body: dict) -> dict:
    """Jev 에 보낸다. TypeSafe key 가 있으면 TypeSafe API, 없으면 OpenRouter 다."""
    settings = get_settings()
    keys = get_llm_api_key_provider()
    typesafe_key = keys.get_typesafe_key()
    if typesafe_key:
        url = f"{settings.typesafe_base_url.rstrip('/')}{TYPESAFE_DECISIONS_PATH}"
        headers = {"Authorization": f"Bearer {typesafe_key}", "Content-Type": "application/json"}
        payload = {**body, "model": TYPESAFE_JEV_MODEL}
    else:
        openrouter_key = keys.get_openrouter_key()
        if not openrouter_key:
            raise DecisionError("Jev 결정 API 에 쓸 TypeSafe key 도 OpenRouter key 도 없다")
        base = settings.llm_base_url.rstrip("/").removesuffix("/api/v1")
        url = f"{base}{OPENROUTER_DECISIONS_PATH}"
        headers = {"Authorization": f"Bearer {openrouter_key}", "Content-Type": "application/json"}
        if settings.openrouter_site_url:
            headers["HTTP-Referer"] = settings.openrouter_site_url
        if settings.openrouter_app_title:
            headers["X-Title"] = settings.openrouter_app_title
        payload = {**body, "model": JEV_MODEL}

    response = await http.post(url, headers=headers, json=payload)
    if getattr(response, "status_code", None) == UNAUTHORIZED:
        # 관리자가 key 를 바꿨을 수 있다. 이번 호출은 실패하고 다음 호출이 새 key 를 읽는다.
        keys.invalidate()
    response.raise_for_status()
    return response.json()


# ── 범용 LLM ────────────────────────────────────────────────────────────────


async def answer_with_chat_model(model: str, body: dict) -> dict:
    """같은 질문을 범용 chat model 에 structured output 으로 묻는다."""
    # import 를 여기서 하는 이유: `app.llm.chat_model` 은 provider client 들을 끌어오므로
    # Jev 만 쓰는 호출에서 그 import 비용을 내지 않는다.
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.llm.chat_model import structured
    from app.prompts import load_prompt

    try:
        request = DecisionRequest.model_validate(body)
    except ValidationError as error:
        raise DecisionError(f"결정 요청의 모양이 틀렸다: {error.error_count()}개 field") from error
    schema = answer_schema(request)

    system = load_prompt(PROMPT_AGENT, "system").body
    human = load_prompt(PROMPT_AGENT, "human").body.format(
        state=json.dumps(request.state, ensure_ascii=False, indent=2, default=str),
        questions=render_questions(request),
        output_contract=render_output_contract(request),
    )
    try:
        parsed = await structured(model, schema, REASONING_OFF).ainvoke(
            [SystemMessage(content=system), HumanMessage(content=human)]
        )
    except Exception as error:  # noqa: BLE001 — 호출자에게는 실패 타입 하나만 보인다
        raise DecisionError(f"결정 model {model} 호출이 실패했다: {error}") from error
    return to_jev_response(request, parsed)


def _choice_ids(question: DecisionQuestion) -> tuple[str, ...]:
    return tuple(question.criteria)


def answer_schema(request: DecisionRequest) -> type[BaseModel]:
    """질문마다 field 가 하나인 답 schema. choice 는 선택지 id 의 enum 이다.

    field 이름은 `q0`, `q1` … 로 두고 질문 이름은 alias 로 단다. 질문 이름이 pydantic 의
    예약어(`model_config` 같은)이거나 식별자가 아니어도 schema 가 깨지지 않게 하려는 것이다.
    noul 의 범위는 schema 에 넣지 않고 `to_jev_response` 에서 검사한다. strict JSON schema 를
    받는 provider 중 `minimum`·`maximum` 을 거절하는 곳이 있다.
    """
    fields: dict[str, Any] = {}
    for index, (name, question) in enumerate(request.questions.items()):
        if question.type == "choice":
            options = _choice_ids(question)
            if not options:
                raise DecisionError(f"choice 질문 {name} 에 선택지가 없다")
            answer = create_model(
                f"ChoiceAnswer{index}",
                __config__=ConfigDict(extra="forbid"),
                choice=(Literal[options], ...),  # type: ignore[valid-type]
            )
        else:
            answer = create_model(
                f"NoulAnswer{index}",
                __config__=ConfigDict(extra="forbid"),
                noul=(float, ...),
            )
        fields[f"q{index}"] = (answer, Field(..., alias=name))
    return create_model(
        "DecisionAnswers",
        __config__=ConfigDict(extra="forbid", populate_by_name=True),
        **fields,
    )


def render_questions(request: DecisionRequest) -> str:
    sections: list[str] = []
    for name, question in request.questions.items():
        if question.type == "choice":
            options = "\n".join(
                f"- `{option}`: {json.dumps(description, ensure_ascii=False)}"
                for option, description in question.criteria.items()
            )
            sections.append(
                f"## {name} (choice)\n\n{question.instructions}\n\nOptions:\n{options}"
            )
        else:
            criteria = question.criteria
            if "true" in criteria and "false" in criteria:
                meaning = (
                    f"1.0 means: {criteria['true']}\n0.0 means: {criteria['false']}"
                )
            else:
                meaning = f"Criteria: {json.dumps(criteria, ensure_ascii=False)}"
            sections.append(f"## {name} (probability)\n\n{question.instructions}\n\n{meaning}")
    return "\n\n".join(sections)


def render_output_contract(request: DecisionRequest) -> str:
    lines: list[str] = []
    for name, question in request.questions.items():
        if question.type == "choice":
            options = ", ".join(json.dumps(option) for option in _choice_ids(question))
            lines.append(f'  {json.dumps(name)}: {{"choice": <one of {options}>}}')
        else:
            lines.append(f'  {json.dumps(name)}: {{"noul": <number from 0.0 to 1.0>}}')
    return (
        "Return one JSON object with exactly these keys and nothing else:\n{\n"
        + ",\n".join(lines)
        + "\n}"
    )


def to_jev_response(request: DecisionRequest, parsed: Any) -> dict:
    """structured output 의 결과를 검사하고 Jev 응답 모양으로 바꾼다."""
    if isinstance(parsed, BaseModel):
        answers = parsed.model_dump(by_alias=True)
    elif isinstance(parsed, dict):
        answers = parsed
    else:
        raise DecisionError(f"결정 model 이 읽을 수 없는 답을 냈다: {type(parsed).__name__}")

    result: dict[str, dict] = {}
    for name, question in request.questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise DecisionError(f"결정 model 이 질문 {name} 에 답하지 않았다")
        if question.type == "choice":
            choice = answer.get("choice")
            if choice not in question.criteria:
                raise DecisionError(f"결정 model 이 질문 {name} 에 없는 선택지 {choice!r} 를 골랐다")
            result[name] = {"type": "choice", "choice": choice}
        else:
            try:
                probability = float(answer.get("noul"))
            except (TypeError, ValueError) as error:
                raise DecisionError(f"결정 model 이 질문 {name} 에 숫자로 답하지 않았다") from error
            if not 0.0 <= probability <= 1.0:
                raise DecisionError(
                    f"결정 model 이 질문 {name} 에 0 과 1 사이가 아닌 {probability} 로 답했다"
                )
            result[name] = {"type": "noul", "noul": probability}
    return {"answers": result}
