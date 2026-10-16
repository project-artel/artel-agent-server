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
    assert question._spoken(text, found).startswith("보스를 잡는다")
    assert found[("tc", 7)].detail == "HP 0"


def test_nested_refs_carry_names_and_spoken_prefixes_are_dropped() -> None:
    found: dict = {}
    question._remember(found, [{"ref": "[[qa:17]]", "label": "QA 17", "fields": {
        "failed_cases": [{"case": {"ref": "[[tc:359]]", "label": "Return 키로 전투 화면"}, "reason": "x"}],
    }}])

    assert found[("tc", 359)].label == "Return 키로 전투 화면"
    assert found[("qa", 17)].kind == "qa"
    assert question._checked("@TC [[tc:359]] 와 @QA [[qa:17]]", found) == "[[tc:359]] 와 [[qa:17]]"


def test_only_ids_a_tool_returned_can_be_read() -> None:
    # 사용자가 말한 "97번 TC" 의 97 은 DB id 가 아니다. 도구 결과에서 본 id 만 get · related 에 쓸 수 있다.
    seen: set[int] = set()
    question._collect_ids(seen, [{"id": 411, "ref": "[[tc:411]]", "fields": {
        "tries": [{"qa_try_id": 52, "attempt": 3}],
        "steps": [{"tc_no": 97, "case": {"ref": "[[tc:359]]", "label": "x"}}],
    }}])

    assert seen == {411, 52, 359}
    assert 97 not in seen


def test_only_mixed_looking_messages_are_split() -> None:
    from app.agents.scenario.requests import looks_multiple

    assert looks_multiple("21번 스텝을 빼 줘. 아 그리고 지금 TC 는 몇 개야?")
    assert looks_multiple("최근 QA 결과는? 이슈는 몇 개야?")
    assert not looks_multiple("보스전 시나리오 만들고 보상 확인까지 넣어 줘")


def test_requests_are_combined_into_one_numbered_answer() -> None:
    from app.agents.scenario.requests import combine
    from app.agents.scenario.schemas import AgentReply, Ref, ScenarioAgentResult, ScenarioChange

    first = ScenarioAgentResult(
        message="m1", scenarios=[],
        reply=AgentReply(result="1건을 수정했어요.", detail="뺐어요.", changes=[ScenarioChange(action="updated", title="맵 진입")]),
    )
    second = ScenarioAgentResult(
        message="m2", scenarios=[], reply=AgentReply(result="87건이에요.", detail="이 버전 기준."),
        refs=[Ref(kind="tr", id=105, label="데모")],
    )

    merged = combine([("마지막 스텝을 빼 줘", first), ("TC 는 몇 개야?", second)])

    assert merged.reply.result == "1. 1건을 수정했어요.\n2. 87건이에요."
    assert "**1. 마지막 스텝을 빼 줘**" in merged.reply.detail and "**2. TC 는 몇 개야?**" in merged.reply.detail
    assert [c.title for c in merged.reply.changes] == ["맵 진입"]
    assert [(r.kind, r.id) for r in merged.refs] == [("tr", 105)]
