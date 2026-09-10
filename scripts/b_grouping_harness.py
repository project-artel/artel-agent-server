# -*- coding: utf-8 -*-
"""B 하네스 — 워크플로 재편 플랜 Step 0.

묶기·순서 노드(B)를 떼어 단발로 부르고, 오케의 실험 채점 창구
(`POST :8081/internal/experiment/authoring/score` — reconcile save=false)로 걷기 잣대
채점을 받는다. 판 전체를 돌리지 않고 B 의 품질 분포를 재는 것이 목적이다.

재료는 실제 세션 스냅샷(케이스 88건 + 도달표 + 시작값)이라 본선과 같은 정보를 본다.
프롬프트 조립은 본선 렌더 함수를 재사용한다 — 두 벌을 만들지 않는다.

usage:
  uv run python scripts/b_grouping_harness.py --session <session.json> \
      --run-id <scratch run> --project-id 1 --app-user-id 1 --reps 10 \
      --out <dir> [--prompts narrow,journey,full]
"""

import argparse
import json
import time
import urllib.request
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.scenario.cases import render_game_shape, render_test_case_list
from app.agents.scenario.schemas import SceneEdge, TestCaseListItem
from app.llm.chat_model import BEDROCK_PREFIX, build_chat_model
from app.llm.models import LLMModel

PROMPTS = {
    "narrow": "첫 전투가 제대로 도는지 보고 싶어",
    "journey": "첫 전투부터 엔딩까지 짜 줘",
    "full": "전부 커버해줘",
}

MODEL = LLMModel.claude_haiku_4_5_bedrock


class Group(BaseModel):
    title: str = Field(description="플레이어가 한 일처럼 읽히는 여정 제목")
    case_ids: list[int] = Field(description="실행 순서 그대로의 케이스 id")


class GroupingPlan(BaseModel):
    """B 의 출력 계약 — 묶기와 순서만. 문장은 다음 노드의 일이다."""

    groups: list[Group]
    note: str = Field(default="", description="범위 해석을 한 줄로 — 무엇을 담고 무엇을 뺐나")


def b_prompt(session: dict, user_request: str) -> str:
    entries = [TestCaseListItem(**c) for c in session["test_case_list"]]
    edges = [SceneEdge(**e) for e in session["scene_edges"]]
    shape = render_game_shape(entries, session.get("entry_scene"), edges, session.get("starting_values"))
    cases = render_test_case_list(entries)
    return (
        "You are the grouping-and-ordering stage of a game QA scenario authoring pipeline.\n"
        "Your ONLY job: decide which cases belong together as journeys, and in what order\n"
        "each journey runs. Do NOT write step sentences — a later stage does that.\n\n"
        "One journey = what a player does in a single sitting, ordered so that what one case\n"
        "leaves behind is where the next one starts. Use the routes and boot values below —\n"
        '"on its own" transitions cannot be instructed; the player crosses them by playing,\n'
        "which is fine: just keep the order playable.\n\n"
        f"=== GAME SHAPE ===\n{shape}\n\n=== CASES ===\n{cases}\n\n"
        f"=== USER REQUEST ===\n{user_request}\n\n"
        "Cover exactly what the request asks — no more, no less. Answer via the schema."
    )


def call_b(prompt: str):
    chat = build_chat_model(MODEL, None, cache_prompt=True)
    assert MODEL.value.startswith(BEDROCK_PREFIX)
    chain = chat.with_structured_output(GroupingPlan, method="json_schema")
    started = time.time()
    plan = chain.invoke(prompt)
    return plan, time.time() - started


def score(orche: str, run_id: int, project_id: int, app_user_id: int, plan: GroupingPlan) -> dict:
    payload = json.dumps({
        "runId": run_id, "projectId": project_id, "appUserId": app_user_id,
        "groups": [{"title": g.title, "caseIds": g.case_ids} for g in plan.groups],
    }).encode()
    req = urllib.request.Request(
        f"{orche}/internal/experiment/authoring/score", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--project-id", type=int, default=1)
    ap.add_argument("--app-user-id", type=int, default=1)
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--prompts", default="narrow,journey,full")
    ap.add_argument("--orche", default="http://localhost:8081")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    session = json.loads(Path(args.session).read_text())
    all_ids = {c["id"] for c in session["test_case_list"]}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for name in args.prompts.split(","):
        prompt = b_prompt(session, PROMPTS[name])
        for rep in range(1, args.reps + 1):
            plan, took = call_b(prompt)
            row = {
                "prompt": name, "rep": rep, "seconds": round(took, 1),
                "groups": [{"title": g.title, "case_ids": g.case_ids} for g in plan.groups],
                "note": plan.note,
                "unknown_ids": sorted(
                    {i for g in plan.groups for i in g.case_ids} - all_ids
                ),
            }
            try:
                row["score"] = score(args.orche, args.run_id, args.project_id, args.app_user_id, plan)
            except Exception as error:  # noqa: BLE001 — 채점 실패도 기록이 남아야 한다
                row["score_error"] = str(error)
            path = out / f"{name}-{rep:02d}.json"
            path.write_text(json.dumps(row, ensure_ascii=False, indent=1))
            summary = row.get("score", {})
            print(
                f"{name} #{rep}: {took:5.1f}s · 묶음 {len(plan.groups)} · "
                f"어긋남 {len(summary.get('contradicted', []))} · 조각 {summary.get('checkedCount', '?')} · "
                f"유령 {len(row['unknown_ids'])}",
                flush=True,
            )


if __name__ == "__main__":
    main()
