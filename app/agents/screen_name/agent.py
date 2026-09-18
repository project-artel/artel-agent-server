"""`screen` 하나를 읽고 이름 하나로 답하는, 한 번 부르고 버리는 agent.

## 왜 QA agent 가 아닌가

`screen_verdict` 와 같은 이유다. QA agent 는 게임을 하는 중이고, 그 agent 를 세워 이름을
짓게 하면 기다리는 동안 게임이 흘러간다. 그 런의 문맥과 예산이 자기 시나리오와 무관한
질문에 쓰이는 것도 같다.

그래서 여기에도 대화가 없다. 호출 한 번, 답 하나, 끝. 이 agent 는 같은 `scene` 의 다른
화면에 뭐라고 이름 붙였는지도 기억하지 않는다 — 기억할 것이 있다면 그것은 content map
자체이고, 그것은 저쪽이 들고 있다.

## 왜 판정과 같은 호출에 얹지 않는가

판정(`SCREEN_SELECTOR_VERDICT`)은 제안이 있을 때만 돌고, 제안은 `(scene, selector)` 마다
평생 한 번뿐이다. 대부분의 `screen` 은 제안 없이 생기므로, 얹었으면 그 screen 들이 전부
이름 없이 남는다 (ARTEL-908). 그래서 행이 새로 생기는 순간 따로 부른다.

## 형식을 어기면 지어내지 않는다

구조화 출력이 몇 번을 다시 시도해도 안 나오면 [ScreenNameError] 로 끝낸다. 부르는 쪽은
그것을 **이름 없는 답**으로 옮긴다(`app/qa/screen_name.py`). 실패가 오류가 아니다 —
이름은 표시값이라 없으면 content map 이 그 행을 이름 없이 그릴 뿐이고, 그것은 완전하고
올바른 결과다.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import Runnable

from app.agents.base import AgentContext
from app.agents.screen_name.capture import fetch_screen_capture
from app.agents.screen_name.errors import ScreenNameError
from app.agents.screen_name.prompt import build_chain_inputs, build_screen_name_prompt
from app.agents.screen_name.schemas import ProposedName, ScreenNameRequest
from app.agents.screen_name.validate import usable_name
from app.llm.chat_model import structured
from app.llm.models import LLMModel, get_model_spec

logger = logging.getLogger(__name__)

# 형식이 안 맞을 때 다시 물어보는 횟수. 판정과 같은 수이고 이유도 같다 — 여기서 오래
# 버티는 것은 QA 런과 나란히 도는 호출을 오래 붙잡는 일이고, 이쪽의 실패는 "이 화면에
# 이름을 안 붙인다" 라서 기본값과 같다.
MAX_ATTEMPTS = 3

StructuredFactory = Callable[[LLMModel], Runnable]


def _default_structured_factory(model: LLMModel) -> Runnable:
    # 이 런의 모델을 그대로 쓴다. 판정과 같은 판단이다 — 그림을 봐야 하는 일이라, 그
    # 게임을 상대할 만하다고 이미 고른 모델이 여기서도 맞다.
    #
    # `reasoning` 은 안 넘긴다. 런의 reasoning 예산은 그 런의 시나리오를 위해 고른 값이고,
    # 여기서 끌어 쓰면 이 곁일이 그 선택을 소리 없이 나눠 갖는다. 이 저장소의 다른 단발
    # agent 도 전부 이렇게 부른다.
    return structured(model, ProposedName)


@dataclass(frozen=True)
class ScreenName:
    """이름 한 건의 결과.

    `name` 이 `None` 이면 이 화면은 이름 없이 남는다. `dropped` 는 그렇게 된 것이 **답이
    있었는데 못 실어서**인 경우의 사유이고, 모델이 짓지 않기로 했으면 `None` 이다. 둘을
    나누는 이유는 `note` 에 적히는 문장이 달라야 하기 때문이다.
    """

    name: str | None
    note: str | None
    dropped: str | None


class ScreenNameAgent:
    """`screen` 하나 → 이름 하나.

    ``structured_factory`` 는 테스트가 실제 모델 대신 정해진 답을 물릴 수 있게 열어 둔
    자리다. `ScreenVerdictAgent` 와 같은 이음매다.
    """

    def __init__(
        self,
        structured_factory: StructuredFactory | None = None,
        prompt_version: str | None = None,
    ) -> None:
        self._prompt = build_screen_name_prompt(prompt_version)
        self._structured_factory = structured_factory or _default_structured_factory

    async def run(self, request: ScreenNameRequest, context: AgentContext) -> ScreenName:
        captures = (
            await fetch_screen_capture(request.screen)
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
                context.trace_config("screen-name"),
            )
        except OutputParserException as error:
            raise ScreenNameError(
                f"the model did not answer in the required shape: {error}"
            ) from error

        name, dropped = usable_name(drafted, request.screen)
        if dropped is not None:
            logger.warning(
                "[screen-name] dropped the name for screen %s: %s",
                request.screen.screen_id,
                dropped,
            )
        note = (drafted.note or "").strip() or None
        return ScreenName(name=name, note=note, dropped=dropped)
