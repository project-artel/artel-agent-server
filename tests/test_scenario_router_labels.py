# -*- coding: utf-8 -*-
"""정답표가 **스스로를 지키는지** 본다. 모델도 네트워크도 안 쓴다 — CI 에서 도는 부분이다.

왜 있나: `modify`↔`authoring` 경계가 v1→v7 동안 다섯 번 움직였고(run 64 시나리오 소실 ·
합치기 누출 · run 68 거절 · run 75 자리 지목 · 나누기 누출), 매번 **한 사건에 대한 패치**였다.
앞의 수정이 여전히 서는지 보는 장치가 없어서 같은 자리가 반복해 깨졌다.

이 파일이 지키는 것은 정확도가 아니라 **정답표의 무결성**이다. 정확도는 모델을 부르므로
`scripts/` 쪽 하네스가 잰다. 여기서 막는 것은 넷이다.

1. 라벨이 명세의 **어느 조항**에서 나왔는지가 모든 줄에 적혀 있고, 그 조항이 지금 프롬프트에
   실제로 존재한다. 조항이 사라지거나 다시 쓰이면 그 조항을 인용한 라벨이 전부 재검토 대상이 된다
2. 라벨 파일이 lock 과 어긋나지 않는다 — 누가 라벨을 고치고 lock 을 안 고치는 것을 막는다
3. 명세 해시가 lock 과 같다. **라벨은 프롬프트 판본에 상대적이다** — v7 을 제자리에서
   고치거나 v8 을 넣고 라벨을 그대로 두면 조용히 진리가 재정의되는 대신 여기가 터진다
4. 갈래별 최소 건수. `authoring` 을 100줄 더 넣고 "데이터셋 키웠다"가 통과하지 않게 한다
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

import pytest

DATA = pathlib.Path(__file__).parent / "data"
LABELS = DATA / "scenario_router_labels.jsonl"
LOCK = DATA / "scenario_router_labels.lock.json"
PROMPTS = pathlib.Path(__file__).parents[1] / "app" / "prompts"

ROUTES = {"greeting", "offtopic", "question", "modify", "authoring"}


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return [json.loads(l) for l in LABELS.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture(scope="module")
def lock() -> dict:
    return json.loads(LOCK.read_text(encoding="utf-8"))


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_every_row_parses_and_ids_are_unique(rows: list[dict]) -> None:
    assert rows, "정답표가 비어 있다"
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), f"중복 id: {[i for i in ids if ids.count(i) > 1][:5]}"
    for r in rows:
        assert r["input"].strip(), f"{r['id']} 입력이 비었다"
        assert r["label"] in ROUTES or r["label"] is None, f"{r['id']} 알 수 없는 라벨 {r['label']}"


def test_a_row_without_a_label_says_why(rows: list[dict]) -> None:
    """라벨을 못 붙인 줄은 `underspecified` 로 남는다. 억지로 하나를 박으면 동전 던지기를
    정답표에 굽는 것이고, 다음 판본이 그 잡음에 대해 "회귀" 로 뜬다."""
    for r in rows:
        if r["label"] is None:
            assert r["spec_status"] == "underspecified", f"{r['id']} 라벨이 없는데 이유가 없다"
            assert r["note"], f"{r['id']} underspecified 인데 설명이 없다"


def test_every_label_cites_a_rule_that_still_exists(rows: list[dict], lock: dict) -> None:
    """**이 검사가 이 파일의 존재 이유다.**

    라벨마다 명세의 조항을 인용한다. 줄 번호가 아니라 **그 조항이 하는 말**로 인용하는 이유는
    줄 번호가 편집 한 번에 밀리기 때문이다 — v6→v7 에서 나누기 절이 들어오자 뒤의 모든 번호가
    세 줄 밀렸다. 문구는 그 편집을 견딘다.
    """
    pinned = next(iter(lock["spec"]))                   # 예: scenario_router/v7/system
    body = (PROMPTS / f"{pinned}.md").read_text(encoding="utf-8")
    flat = _squeeze(body)
    for r in rows:
        if r["label"] is None:
            continue
        anchor = r.get("spec_anchor")
        assert anchor, f"{r['id']} 라벨에 근거 조항이 없다 — 근거 없는 라벨은 다음 사람이 못 믿는다"
        assert _squeeze(anchor) in flat, (
            f"{r['id']} 가 인용한 조항이 {pinned} 에 없다: {anchor!r}\n"
            f"조항이 다시 쓰였다면 그 조항을 인용한 라벨을 전부 재검토해야 한다."
        )


def test_cited_line_number_still_points_at_the_rule(rows: list[dict], lock: dict) -> None:
    """줄 번호는 파생값이지만 맞아 있어야 사람이 따라갈 수 있다. ±3줄까지 본다."""
    pinned = next(iter(lock["spec"]))
    lines = (PROMPTS / f"{pinned}.md").read_text(encoding="utf-8").splitlines()
    for r in rows:
        if not r.get("spec_anchor"):
            continue
        where = r["spec_clause"]
        assert where.startswith(pinned), f"{r['id']} 가 못 박힌 판본이 아닌 곳을 가리킨다: {where}"
        line = int(where.rsplit(":", 1)[1])
        window = _squeeze(" ".join(lines[max(0, line - 2) : line + 3]))
        assert _squeeze(r["spec_anchor"]) in window, (
            f"{r['id']} 의 줄 번호가 밀렸다: {where} 에 {r['spec_anchor']!r} 가 없다"
        )


def test_labels_match_the_lock(rows: list[dict], lock: dict) -> None:
    body = LABELS.read_text(encoding="utf-8")
    assert hashlib.sha256(body.encode()).hexdigest() == lock["labels_sha256"], (
        "정답표를 고치고 lock 을 안 고쳤다. 라벨을 바꾸는 것은 기준을 바꾸는 일이므로 "
        "흔적이 남아야 한다."
    )
    assert lock["rows"] == len(rows)


def test_spec_hash_matches_the_prompt_lock(lock: dict) -> None:
    """라벨은 프롬프트 판본에 상대적이다. 명세가 제자리에서 바뀌면 여기가 터진다."""
    prompts_lock = json.loads((PROMPTS / "prompts-lock.json").read_text(encoding="utf-8"))["prompts"]
    for name, digest in lock["spec"].items():
        assert name in prompts_lock, f"못 박은 명세가 사라졌다: {name}"
        assert prompts_lock[name] == digest, (
            f"{name} 이 제자리에서 바뀌었다. 라벨을 재검토하고 lock 을 갱신해야 한다."
        )


def test_each_route_has_enough_rows(rows: list[dict], lock: dict) -> None:
    """`authoring` 을 100줄 더 넣고 "데이터셋 키웠다" 가 통과하지 않게 한다."""
    counted: dict[str, int] = {}
    for r in rows:
        if r["label"]:
            counted[r["label"]] = counted.get(r["label"], 0) + 1
    short = {k: (counted.get(k, 0), need) for k, need in lock["minimum_per_route"].items()
             if counted.get(k, 0) < need}
    assert not short, f"갈래별 최소 건수 미달 (있는 것, 필요한 것): {short}"


def test_tainted_rows_are_excluded_from_the_denominator(rows: list[dict], lock: dict) -> None:
    """프롬프트가 답을 쥐고 있는 줄과 문맥이 필요한 줄은 정확도에 섞지 않는다.

    실트래픽만 센다 — 손으로 쓴 트립와이어는 퍼센트가 아니라 100% 게이트로 쓴다.
    """
    traffic = [r for r in rows if r["origin"].startswith("db:")]
    scored = [r for r in traffic if r["label"] and not r["label_leaked"] and not r["needs_context"]]
    assert len(scored) == lock["scored_denominator"], (
        f"분모가 lock 과 다르다: {len(scored)} vs {lock['scored_denominator']}"
    )
    for r in traffic:
        if r["label_leaked"]:
            assert r["in_prompt"], f"{r['id']} 답이 샌다면서 프롬프트에 인용돼 있지 않다"


def test_handwritten_rows_are_marked_as_such(rows: list[dict]) -> None:
    """손으로 쓴 줄은 실제 분포가 아니다. 출처가 적혀 있어야 정확도 계산에서 가릴 수 있다."""
    hand = [r for r in rows if r["origin"].startswith("handwritten:")]
    assert hand, "트립와이어가 없다 — offtopic 재발(run 68)을 잡는 장치가 사라졌다"
    for r in hand:
        assert r["origin"].split(":", 1)[1] in {"true_offtopic", "hard_negative"}, r["origin"]
    # 하드 네거티브가 진짜 네거티브여야 한다 — offtopic 이 **아닌** 것을 증명하는 줄이다.
    for r in hand:
        if r["origin"].endswith("hard_negative"):
            assert r["label"] != "offtopic", f"{r['id']} 하드 네거티브인데 라벨이 offtopic 이다"
