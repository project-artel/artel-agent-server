"""질문 갈래의 리소스 도구 POC(ARTEL-1086). 모델 없이 배선만 본다 — 채널이 답을 짝짓는지, 표식 검사가 도구 결과
안의 번호만 남기는지."""

from __future__ import annotations

import asyncio

from app.agents.scenario import question
from app.sessions.channel import ScenarioChannel


def test_resource_answers_are_matched_by_message_id() -> None:
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    async def scenario() -> list[dict | None]:
        channel = ScenarioChannel(send, search_timeout=1.0)
        first = asyncio.create_task(channel.query_resource({"op": "get", "kind": "case", "id": 1}))
        second = asyncio.create_task(channel.query_resource({"op": "get", "kind": "case", "id": 2}))
        await asyncio.sleep(0.01)
        # 늦게 보낸 것에 먼저 답해도 각자 제 답을 받는다.
        channel.deliver({"type": "resource_query_result", "correlationId": sent[1]["messageId"], "items": [{"id": 2}]})
        channel.deliver({"type": "resource_query_result", "correlationId": sent[0]["messageId"], "items": [{"id": 1}]})
        return [await first, await second]

    first, second = asyncio.run(scenario())
    assert sent[0]["type"] == "resource_query" and sent[0]["kind"] == "case"
    assert first == {"items": [{"id": 1}], "note": None}
    assert second == {"items": [{"id": 2}], "note": None}


def test_markers_keep_only_what_a_tool_returned() -> None:
    found: dict = {}
    question._remember(found, {"ref": "[[tc:7]]", "label": "보스를 잡는다", "fields": {"expected": "HP 0"}})
    question._remember(found, {"ref": None, "label": "1.0"})

    text = question._checked("[[tc:7]] 은 실패했고 [[tc:99]] 는 모른다", found)

    assert text == "[[tc:7]] 은 실패했고  는 모른다"
    assert question._spoken(text, found).startswith("@TC 보스를 잡는다")
    assert found[("tc", 7)].detail == "HP 0"
