# -*- coding: utf-8 -*-
"""결정 전용 모델 라우터(ARTEL-944). 모델도 네트워크도 안 쓴다 — 응답을 손으로 지어 넣는다.

여기서 지키는 것은 분류 성능이 아니라 **배선과 규칙**이다. 성능은 실제 호출이 드는 일이라
`.poc/jev/final.py` 가 재고, 그 수는 이슈와 보고서에 남는다.

막는 것 넷.

1. 되묻기 규칙이 **한쪽으로만** 걸린다. 반대 방향에도 걸면 163줄에서 15건을 되묻고 13건이
   헛경보였다(실측 2026-10-06). 그 비대칭이 코드에 남아 있는지 본다
2. 호출이 실패해도 턴이 죽지 않고 `authoring` 으로 떨어진다 — 그 갈래는 아무것도 덮어쓰지 않는다
3. 선택지 명세가 **조용히 바뀌지 않는다.** 산문(`scenario_router/v7`)을 옮겨 적은 것이라
   어긋나면 측정이 거짓이 된다
4. 설정 한 줄로 되돌아간다
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.scenario import jev_criteria as spec
from app.agents.scenario.router import JevScenarioRouter, Route, ScenarioRouter

# 이 값을 바꾸려면 `scenario_router/v7/system.md` 와 맞는지 확인하고 함께 바꾼다.
PINNED_DIGEST = "8b0920dd2eb4554c6ebee346c22f054ac5ab1bbc69f5de944282edd4e99da28e"


def _answers(route: str, target: str) -> dict:
    return {"route": {"choice": route}, "target": {"choice": target}}


def _router(route: str, target: str) -> JevScenarioRouter:
    made = JevScenarioRouter()

    async def fixed(_: str) -> dict:
        return _answers(route, target)

    made._decide = fixed  # type: ignore[method-assign]
    return made


# ── 되묻기 규칙 ───────────────────────────────────────────────────────────────


def test_modify_on_an_existing_scenario_goes_straight_through() -> None:
    verdict = asyncio.run(_router("modify", "existing").route("이 시나리오에서 3번 스텝 빼줘"))
    assert verdict.route is Route.MODIFY


@pytest.mark.parametrize("target", ["new", "neither"])
def test_modify_asks_back_when_the_target_is_not_an_existing_scenario(target: str) -> None:
    """되돌릴 수 없는 방향을 막는 자리.

    실측에서 범위 지정("게임 시작 후 첫 스테이지 전까지만")이 `modify` 로 새어 기존 본문을
    교체할 뻔했다. 그 줄들의 대상은 `new` 였다 — 두 답이 어긋나는 것이 신호다.
    """
    verdict = asyncio.run(_router("modify", target).route("게임 시작 후 첫 스테이지 전까지만"))
    assert verdict.route is Route.ASK


def test_authoring_on_an_existing_target_is_left_alone() -> None:
    """**반대 방향에는 조건을 걸지 않는다.**

    걸어 보니 163줄에서 13건이 헛경보였다. `authoring` 은 아무것도 덮어쓰지 않으므로 틀려도
    시나리오가 하나 더 생길 뿐이고, 되묻는 비용이 그보다 크다.
    """
    verdict = asyncio.run(_router("authoring", "existing").route("첫번째 스테이지를 클리어해"))
    assert verdict.route is Route.AUTHORING


@pytest.mark.parametrize("route", ["greeting", "offtopic", "question", "authoring"])
def test_the_other_routes_pass_through_whatever_the_target_says(route: str) -> None:
    for target in ("existing", "new", "neither"):
        verdict = asyncio.run(_router(route, target).route("아무 말"))
        assert verdict.route is Route(route)


# ── 실패와 폴백 ───────────────────────────────────────────────────────────────


def test_a_failed_call_falls_back_to_authoring_instead_of_killing_the_turn() -> None:
    made = JevScenarioRouter()

    async def boom(_: str) -> dict:
        raise RuntimeError("결정 모델 죽음")

    made._decide = boom  # type: ignore[method-assign]
    verdict = asyncio.run(made.route("전투도 짜줘"))
    assert verdict.route is Route.AUTHORING


def test_an_unknown_route_name_falls_back_instead_of_raising() -> None:
    """선택지에 없는 이름이 와도 턴은 산다. 폴백이 분류 실패를 흡수한다."""
    made = JevScenarioRouter()

    async def nonsense(_: str) -> dict:
        return _answers("무언가", "existing")

    made._decide = nonsense  # type: ignore[method-assign]
    verdict = asyncio.run(made.route("아무 말"))
    assert verdict.route is Route.AUTHORING


def test_the_reply_field_is_left_empty() -> None:
    """이 모델은 자유 텍스트를 못 낸다. 문구는 `ScenarioReplier` 가 쓴다."""
    verdict = asyncio.run(_router("greeting", "neither").route("안녕"))
    assert verdict.reply == ""


# ── 명세와 설정 ───────────────────────────────────────────────────────────────


def test_the_criteria_have_not_changed_without_anyone_noticing() -> None:
    assert spec.digest() == PINNED_DIGEST, (
        "선택지 명세가 바뀌었다. 이것은 `scenario_router/v7/system.md` 의 산문을 옮겨 적은 "
        "것이므로, 산문과 맞는지 확인하고 측정을 다시 한 뒤 PINNED_DIGEST 를 갱신한다."
    )


def test_every_route_option_is_fully_described() -> None:
    """1차 측정에서 `question` 이 6/10 이었던 원인이 빈 칸이었다."""
    for name, option in spec.ROUTE_CRITERIA.items():
        assert option.get("what"), f"{name}: what 이 비었다"
        assert option.get("examples"), f"{name}: examples 가 비었다"
    for name in ("question", "greeting", "authoring", "modify", "offtopic"):
        assert spec.ROUTE_CRITERIA[name].get("not_for"), f"{name}: not_for 가 비었다"


def test_the_route_options_match_the_five_routes_the_code_knows() -> None:
    """`ask` 는 선택지가 아니다 — 모델이 고르는 답이 아니라 두 답이 어긋날 때 코드가 내는 결과다."""
    assert set(spec.ROUTE_CRITERIA) == {r.value for r in Route} - {Route.ASK.value}


def test_the_engine_switch_picks_the_router(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings, get_settings
    from app.sessions.service import _build_router

    def with_engine(engine: str, enabled: bool = True) -> Settings:
        base = get_settings()
        return base.model_copy(update={
            "scenario_router_engine": engine, "scenario_router_enabled": enabled,
        })

    import app.sessions.service as service

    monkeypatch.setattr(service, "get_settings", lambda: with_engine("jev"))
    assert isinstance(_build_router(), JevScenarioRouter)

    monkeypatch.setattr(service, "get_settings", lambda: with_engine("haiku"))
    made = _build_router()
    assert isinstance(made, ScenarioRouter) and not isinstance(made, JevScenarioRouter)

    monkeypatch.setattr(service, "get_settings", lambda: with_engine("jev", enabled=False))
    assert _build_router() is None
