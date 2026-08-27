"""접두 캐시가 대화를 따라 자라는지 OpenRouter 에 직접 물어본다.

에이전트도 게임도 배포도 필요 없다. 캐시를 건드리는 변경마다 다시 쓸 수 있도록 남긴다 —
ARTEL-614 를 이것으로 잡았고, 그때는 `content: null` 하나가 범인이었다.

    python scripts/probe_prompt_cache.py

`cached` 가 `input` 을 따라 오르면 정상이다. 한 값에 못 박히면 그 자리에서 접두가 깨진 것이고,
어느 변형이 깨뜨리는지는 아래 갈래를 늘려 가며 좁힌다.
"""

import os
import sys

import httpx

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("PROBE_MODEL", "openai/gpt-5.6-luna")
# 접두가 provider 의 최소치를 넘어야 아무것도 캐시되지 않는다. 실제 시스템 프롬프트와
# 비슷한 규모로 채운다.
SYSTEM = "You are a QA agent. " + ("Follow the scenario carefully. " * 900)
TURNS = int(os.environ.get("PROBE_TURNS", "7"))


def call(key: str, messages: list[dict]) -> tuple[int, int]:
    response = httpx.post(
        URL,
        timeout=120,
        headers={"Authorization": f"Bearer {key}"},
        json={"model": MODEL, "messages": messages, "max_tokens": 16, "usage": {"include": True}},
    )
    response.raise_for_status()
    usage = response.json().get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return usage.get("prompt_tokens", 0), details.get("cached_tokens", 0)


def run(key: str, label: str, *, content) -> None:
    """`content` 는 tool_calls 를 든 assistant 메시지에 실을 값. `...` 이면 키를 뺀다."""
    print(f"\n=== {label}")
    print(f"{'turn':>4} {'input':>8} {'cached':>8} {'cached%':>8}")

    history = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "Start the scenario."},
    ]
    for turn in range(1, TURNS + 1):
        # 매 턴 바뀌는 꼬리. 라이브 뷰가 이 자리에 붙는다.
        asked = history + [{"role": "user", "content": f"scene {turn} " + ("value " * 200)}]
        total, cached = call(key, asked)
        share = (100 * cached // total) if total else 0
        print(f"{turn:>4} {total:>8} {cached:>8} {share:>7}%")

        call_id = f"call_{turn:03d}"
        speaking = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "observe_scene", "arguments": '{"thought":"look"}'},
                }
            ],
        }
        if content is not ...:
            speaking["content"] = content
        history.append(speaking)
        history.append(
            {"role": "tool", "tool_call_id": call_id, "content": f"result {turn} " + ("line " * 150)}
        )


def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("OPENROUTER_API_KEY 가 없다.", file=sys.stderr)
        return 1

    # 두 갈래의 차이는 이 값 하나뿐이다.
    run(key, "content=None — langchain 이 지금 보내는 모양", content=None)
    run(key, 'content="" — 이 레포가 보내는 모양', content="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
