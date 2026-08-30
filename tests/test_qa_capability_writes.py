"""이 런이 배운 것을 content map 에 적는 tool 셋 (ARTEL-645).

실측이 이 tool 들을 부른다 — `artel_integration` 의 capability 472 행 중
`verification = 'confirmed'` 이 2 행이고, 418 행은 `interaction = 'none'` 이라 action 전후의
`pulse` 를 비교하는 기계 검증이 영영 아무 말도 못 한다.

넷을 못박는다. 넷은 서로 다른 방식으로 깨진다:

- **프레임이 계약대로 나가는가** — 타입 문자열과 필드 철자는 orchestration 의
  `contentmap/observe/CapabilityWriteFrames.kt` 가 정한 것이다. PR 135 는 없는 frame 을 이
  저장소가 지어냈다가 통째로 닫혔고, 이 검사가 그것을 되풀이하지 않게 하는 자리다.
- **거절이 프레임을 쓰기 전에 나는가** — 특히 `inferred` 인데 딛고 선 것을 안 밝힌 경우.
  저쪽도 거절하지만, 무엇을 고치면 되는지 말할 수 있는 자리는 이쪽이다.
- **거절이 런을 안 죽이는가** — 저쪽이 거절해도, 답이 안 와도, 보내다 터져도 tool 은
  문장을 돌려주고 런은 계속 간다.
- **agent 가 적을 대상을 보는가** — 씬 문맥 블록은 잘려 있고(실측 한 씬이 232 행),
  `list_scene_capabilities` 가 나머지에 닿는다. 이것이 없으면 tool 을 줘도 쓸 대상이 없다.
"""

import asyncio

import pytest

from app.agents.qa.capability import CAPABILITY_PAGE
from app.agents.qa.tools import QaRunState, build_tools
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType
from app.qa.scene_context import SceneContext


def make(timeout: float = 0.05):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(
        qa_try_id=7,
        send=send,
        action_timeout=timeout,
        write_timeout=timeout,
        screen_selector_timeout=timeout,
    )
    state = QaRunState(total_steps=1)
    tools = {tool.name: tool for tool in build_tools(channel, state)}
    return channel, state, tools, sent


def standing(channel: QaRunChannel, scene: str = "TurnBattleScene") -> None:
    """런이 `scene` 하나에 서 있게 한다. tool 이 그 이름을 인자가 아니라 여기서 읽는다."""
    channel.on_game_state({"type": "GAME_STATE", "payload": {"scene": scene, "interactables": []}})


def writes(sent: list[dict], message_type: MessageType) -> list[dict]:
    return [frame for frame in sent if frame["type"] == message_type.value]


def answer(
    channel: QaRunChannel,
    sent: list[dict],
    message_type: MessageType,
    payload: dict,
):
    """저쪽이 답하는 것처럼, 방금 나간 쓰기의 correlation 을 물고."""
    already = len(writes(sent, message_type))

    async def reply() -> None:
        for _ in range(200):
            if len(writes(sent, message_type)) > already:
                break
            await asyncio.sleep(0)
        channel.on_capability_write_result(
            {
                "type": MessageType.CAPABILITY_WRITE_RESULT.value,
                "correlationId": writes(sent, message_type)[-1]["messageId"],
                "payload": payload,
            }
        )

    return asyncio.create_task(reply())


def refuse(channel: QaRunChannel, sent: list[dict], message_type: MessageType, reason: str):
    """저쪽이 correlation 붙은 `ERROR` 로 거절하는 것처럼."""
    already = len(writes(sent, message_type))

    async def reply() -> None:
        for _ in range(200):
            if len(writes(sent, message_type)) > already:
                break
            await asyncio.sleep(0)
        channel.on_error(
            {
                "type": MessageType.ERROR.value,
                "correlationId": writes(sent, message_type)[-1]["messageId"],
                "payload": {"message": reason},
            }
        )

    return asyncio.create_task(reply())


ACCEPTED_VERDICT = {
    "type": "CAPABILITY_VERDICT",
    "capability_id": "3184",
    "capability_key": "TurnBattleScene|Combat.TurnManager|EndTurn|0|a1b2c3",
    "scene_id": "12",
    "verification": "confirmed",
    "observation_id": "77",
    "created": False,
}

ACCEPTED_DISCOVERY = {
    "type": "CAPABILITY_DISCOVERED",
    "capability_id": "3185",
    "capability_key": None,
    "scene_id": "12",
    "verification": "confirmed",
    "observation_id": "78",
    "created": True,
}


def scene_context(pressable: int = 2, not_a_step: int = 30) -> SceneContext:
    """orchestration 이 내는 모양 그대로의 씬 문맥 하나 (ARTEL-680 이 칸을 둘로 갈랐다)."""
    return SceneContext.model_validate(
        {
            "gameBuildId": "2",
            "contentMapId": "2",
            "capture": "player",
            "scenes": [
                {
                    "sceneName": "TurnBattleScene",
                    "knownToContentMap": True,
                    "capabilities": [
                        {
                            "capabilityId": str(100 + index),
                            "capabilityKey": f"TurnBattleScene|Combat|Press{index}",
                            "summary": f"Combat.EndTurnButton ends turn {index}.",
                            "interaction": "click",
                            "status": "runnable",
                            "verification": "unverified",
                            "controlLabel": f"EndTurn{index}",
                        }
                        for index in range(pressable)
                    ],
                    "notAStepCapabilities": [
                        {
                            "capabilityId": str(500 + index),
                            "capabilityKey": f"TurnBattleScene|Combat|Happens{index}",
                            "summary": (
                                "Combat.RewardPanel shows a reward line when the last "
                                f"Combat.Enemy {index} reaches hp 0."
                            ),
                            "interaction": "none",
                            "status": "not-a-step",
                            "verification": "unverified",
                        }
                        for index in range(not_a_step)
                    ],
                    "knowledge": [],
                }
            ],
        }
    )


# --- 프레임이 계약대로 나가는가 -----------------------------------------------


def test_verdict_프레임이_계약대로_나간다() -> None:
    """타입 문자열과 필드 철자가 `CapabilityWriteFrames.kt` 와 같아야 한다.

    `scene` 은 인자가 아니라 런이 서 있는 씬에서 채운다. 인자로 받으면 "서 있지 않은 씬의
    행에는 verdict 를 못 찍는다" 는 규칙이 모델의 성실함에 걸린다.
    """

    async def run() -> None:
        channel, _, tools, sent = make()
        standing(channel)
        task = answer(channel, sent, MessageType.CAPABILITY_VERDICT, ACCEPTED_VERDICT)

        result = await tools["record_capability_verdict"].ainvoke(
            {
                "step": 1,
                "thought": "EndTurn 을 눌러 봤다",
                "capability_key": "TurnBattleScene|Combat.TurnManager|EndTurn|0|a1b2c3",
                "verdict": "works",
                "rationale": "Clicked Canvas[7]/EndTurn[0]. The turn counter went 3 → 4.",
            }
        )
        await task

        frame = writes(sent, MessageType.CAPABILITY_VERDICT)[-1]
        assert frame["type"] == "CAPABILITY_VERDICT"
        payload = frame["payload"]
        assert payload["scene"] == "TurnBattleScene"
        assert payload["capability_key"] == "TurnBattleScene|Combat.TurnManager|EndTurn|0|a1b2c3"
        assert payload["capability_id"] is None
        assert payload["verdict"] == "works"
        assert payload["rationale"].startswith("Clicked Canvas[7]/EndTurn[0].")
        # 답이 말한 것을 모델도 읽는다. `observation_id` 는 나중에 `based_on` 에 실릴 값이다.
        assert "3184" in result
        assert "confirmed" in result
        assert "77" in result

    asyncio.run(run())


def test_discovery_프레임이_계약대로_나간다() -> None:
    """`observed` 는 verdict 를 달고 나간다. 그것이 `observed` 라는 말의 뜻이다."""

    async def run() -> None:
        channel, state, tools, sent = make()
        standing(channel)
        task = answer(channel, sent, MessageType.CAPABILITY_DISCOVERED, ACCEPTED_DISCOVERY)

        result = await tools["record_new_capability"].ainvoke(
            {
                "step": 1,
                "thought": "보상 패널이 열리는 것을 봤다",
                "origin": "observed",
                "summary": (
                    "Combat.RewardPanel shows a reward line when the last Combat.Enemy "
                    "reaches hp 0."
                ),
                "given_text": "At least one enemy is alive in the battle.",
                "interaction": "none",
                "rationale": "Killed the last enemy twice. Both times the panel opened.",
                "verdict": "works",
            }
        )
        await task

        frame = writes(sent, MessageType.CAPABILITY_DISCOVERED)[-1]
        assert frame["type"] == "CAPABILITY_DISCOVERED"
        payload = frame["payload"]
        assert payload["scene"] == "TurnBattleScene"
        assert payload["origin"] == "observed"
        assert payload["interaction"] == "none"
        assert payload["input_key"] is None
        assert payload["verdict"] == "works"
        assert payload["based_on"] == []
        assert payload["given_text"] == "At least one enemy is alive in the battle."
        assert "wrote a new row" in result
        # 키 없는 행이라, 나중에 verdict 를 찍는 길이 id 뿐이라는 것을 말해야 한다.
        assert "This row has no key" in result
        assert "`capability_id` 3185" in result
        # 이 런이 받은 observation 은 나중에 `inferred` 가 딛고 설 수 있는 유일한 값이다.
        assert state.capability_observations == {"78": "3185"}

    asyncio.run(run())


def test_모델이_이름_댄_method_에_이_런이_실제로_보낸_인자가_붙는다() -> None:
    """재현은 `capability_observation` 에서 읽힌다. 그 칸을 모델이 지어내면 안 된다.

    모델은 "무엇으로 눌렀나" 만 말하고, 인자는 이 런이 실제로 dispatch 한 값이 낸다.
    """

    async def run() -> None:
        channel, _, tools, sent = make()
        standing(channel)
        # 실제로 하나 눌러 본다. 게임이 답하지 않아도 무엇을 보냈는지는 남는다.
        await tools["click_button"].ainvoke(
            {"step": 1, "target_id": 4213, "thought": "EndTurn 을 누른다"}
        )
        task = answer(channel, sent, MessageType.CAPABILITY_VERDICT, ACCEPTED_VERDICT)

        await tools["record_capability_verdict"].ainvoke(
            {
                "step": 1,
                "thought": "됐다",
                "capability_key": "TurnBattleScene|Combat|EndTurn",
                "verdict": "works",
                "rationale": "The turn counter moved.",
                "action_method": "button_click",
            }
        )
        await task

        action = writes(sent, MessageType.CAPABILITY_VERDICT)[-1]["payload"]["action"]
        assert action["method"] == "button_click"
        assert action["params"] == [4213]
        # 근거 없는 수를 안 적는다. 저쪽 기본값 1 이 낫다.
        assert action["attempts"] is None

    asyncio.run(run())


def test_이_런이_보낸_적_없는_method_는_안_실린다() -> None:
    """지어낸 재현을 넣느니 그 칸을 비운다. 틀린 재현은 아무도 의심하지 않는다."""

    async def run() -> None:
        channel, _, tools, sent = make()
        standing(channel)
        task = answer(channel, sent, MessageType.CAPABILITY_VERDICT, ACCEPTED_VERDICT)

        await tools["record_capability_verdict"].ainvoke(
            {
                "step": 1,
                "thought": "됐다",
                "capability_key": "TurnBattleScene|Combat|EndTurn",
                "verdict": "works",
                "rationale": "The turn counter moved.",
                "action_method": "button_click",
            }
        )
        await task

        assert writes(sent, MessageType.CAPABILITY_VERDICT)[-1]["payload"]["action"] is None

    asyncio.run(run())


# --- 거절이 프레임을 쓰기 전에 나는가 -----------------------------------------


def test_inferred_인데_근거를_안_밝히면_프레임이_안_나간다() -> None:
    """이슈가 이름을 댄 경우다.

    저쪽도 거절한다. 여기서 먼저 거절하는 것은 왕복 하나를 아끼려는 것이자, 무엇을 고치면
    되는지를 말할 수 있는 자리가 여기이기 때문이다 — 딛고 설 observation 이 없으면 아직
    적을 것이 없다는 것이 답이다.
    """

    async def run() -> None:
        channel, _, tools, sent = make()
        standing(channel)

        result = await tools["record_new_capability"].ainvoke(
            {
                "step": 1,
                "thought": "그럴 것 같다",
                "origin": "inferred",
                "summary": "Combat.Enemy dies when hp reaches 0.",
                "interaction": "none",
                "rationale": "The hp bar looked empty when the sprite vanished.",
            }
        )

        assert writes(sent, MessageType.CAPABILITY_DISCOVERED) == []
        assert "`based_on`" in result
        assert "nothing was recorded" in result
        # 무엇을 하면 되는지가 함께 있어야 한다.
        assert "observed" in result

    asyncio.run(run())


def test_이_런이_받은_적_없는_observation_은_근거가_못_된다() -> None:
    """`based_on` 은 **이 런의** observation 이어야 하고, 저쪽은 그것을 거절한다.

    이 런이 무엇을 받았는지 아는 곳은 여기 말고 없다 — `knowledge_seen` 이 있는 이유와 같다.
    """

    async def run() -> None:
        channel, state, tools, sent = make()
        standing(channel)
        state.capability_observations["77"] = "3184"

        result = await tools["record_new_capability"].ainvoke(
            {
                "step": 1,
                "thought": "앞서 본 것에서 이어진다",
                "origin": "inferred",
                "summary": "Combat.Enemy dies when hp reaches 0.",
                "interaction": "none",
                "rationale": "The hp bar emptied and the sprite went.",
                "based_on": ["999"],
            }
        )

        assert writes(sent, MessageType.CAPABILITY_DISCOVERED) == []
        assert "999" in result
        # 이 런이 실제로 들고 있는 id 를 알려 준다. 그 목록이 없으면 다시 지어낸다.
        assert "77" in result

    asyncio.run(run())


def test_받은_observation_을_대면_inferred_가_나간다() -> None:
    """근거를 밝힌 추론은 정상 경로다. 거절 규칙이 추론 자체를 막는 것이 아니다."""

    async def run() -> None:
        channel, state, tools, sent = make()
        standing(channel)
        state.capability_observations["77"] = "3184"
        task = answer(
            channel,
            sent,
            MessageType.CAPABILITY_DISCOVERED,
            {**ACCEPTED_DISCOVERY, "verification": "unverified", "observation_id": None},
        )

        await tools["record_new_capability"].ainvoke(
            {
                "step": 1,
                "thought": "앞서 본 것에서 이어진다",
                "origin": "inferred",
                "summary": "Combat.Enemy dies when hp reaches 0.",
                "interaction": "none",
                "rationale": "The hp bar emptied and the sprite went.",
                "based_on": ["77"],
            }
        )
        await task

        payload = writes(sent, MessageType.CAPABILITY_DISCOVERED)[-1]["payload"]
        assert payload["origin"] == "inferred"
        assert payload["based_on"] == ["77"]
        assert payload["verdict"] is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ({"origin": "inferred", "verdict": "works", "based_on": ["77"]}, "not a sighting"),
        ({"origin": "observed"}, "requires a `verdict`"),
        ({"origin": "evidence", "verdict": "works"}, "observed, inferred"),
        ({"origin": "observed", "verdict": "works", "interaction": "press"}, "input_key"),
        (
            {"origin": "observed", "verdict": "works", "input_key": "Space"},
            "input_key",
        ),
        ({"origin": "observed", "verdict": "works", "interaction": "swipe"}, "click, type"),
        ({"origin": "observed", "verdict": "maybe"}, "works, fails"),
        (
            {"origin": "observed", "verdict": "works", "input_phase": "sideways"},
            "down, held, up",
        ),
        ({"origin": "observed", "verdict": "works", "rationale": "  "}, "`rationale`"),
        ({"origin": "observed", "verdict": "works", "summary": " "}, "`summary`"),
    ],
)
def test_계약을_못_지키는_discovery_는_프레임을_안_쓴다(arguments: dict, expected: str) -> None:
    """저쪽이 거절할 것을 여기서 먼저 거절한다.

    왕복 하나를 아끼는 것보다, 거절 사유가 "무엇을 고치면 되는지" 를 말할 수 있는 자리가
    여기라는 것이 크다. 저쪽 사유는 제약 이름이나 enum 목록이라 모델이 읽고 고치기 어렵다.
    """

    async def run() -> None:
        channel, state, tools, sent = make()
        standing(channel)
        state.capability_observations["77"] = "3184"

        result = await tools["record_new_capability"].ainvoke(
            {
                "step": 1,
                "thought": "적어 둔다",
                "summary": "Combat.RewardPanel opens when the last enemy dies.",
                "interaction": "none",
                "rationale": "Watched it twice.",
                **arguments,
            }
        )

        assert writes(sent, MessageType.CAPABILITY_DISCOVERED) == []
        assert expected in result

    asyncio.run(run())


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ({}, "exactly one"),
        ({"capability_key": "k", "capability_id": "3184"}, "exactly one"),
        ({"capability_key": "k", "verdict": "probably"}, "works, fails"),
        ({"capability_key": "k", "rationale": ""}, "`rationale`"),
        ({"capability_key": "k", "rationale": "x" * 2_001}, "2000 characters"),
    ],
)
def test_계약을_못_지키는_verdict_는_프레임을_안_쓴다(arguments: dict, expected: str) -> None:
    async def run() -> None:
        channel, _, tools, sent = make()
        standing(channel)

        result = await tools["record_capability_verdict"].ainvoke(
            {
                "step": 1,
                "thought": "적어 둔다",
                "verdict": "works",
                "rationale": "Watched it.",
                **arguments,
            }
        )

        assert writes(sent, MessageType.CAPABILITY_VERDICT) == []
        assert expected in result

    asyncio.run(run())


def test_서_있는_씬을_모르면_아무것도_안_적는다() -> None:
    """`scene` 이 필수이고 그것이 거절 규칙의 축이다. 이름을 모르면 프레임을 못 만든다."""

    async def run() -> None:
        _, _, tools, sent = make()

        result = await tools["record_capability_verdict"].ainvoke(
            {
                "step": 1,
                "thought": "적어 둔다",
                "capability_key": "k",
                "verdict": "works",
                "rationale": "Watched it.",
            }
        )

        assert sent == []
        assert "Observe the scene first" in result

    asyncio.run(run())


# --- 거절이 런을 안 죽이는가 --------------------------------------------------


def test_저쪽이_거절해도_런은_계속_간다() -> None:
    """거절은 문장으로 도착한다. 예외로 새면 그 프레임 하나가 런을 끝낸다."""

    async def run() -> None:
        channel, _, tools, sent = make()
        standing(channel)
        task = refuse(
            channel,
            sent,
            MessageType.CAPABILITY_VERDICT,
            "CAPABILITY_VERDICT references an unknown capability_key: k",
        )

        result = await tools["record_capability_verdict"].ainvoke(
            {
                "step": 1,
                "thought": "적어 둔다",
                "capability_key": "k",
                "verdict": "works",
                "rationale": "Watched it.",
            }
        )
        await task

        assert "refused" in result
        assert "unknown capability_key" in result
        assert "Nothing was recorded" in result
        # 다음 tool 이 여전히 돈다.
        assert channel.cancelled is False

    asyncio.run(run())


def test_답이_안_오면_실패로_옮겨_적지_않는다() -> None:
    """침묵은 "안 됐다" 가 아니다.

    이 프레임을 모르는 orchestration 은 라우터에서 통째로 떨어뜨리고 그 거절이 이 소켓으로
    안 돌아온다. 그때 "안 됐다" 고 하면 모델이 같은 문장을 계속 다시 보낸다.
    """

    async def run() -> None:
        channel, _, tools, _ = make(timeout=0.01)
        standing(channel)

        result = await tools["record_capability_verdict"].ainvoke(
            {
                "step": 1,
                "thought": "적어 둔다",
                "capability_key": "k",
                "verdict": "works",
                "rationale": "Watched it.",
            }
        )

        assert "cannot say" in result
        assert "Do not send the same thing again" in result

    asyncio.run(run())


def test_남의_답으로_tool_을_풀어_주지_않는다() -> None:
    """한 타입이 쓰기 둘에 답한다. `type` 을 안 보면 discovery 의 답이 verdict 를 푼다.

    그러면 verdict 를 보낸 tool 이 방금 만들어진 남의 행 id 를 자기 것으로 읽고, 그 id 가
    `based_on` 으로 다시 나간다.
    """

    async def run() -> None:
        channel, state, tools, sent = make(timeout=0.05)
        standing(channel)
        task = answer(channel, sent, MessageType.CAPABILITY_VERDICT, ACCEPTED_DISCOVERY)

        result = await tools["record_capability_verdict"].ainvoke(
            {
                "step": 1,
                "thought": "적어 둔다",
                "capability_key": "k",
                "verdict": "works",
                "rationale": "Watched it.",
            }
        )
        await task

        # 버려지고 타임아웃으로 간다 — 실제 상황이 "확인할 수 없다" 다.
        assert "cannot say" in result
        assert state.capability_observations == {}

    asyncio.run(run())


# --- agent 가 적을 대상을 보는가 ----------------------------------------------


def test_씬_문맥_블록이_누를_수_없는_것도_보여_준다() -> None:
    """ARTEL-680 이전에는 `not-a-step` 418 행이 agent 에게 한 줄도 안 왔다.

    적을 대상을 모르면 키를 지목할 수 없고, tool 을 줘도 쓸 곳이 없다.
    """
    block = scene_context(pressable=2, not_a_step=30).render("TurnBattleScene")

    assert block is not None
    assert "things the map says HAPPEN here" in block
    assert "30 known" in block
    # 자른 것을 자른 만큼 말한다. 조용히 자른 목록은 전부인 것으로 읽힌다.
    assert "showing 6 of 30 lines" in block
    assert "list_scene_capabilities reaches every one of them" in block


def test_누를_수_없는_것이_하나도_없으면_그_문단을_안_낸다() -> None:
    """이 칸을 모르는 orchestration 에서는 늘 빈다.

    그때 "여기서 일어나는 일이 없다" 는 문장은 사실이 아니라 배포 상태를 말한 것이다.
    """
    block = scene_context(pressable=2, not_a_step=0).render("TurnBattleScene")

    assert block is not None
    assert "things the map says HAPPEN here" not in block


def test_목록_tool_이_블록이_못_그린_나머지에_닿는다() -> None:
    """블록은 씬에 들어갈 때 한 번 그려지고 런 내내 문맥에 앉으므로 전부를 못 그린다.

    실측 `TurnBattleScene` 이 232 행이라, 이 tool 이 없으면 agent 가 볼 수 있는 것은 14 줄이다.
    """

    async def run() -> None:
        channel, _, tools, sent = make()
        standing(channel)
        channel.scene.scene_context = scene_context(pressable=2, not_a_step=30)

        result = await tools["list_scene_capabilities"].ainvoke(
            {"step": 1, "thought": "방금 본 것이 지도에 있나 본다", "contains": ""}
        )

        assert "32 mapped capability line(s)" in result
        assert f"showing 1-{CAPABILITY_PAGE}" in result
        assert "call again with offset=20" in result
        # 프레임은 하나도 안 나간다. 씬 문맥은 런 시작에 이미 받아 둔 것이다.
        assert sent == []

    asyncio.run(run())


def test_목록_tool_이_방금_본_것을_찾아_키를_준다() -> None:
    """실제 쓰임은 232 줄을 훑는 것이 아니라 방금 본 것에 해당하는 줄을 찾는 것이다."""

    async def run() -> None:
        channel, _, tools, _ = make()
        standing(channel)
        channel.scene.scene_context = scene_context(pressable=2, not_a_step=30)

        result = await tools["list_scene_capabilities"].ainvoke(
            {"step": 1, "thought": "보상 패널이 열렸다", "contains": "reward"}
        )

        assert "30 capability line(s) match" in result
        assert "[TurnBattleScene|Combat|Happens0]" in result
        # 누를 수 없다는 것이 그 줄에서 가장 중요한 정보다.
        assert "happens" in result

    asyncio.run(run())


def test_지도에_없으면_없다고_말하되_안_일어난다고_말하지_않는다() -> None:
    """빈 결과는 지도가 놓쳤다는 뜻이지 그런 일이 없다는 뜻이 아니다.

    그 구분이 `list_scene_capabilities` 와 `record_new_capability` 사이의 갈림길이다.
    """

    async def run() -> None:
        channel, _, tools, _ = make()
        standing(channel)
        channel.scene.scene_context = scene_context(pressable=2, not_a_step=30)

        result = await tools["list_scene_capabilities"].ainvoke(
            {"step": 1, "thought": "드래그가 지도에 있나", "contains": "drag"}
        )

        assert "never recorded it, not that it does not happen" in result
        assert "record_new_capability" in result

    asyncio.run(run())
