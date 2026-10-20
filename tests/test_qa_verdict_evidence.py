"""판정 근거(ARTEL-1133): 근거를 판독 기록과 맞춰 보고, 판정과 항목이 어긋나면 돌려보낸다."""

import asyncio

from app.agents.qa.arch import PhaseCycleMode, arch_fingerprint, default_resolved_arch, structure_of
from app.agents.qa.tools import QaRunState, build_tools
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType
from app.qa.pulse import PulseMemory, PulseReading
from app.qa.verdict_evidence import EvidenceSource, VerdictCheck, cite, contradiction

_OFF = default_resolved_arch().model_copy(update={"phase_cycle": PhaseCycleMode.off})
_ON = _OFF.model_copy(update={"verdict_evidence": "on"})


def memory() -> PulseMemory:
    pulse = PulseMemory()
    pulse.apply(PulseReading.model_validate({
        "schema": 2, "reading": 1, "frame": 10, "scene": "Map_scene", "whole": True, "statics": [],
        "active": [{
            "scene": "Map_scene", "id": 1, "path": "MapScene", "selector": "MapScene[1]",
            "members": [{"on": "Map.MapMove", "member": "stage", "value": {"path": "Canvas/Stage", "label": "Stage : 0"}}],
        }, {
            "scene": "Map_scene", "id": 2, "path": "Enemy", "selector": "Enemy[3]",
            "members": [{"on": "Combat.Enemy", "member": "hp", "value": 10}],
        }],
        "deactive": [], "changed": [], "watching": 2, "unresolved": 0, "unwatchable": 0,
    }))
    return pulse


def check(met="yes", *sources: EvidenceSource) -> VerdictCheck:
    return VerdictCheck(expected="맵에 Stage 글자가 보인다", observed="Stage : 0", met=met, sources=list(sources))


def test_근거가_기록과_맞으면_확인됨이다() -> None:
    checks, status = cite(
        [check("yes",
               EvidenceSource(kind="pulse", object="MapScene", member="Map.MapMove::stage", value="Stage : 0"),
               EvidenceSource(kind="scene", object="Map_scene"),
               EvidenceSource(kind="capture", object="cap-1"))],
        memory(), {"cap-1"}, "Map_scene",
    )
    assert status == "verified"
    assert [s["citation"] for s in checks[0]["sources"]] == ["match", "match", "match"]


def test_다른_값_읽지_않은_값_보지_않은_캡처는_불확실이다() -> None:
    checks, status = cite(
        [check("yes",
               EvidenceSource(kind="pulse", object="Enemy[3]", member="hp", value="0"),
               EvidenceSource(kind="pulse", object="Lock", member="IsLocked", value="false"),
               EvidenceSource(kind="capture", object="cap-9"))],
        memory(), {"cap-1"}, "Map_scene",
    )
    assert status == "uncertain"
    assert [(s["citation"], s["recorded"]) for s in checks[0]["sources"]] == [
        ("mismatch", "10"), ("missing", None), ("missing", None),
    ]


def test_근거_없는_항목이_있으면_확인됨이_아니다() -> None:
    _, status = cite([check("unknown")], memory(), set(), "Map_scene")
    assert status == "uncertain"
    assert cite([], memory(), set(), None)[1] == "none"


def test_판정과_항목이_반대면_사정을_말한다() -> None:
    assert contradiction(True, [check("no")]) is not None
    assert contradiction(False, [check("yes"), check("yes")]) is not None
    assert contradiction(False, [check("no")]) is None
    assert contradiction(True, [check("yes"), check("unknown")]) is None


def make(arch, step_meta):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=0.05, write_timeout=0.05, reading_wait=0.05)
    state = QaRunState(total_steps=len(step_meta), step_meta=step_meta)
    tools = {tool.name: tool for tool in build_tools(channel, state, arch=arch)}
    return channel, state, tools, sent


def statuses(sent: list[dict]) -> list[dict]:
    return [f["payload"] for f in sent if f["type"] == MessageType.STATUS.value]


def test_축을_끈_런은_이유만_더해지고_도구_모양은_그대로다() -> None:
    async def run() -> None:
        _, _, tools, sent = make(_OFF, [(5, True)])
        assert "checks" not in tools["report_step"].args
        await tools["report_step"].ainvoke({"step": 1, "passed": True, "message": "보인다", "thought": "Stage 가 떴다"})
        payload = statuses(sent)[0]
        assert payload["reason"] == "Stage 가 떴다"
        assert payload["checks"] is None and payload["evidence"] is None

    asyncio.run(run())


def test_축을_끈_런의_지문은_축이_생기기_전과_같다() -> None:
    names, middleware, digest = structure_of(_OFF)
    assert digest == structure_of(_OFF.model_copy(update={"verdict_evidence": "off"}))[2]
    assert digest != structure_of(_ON)[2]
    # 끈 런의 digest 에는 이 칸이 아예 들어가지 않는다.
    assert arch_fingerprint(_OFF, [], middleware) == arch_fingerprint(_OFF, [], middleware)


def test_켠_런은_근거를_대조해_싣고_어긋난_판정은_돌려보낸다() -> None:
    async def run() -> None:
        channel, _, tools, sent = make(_ON, [(5, True)])
        channel.scene.pulse = memory()
        tool = tools["report_step"]
        assert "checks" in tool.args

        source = {"kind": "pulse", "object": "MapScene", "member": "stage", "value": "Stage : 0"}
        refused = await tool.ainvoke({
            "step": 1, "passed": True, "message": "보인다", "thought": "t",
            "checks": [{"expected": "Stage 글자", "observed": "없다", "met": "no", "sources": [source]}],
        })
        assert refused.startswith("Nothing was recorded") and not statuses(sent)

        empty = await tool.ainvoke({"step": 1, "passed": True, "message": "보인다", "thought": "t"})
        assert empty.startswith("Nothing was recorded") and not statuses(sent)

        await tool.ainvoke({
            "step": 1, "passed": True, "message": "보인다", "thought": "t",
            "checks": [{"expected": "Stage 글자", "observed": "Stage : 0", "met": "yes", "sources": [source]}],
        })
        payload = statuses(sent)[0]
        assert payload["evidence"] == "verified"
        assert payload["checks"][0]["sources"][0]["citation"] == "match"

    asyncio.run(run())


def test_검증이_아닌_스텝은_항목_없이_적힌다() -> None:
    async def run() -> None:
        _, _, tools, sent = make(_ON, [(None, False)])
        answer = await tools["report_step"].ainvoke({"step": 1, "passed": True, "message": "눌렀다", "thought": "t"})
        assert not answer.startswith("Nothing was recorded")
        assert statuses(sent)[0]["checks"] is None

    asyncio.run(run())
