"""제안 하나를 읽고 whitelist 항목으로 답하는, 한 번 부르고 버리는 agent.

## 왜 QA agent 가 아닌가

QA agent 는 게임을 하는 중이다. 그 agent 를 세워 판정시키면 기다리는 동안 게임이
흘러가고, 그 런의 문맥과 예산이 자기 시나리오와 무관한 질문에 쓰인다. 판정은 몇 초짜리
질문이지만 QA 런은 그 몇 초 동안 서 있을 수 없다.

그래서 여기에는 대화가 없다. 호출 한 번, 답 하나, 끝. 이 agent 는 이전 제안에 뭐라고
답했는지도 기억하지 않는다 — 기억할 것이 있다면 그것은 whitelist 자체이고, 그것은 저쪽이
들고 있다.

## 형식을 어기면 지어내지 않는다

구조화 출력이 몇 번을 다시 시도해도 안 나오면 [ScreenVerdictError] 로 끝낸다. 부르는 쪽은
그것을 **항목 없는 답**으로 옮긴다(`app/qa/screen_verdict.py`) — 스키마를 채우려고 항목을
만들어 내는 것이 이 경로에서 가장 비싼 실수다. 빈 답은 지도를 종전대로 두지만, 지어낸
항목은 그 `scene` 의 화면을 다음 관측부터 갈라 놓고 되돌릴 수 없다.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import Runnable

from app.agents.base import AgentContext
from app.agents.screen_verdict.capture import fetch_proposal_captures
from app.agents.screen_verdict.errors import ScreenVerdictError
from app.agents.screen_verdict.prompt import build_chain_inputs, build_screen_verdict_prompt
from app.agents.screen_verdict.schemas import (
    ProposedVerdict,
    ScreenVerdictRequest,
)
from app.agents.screen_verdict.validate import DroppedEntry, usable_entries
from app.llm.chat_model import build_chat_model, select_structured_method
from app.llm.models import LLMModel, get_model_spec
from app.qa.envelope import ScreenSelectorEntry

logger = logging.getLogger(__name__)

# 형식이 안 맞을 때 다시 물어보는 횟수. `knowledge_query` 보다 적다 — 그쪽은 실패가 곧
# 검색되지 않는 지식 항목이지만, 이쪽의 실패는 "이 후보들을 목록에 안 넣는다" 이고 그것이
# 기본값과 같다. 여기서 오래 버티는 것은 QA 런과 나란히 도는 호출을 오래 붙잡는 일이다.
MAX_ATTEMPTS = 3

StructuredFactory = Callable[[LLMModel], Runnable]


def _default_structured_factory(model: LLMModel) -> Runnable:
    # 이 런의 모델을 그대로 쓴다. 요약(`qa_compaction`)이 값싼 모델로 고정된 것과 다른
    # 판단이고, 차이는 일의 성격이다 — 요약은 압축이지만 이것은 판단이고, 게다가 그림을
    # 봐야 하는 판단이다.
    #
    # `reasoning` 은 안 넘긴다. 런의 reasoning 예산은 그 런의 시나리오를 위해 고른 값이고,
    # 여기서 끌어 쓰면 이 곁일이 그 선택을 소리 없이 나눠 갖는다. 이 저장소의 다른 단발
    # agent 도 전부 이렇게 부른다.
    chat = build_chat_model(model)
    if select_structured_method(model) == "json_schema":
        return chat.with_structured_output(
            ProposedVerdict, method="json_schema", strict=True
        )
    return chat.with_structured_output(ProposedVerdict, method="json_mode")


@dataclass(frozen=True)
class ScreenVerdict:
    """판정 하나의 결과.

    버린 것을 결과에 들고 다닌다. 버림이 조용하면 "왜 이 후보만 목록에 없나" 를 되짚을
    자리가 어디에도 없고, 그것은 모델이 자꾸 후보 밖을 가리키기 시작해도 아무도 모른다는
    뜻이다.
    """

    entries: list[ScreenSelectorEntry]
    note: str | None
    dropped: list[DroppedEntry]


class ScreenVerdictAgent:
    """제안 하나 → whitelist 항목들.

    ``structured_factory`` 는 테스트가 실제 모델 대신 정해진 답을 물릴 수 있게 열어 둔
    자리다. `KnowledgeQueryAgent` 와 같은 이음매다.
    """

    def __init__(
        self,
        structured_factory: StructuredFactory | None = None,
        prompt_version: str | None = None,
    ) -> None:
        self._prompt = build_screen_verdict_prompt(prompt_version)
        self._structured_factory = structured_factory or _default_structured_factory

    async def run(
        self, request: ScreenVerdictRequest, context: AgentContext
    ) -> ScreenVerdict:
        captures = (
            await fetch_proposal_captures(request.proposal)
            if get_model_spec(request.model).supports_vision
            else []
        )
        chain = (self._prompt | self._structured_factory(request.model)).with_retry(
            retry_if_exception_type=(OutputParserException,),
            stop_after_attempt=MAX_ATTEMPTS,
        )
        try:
            drafted = await chain.ainvoke(
                build_chain_inputs(request, captures),
                context.trace_config("screen-selector-verdict"),
            )
        except OutputParserException as error:
            raise ScreenVerdictError(
                f"the model did not answer in the required shape: {error}"
            ) from error

        entries, dropped = usable_entries(drafted.entries, request.proposal.candidates)
        for item in dropped:
            logger.warning(
                "[screen-verdict] dropped an entry (%s): %r", item.reason, item.entry
            )
        note = (drafted.note or "").strip() or None
        return ScreenVerdict(entries=entries, note=note, dropped=dropped)
