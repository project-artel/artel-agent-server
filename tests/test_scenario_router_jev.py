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
from app.prompts import resolve_version



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


def test_the_criteria_are_locked_like_a_prompt() -> None:
    """명세가 조용히 바뀌지 않는다 — **전용 핀이 아니라 `prompts-lock.json` 이 잡는다.**

    처음에는 이 모듈이 `digest()` 로 자기 해시를 들고 있었다. 그건 lock 이 이미 하는 일을
    다시 만든 것이었고, 더 나쁘게는 **산문과의 어긋남을 사람 기억에 맡겼다** — criteria 는
    같은 판본의 `system.md` 를 옮겨 적은 것이라 둘이 따로 움직이면 측정이 거짓이 된다.
    판본 디렉터리로 옮기면 판본이 둘을 묶는다.
    """
    from app.prompts.lock import compute_lock, read_lock

    computed, locked = compute_lock(), read_lock()
    key = f"{spec.AGENT}/{resolve_version(spec.AGENT)}/{spec.NAME}"
    assert key in computed["data"], f"{key} 가 lock 계산에 없다 — data_in 이 못 본다"
    assert locked.get("data", {}).get(key) == computed["data"][key], (
        "criteria.json 이 바뀌었는데 lock 을 안 고쳤다. "
        "`python -m app.prompts.lock --write` 를 돌리고, 같은 판본의 system.md 산문과 "
        "맞는지도 확인한다."
    )


def test_the_criteria_live_beside_the_prose_they_transcribe() -> None:
    """criteria 와 산문이 **같은 판본 디렉터리**에 있다.

    이것이 어긋남을 막는 실체다. 다른 자리에 두면 산문을 v8 로 올리고 criteria 를 그대로
    두는 일이 조용히 지나간다.
    """
    from app.prompts import PROMPTS_ROOT, data_in, roles_in

    version = resolve_version(spec.AGENT)
    assert spec.NAME in data_in(spec.AGENT, version)
    assert "system" in roles_in(spec.AGENT, version)
    assert (PROMPTS_ROOT / spec.AGENT / version / f"{spec.NAME}.json").is_file()


def test_every_route_option_is_fully_described() -> None:
    """1차 측정에서 `question` 이 6/10 이었던 원인이 빈 칸이었다."""
    for name, option in spec.route_criteria().items():
        assert option.get("what"), f"{name}: what 이 비었다"
        assert option.get("examples"), f"{name}: examples 가 비었다"
    for name in ("question", "greeting", "authoring", "modify", "offtopic"):
        assert spec.route_criteria()[name].get("not_for"), f"{name}: not_for 가 비었다"


def test_the_route_options_match_the_five_routes_the_code_knows() -> None:
    """`ask` 는 선택지가 아니다 — 모델이 고르는 답이 아니라 두 답이 어긋날 때 코드가 내는 결과다."""
    assert set(spec.route_criteria()) == {r.value for r in Route} - {Route.ASK.value}


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
