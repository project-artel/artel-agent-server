# -*- coding: utf-8 -*-
"""입구 라우터가 직전 턴을 보고, 아무것도 하지 말라는 말과 할 수 없는 일을 따로 받는다 (ARTEL-942).

막는 것 넷.

1. 결정 전용 모델에 직전 턴이 **칸을 나눠** 실린다. 산문 한 줄로 이으면 누가 한 말인지 섞인다
2. 직전 턴은 대화 기록의 **마지막 Agent 답과 그 앞 사용자 말**이고, 메시지당 상한으로 잘린다
3. "하지 마"·"이슈 만들어 줘" 가 시나리오를 쓰지 않고 한마디로 끝난다 — 갈래가 없던 동안 앞의 것은
   modify 로 가서 시나리오를 다시 쓸 수 있었다
4. 선택지 순서가 그대로다 — 순서가 모델 답을 바꾼다
"""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage

from app.agents.scenario import jev_criteria as spec
from app.agents.scenario.agent import ScenarioAgent, _previous_turn
from app.agents.scenario.replier import FALLBACK, ScenarioReplier
from app.agents.scenario.router import JevScenarioRouter, PreviousTurn, Route, RouteVerdict, ScenarioRouter
from app.agents.scenario.workflow import _history_limits
from tests.test_agents_scenario import _CTX, _channel, _request
from tests.test_scenario_router import _capturing_factory


def _capturing_jev(seen: list) -> JevScenarioRouter:
    made = JevScenarioRouter()

    async def fixed(state: dict) -> dict:
        seen.append(state)
        return {"route": {"choice": "authoring"}, "target": {"choice": "new"}}

    made._decide = fixed  # type: ignore[method-assign]
    return made


def test_jev_state_carries_the_previous_turn_in_separate_fields() -> None:
    seen: list = []
    previous = PreviousTurn(user="게임 시작 후 맵까지 짜줘", assistant="시나리오를 저장했어요. 새로 만들까요, 기존 거에 넣을까요?")

    asyncio.run(_capturing_jev(seen).route("후자로 해줘", previous=previous))

    assert seen == [{
        "user_message": "후자로 해줘",
        "previous_user_message": previous.user,
        "previous_assistant_message": previous.assistant,
    }]


def test_jev_state_is_the_message_alone_on_the_first_turn() -> None:
    seen: list = []

    asyncio.run(_capturing_jev(seen).route("안녕"))

    assert seen == [{"user_message": "안녕"}]


def test_previous_turn_is_the_last_reply_and_the_message_before_it() -> None:
    request = _request(user_input="그거 말고", history=[
        HumanMessage("첫 요청"), AIMessage("첫 답"),
        HumanMessage("둘째 요청"), AIMessage("둘째 답"),
    ])

    assert _previous_turn(request) == PreviousTurn(user="둘째 요청", assistant="둘째 답")


def test_previous_turn_is_cut_at_the_per_message_cap() -> None:
    _, per_message = _history_limits()
    request = _request(user_input="응", history=[HumanMessage("요청"), AIMessage("가" * (per_message + 50))])

    turn = _previous_turn(request)

    assert turn is not None
    assert turn.assistant.startswith("가" * per_message)
    assert turn.assistant.endswith("…(이하 생략)")
    assert len(turn.assistant) < per_message + 50


def test_previous_turn_is_none_without_a_reply() -> None:
    assert _previous_turn(_request(user_input="안녕")) is None


def _fixed(route: Route) -> ScenarioRouter:
    async def caller(prompt: str) -> RouteVerdict:
        return RouteVerdict(route=route)

    return ScenarioRouter(caller=caller)


def test_decline_and_unsupported_end_with_one_reply_and_no_scenario(monkeypatch) -> None:
    asked: list = []

    async def write(self, route, user_input):  # 문구 노드의 모델 호출을 대신한다
        asked.append(route)
        return ""  # 문구를 못 받으면 고정 문구로 떨어져야 한다

    monkeypatch.setattr(ScenarioReplier, "write", write)
    for route, said in ((Route.DECLINE, "아니 둘 다 하지 마"), (Route.UNSUPPORTED, "QA 이슈 하나 만들어줘")):
        seen: dict = {}
        agent = ScenarioAgent(agent_factory=_capturing_factory(seen), router=_fixed(route))

        out = asyncio.run(agent.run(_request(user_input=said), _CTX, _channel()))

        assert out.scenarios == []
        assert out.message
        assert "system_prompt" not in seen  # 저작 루프가 돌지 않았다
    assert asked == [Route.DECLINE, Route.UNSUPPORTED]


def test_the_reply_node_has_a_fallback_for_every_reply_only_route() -> None:
    assert {Route.GREETING, Route.OFFTOPIC, Route.DECLINE, Route.UNSUPPORTED} <= set(FALLBACK)


def test_route_choices_keep_their_order() -> None:
    assert list(spec.route_criteria()) == [
        "greeting", "offtopic", "decline", "unsupported", "question", "modify", "authoring", "case",
    ]
    assert {choice for choice in spec.route_criteria()} <= {route.value for route in Route}
