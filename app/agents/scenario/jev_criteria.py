# -*- coding: utf-8 -*-
"""결정 전용 라우터에게 주는 **선택지 명세**를 판본 디렉터리에서 읽는다.

명세는 `app/prompts/scenario_router/<판본>/criteria.json` 에 있다. 코드에 두지 않는 이유는
그것이 같은 디렉터리의 `system.md` 산문을 **옮겨 적은 것**이기 때문이다 — 둘이 어긋나면
측정이 거짓이 되는데, 코드에 두면 둘을 묶는 것이 사람의 기억뿐이다. 같은 판본 디렉터리에
두면 판본이 둘을 묶고 `prompts-lock.json` 이 조용한 변경을 잡는다.

`.md` 가 아니라 `.json` 인 이유는 결정 전용 모델이 선택지를 **구조로** 받기 때문이다. 산문
템플릿이 아니라 코드가 읽는 값이므로 역할(role)도 아니고 placeholder 검사도 할 것이 없다 —
그래서 로더가 `load_data`/`data_in` 으로 따로 다룬다.

**칸을 다 채워야 한다.** 1차 측정에서 이 명세를 세 줄로 요약해 줬더니 `question` 갈래가
6/10 이었다. 산문 전문을 받는 Haiku 와 요약을 받는 결정 모델을 견준 셈이었다. 칸을 채우자
10/10 이 됐고 그 효과가 tune·test 양쪽에서 같이 나왔다(실측 2026-10-06, ARTEL-944).
"""

from __future__ import annotations

from typing import Any

from app.prompts import load_data

AGENT = "scenario_router"
NAME = "criteria"


def _spec() -> dict[str, Any]:
    """읽은 것을 그대로 돌려준다. 캐시는 로더가 쥔다(`_read_data` 의 `lru_cache`)."""
    spec = load_data(AGENT, NAME)
    if not isinstance(spec, dict):
        raise TypeError(f"{AGENT}/{NAME}.json 이 객체가 아니다: {type(spec).__name__}")
    return spec


def route_criteria() -> dict[str, Any]:
    return _spec()["route_criteria"]


def route_instructions() -> str:
    return _spec()["route_instructions"]


def target_criteria() -> dict[str, Any]:
    return _spec()["target_criteria"]


def target_instructions() -> str:
    return _spec()["target_instructions"]
