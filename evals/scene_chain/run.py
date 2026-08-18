"""측정 러너 — arm 별로 모델을 부르고, 인용을 대조하고, 수치를 파일로 남긴다.

    PYTHONPATH=. python evals/scene_chain/run.py \
        --content-map evals/scene_chain/data/golden-content-map.json \
        --capture /path/to/wv-editor-latest.json \
        --pseudo-cs /path/to/wv-cs \
        --arm all --repeats 2

모델을 부르지 않고 이미 받아 둔 응답으로 채점만 다시 하려면:

    ... --replay evals/scene_chain/output/<파일>.json

`--replay` 는 경로를 명시로 받는다. 파일명 규칙으로 찾아내면 어느 응답을 채점한 것인지
나중에 댈 수 없다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from evals.scene_chain.arms import Arm, ArmInput, build_arm_input, read_pseudo_cs
from evals.scene_chain.citations import MalformedOutput, check_chain, parse_chains
from evals.scene_chain.evidence import Capture, ContentMap
from evals.scene_chain.scoring import (
    RunScore,
    join_baseline_checks,
    load_golden_chains,
    score_run,
    summarize,
)

HERE = Path(__file__).parent
GOLDEN_CHAINS = HERE / "data" / "golden-chains.json"
DEFAULT_OUTPUT = HERE / "output"

# 모델이 JSON 을 코드펜스에 싸서 내는 일이 잦다. 응답 원문은 그대로 저장하고 파싱만 벗긴다.
FENCE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)


def strip_fence(text: str) -> str:
    match = FENCE.match(text)
    return match.group("body") if match else text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="씬 명세만으로 기능을 잇는 능력 측정")
    parser.add_argument("--content-map", type=Path, required=True)
    parser.add_argument("--pseudo-cs", type=Path, required=True, help="wv2cs.py 가 낸 디렉터리")
    # 없으면 in-capture 단이 죽고, content_map 밖에서만 근거가 나오는 골든(SC-6·SC-7)이
    # 어느 arm 에서도 성립하지 않는다. 그러면 arm 이 귀무가설을 넘을 길이 사라지는데
    # 실행은 조용히 성공한다 — 재지 못한 것이 잰 것처럼 보이는 제일 나쁜 실패다.
    parser.add_argument("--capture", type=Path, required=True, help="근거 캡처 JSON")
    parser.add_argument("--golden", type=Path, default=GOLDEN_CHAINS)
    parser.add_argument("--arm", default="all", choices=[*(arm.value for arm in Arm), "all"])
    parser.add_argument("--model", default="anthropic/claude-sonnet-5")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--replay",
        type=Path,
        nargs="+",
        default=None,
        help="모델을 부르지 않고 이 결과 파일들로 재채점. 채점 규칙이 바뀌면 지난 실행 전부를 같은 자로 다시 잰다",
    )
    parser.add_argument("--dry-run", action="store_true", help="프롬프트만 조립하고 크기를 찍는다")
    return parser.parse_args(argv)


def require_readable(*paths: Path) -> None:
    """입력이 하나라도 없으면 즉시 죽는다. 절반만 채워진 측정이 제일 나쁘다."""
    missing = [str(path) for path in paths if path is not None and not path.exists()]
    if missing:
        print(f"[scene-chain] 입력을 읽을 수 없다: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)


async def call_model(model_name: str, arm_input: ArmInput) -> dict:
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.llm.chat_model import TEMPERATURE, build_chat_model
    from app.llm.models import LLMModel

    chat = build_chat_model(LLMModel(model_name))
    started = time.monotonic()
    response = await chat.ainvoke(
        [SystemMessage(content=arm_input.system), HumanMessage(content=arm_input.human)]
    )
    duration = time.monotonic() - started

    usage = response.usage_metadata or {}
    token_usage = (response.response_metadata or {}).get("token_usage") or {}
    cost = token_usage.get("cost")
    return {
        "text": response.text if isinstance(response.text, str) else str(response.content),
        "durationSeconds": round(duration, 3),
        "inputTokens": usage.get("input_tokens", 0),
        "outputTokens": usage.get("output_tokens", 0),
        "costUsd": float(cost) if isinstance(cost, (int, float)) else None,
        "temperature": TEMPERATURE,
        "model": model_name,
    }


def grade(
    arm: Arm,
    repeat: int,
    raw_text: str,
    content_map: ContentMap,
    capture: Capture | None,
    golden,
) -> tuple[RunScore, list[dict]]:
    try:
        chains = parse_chains(strip_fence(raw_text))
    except (MalformedOutput, json.JSONDecodeError) as error:
        score = RunScore(arm=arm.value, repeat=repeat, golden_total=len(golden), golden_correct=0)
        # 스키마를 어긴 응답은 0점이 아니라 "측정 불가"다. 수치와 나란히 남긴다.
        score.malformed_output = str(error)
        return score, []

    checks = [check_chain(chain, content_map, capture) for chain in chains]
    detail = [
        {
            "summary": check.chain.summary,
            "passed": check.passed,
            "citations": [
                {
                    "capabilityId": item.citation.capability_id,
                    "unit": item.citation.unit,
                    "role": item.citation.role.value,
                    "via": item.citation.via,
                    "verdict": item.verdict.value,
                    "resolved": sorted(item.capability_ids),
                    "reason": item.reason,
                }
                for item in check.checks
            ],
        }
        for check in checks
    ]
    return score_run(arm.value, repeat, checks, golden, content_map), detail


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    require_readable(args.content_map, args.pseudo_cs, args.golden, args.capture)

    content_map = ContentMap.load(args.content_map)
    capture = Capture.load(args.capture)
    golden = load_golden_chains(args.golden)
    content_map_text = args.content_map.read_text(encoding="utf-8")
    pseudo_cs_text = read_pseudo_cs(args.pseudo_cs)
    arms = list(Arm) if args.arm == "all" else [Arm(args.arm)]

    if args.replay:
        require_readable(*args.replay)
        replayed: list[RunScore] = []
        details = {}
        for path in args.replay:
            saved = json.loads(path.read_text(encoding="utf-8"))
            score, detail = grade(
                Arm(saved["arm"]),
                saved["repeat"],
                saved["response"]["text"],
                content_map,
                capture,
                golden,
            )
            score.duration_seconds = saved["response"].get("durationSeconds", 0.0)
            score.cost_usd = saved["response"].get("costUsd")
            score.input_tokens = saved["response"].get("inputTokens", 0)
            score.output_tokens = saved["response"].get("outputTokens", 0)
            replayed.append(score)
            details[path.name] = {"score": score.as_dict(), "chains": detail}
        replayed.append(_baseline(content_map, golden))
        print(
            json.dumps(
                {"runs": details, "summary": summarize(replayed)}, ensure_ascii=False, indent=2
            )
        )
        return 0

    if args.dry_run:
        for arm in arms:
            arm_input = build_arm_input(arm, content_map_text, pseudo_cs_text)
            print(
                f"arm {arm.value}: human {len(arm_input.human)} chars "
                f"(~{arm_input.approximate_tokens} tokens) evidence {arm_input.evidence_sha256[:12]}"
            )
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    scores: list[RunScore] = []
    for arm in arms:
        arm_input = build_arm_input(arm, content_map_text, pseudo_cs_text)
        for repeat in range(1, args.repeats + 1):
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            print(f"[scene-chain] arm {arm.value} repeat {repeat} …", file=sys.stderr, flush=True)
            response = asyncio.run(call_model(args.model, arm_input))
            score, detail = grade(
                arm, repeat, response["text"], content_map, capture, golden
            )
            score.duration_seconds = response["durationSeconds"]
            score.cost_usd = response["costUsd"]
            score.input_tokens = response["inputTokens"]
            score.output_tokens = response["outputTokens"]
            scores.append(score)

            path = args.output_dir / f"{stamp}-{arm.value}-{repeat}.json"
            path.write_text(
                json.dumps(
                    {
                        "arm": arm.value,
                        "repeat": repeat,
                        "model": response["model"],
                        "temperature": response["temperature"],
                        # 프롬프트 본문과 입력 해시. 이것이 없으면 지난 수치를 다시 설명할 수 없다.
                        "prompt": {"system": arm_input.system, "human": arm_input.human},
                        "evidenceSha256": arm_input.evidence_sha256,
                        "response": response,
                        "score": score.as_dict(),
                        "chains": detail,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"[scene-chain] -> {path}", file=sys.stderr)

    # 귀무가설을 같은 표에 싣는다. 모델 없이 조인만 돌린 답보다 못한 arm 이 있는지가
    # 이 실험이 답해야 할 첫 질문이고, 옆에 없으면 아무도 그것을 묻지 않는다.
    scores.append(_baseline(content_map, golden))
    print(json.dumps(summarize(scores), ensure_ascii=False, indent=2))
    return 0


def _baseline(content_map: ContentMap, golden) -> RunScore:
    return score_run("join-baseline", 0, join_baseline_checks(content_map), golden, content_map)


if __name__ == "__main__":
    raise SystemExit(main())
