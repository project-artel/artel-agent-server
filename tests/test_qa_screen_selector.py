"""지금 어느 `screen` 이라고 판정됐는지 보여 주고, 틀렸으면 고치게 한다 (ARTEL-657).

세 가지를 못박는다. 셋은 서로 다른 방식으로 깨진다:

- **판정이 agent 에게 보이는가** — 화면 id 와 그 화면을 가른 selector 가 씬 뷰에 실려야
  하고, `GAME_STATE` 가 한 장도 안 오는 실측 런에서도 실려야 한다. 그리고 **다른 `scene`
  의 판정은 안 실려야 한다** — 판정은 드물게 오므로 그 검사가 없으면 옆 `scene` 의 화면
  번호가 지금 화면으로 앉는다.
- **tool 이 계약대로 나가는가** — `SCREEN_SELECTOR_RULE` 의 payload 모양은
  orchestration 의 `ScreenSelectorFrames.kt` 가 정한 것이고, `scene` 은 인자가 아니라 런의
  현재 `scene` 에서 채워져야 한다.
- **거절이 모델에게 도착하는가** — 이 `scene` 에서 본 적 없는 selector, 다른 `scene` 의
  selector, 사유 없는 호출. 셋 다 조용히 성공으로 읽히면 안 된다.
"""

import asyncio
import json

from app.agents.qa.screen import MAX_PATTERN_LENGTH, SCREEN_SELECTOR_MATCHES
from app.agents.qa.tools import QaRunState, build_tools
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType, ScreenSelectorProposalPayload
from app.qa.screen import MAX_DISCRIMINATOR_SHOWN, ScreenMap
from app.qa.scene import SceneMemory
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore


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
    tools = {tool.name: tool for tool in build_tools(channel, QaRunState(total_steps=1))}
    return channel, tools, sent


def proposal(
    scene: str = "TurnBattleScene",
    screen_id: str = "227",
    previous_screen_id: str | None = "226",
    discriminator: list[dict] | None = None,
) -> dict:
    """`SCREEN_SELECTOR_PROPOSAL` 한 장, orchestration 이 보내는 모양 그대로."""
    entries = (
        discriminator
        if discriminator is not None
        else [
            {"selector": "CombineSystem[7]/CombineButton[0]", "active": True},
            {"selector": "CombineSystem[7]/CombineZone[1]/Button[2]", "active": False},
        ]
    )
    previous = (
        None
        if previous_screen_id is None
        else {"screen_id": previous_screen_id, "name": None, "discriminator": []}
    )
    return {
        "type": "SCREEN_SELECTOR_PROPOSAL",
        "messageId": "prop-1",
        "payload": {
            "reason": "unknown-selector",
            "scene": {"scene_id": "12", "name": scene},
            "previous_screen": previous,
            "current_screen": {
                "screen_id": screen_id,
                "name": None,
                "discriminator": entries,
                "capture_url": "https://example.invalid/shot.jpg",
                "capture_expires_at": "2026-08-28T09:00:00Z",
            },
            "changes": [
                {"selector": "CombineSystem[7]/CombineZone[1]/Zone1[0]", "was": False, "now": True}
            ],
            "candidates": [
                {
                    "selector": "CombineSystem[7]/CombineZone[1]/Zone1[0]",
                    "path": "CombineSystem/CombineZone/Zone1",
                    "active": True,
                    "instances_in_reading": 1,
                    "readings_seen_in_scene": 431,
                    "distinct_values_observed": 1,
                    "in_whitelist": False,
                }
            ],
        },
    }


def game_state(scene: str = "TurnBattleScene") -> dict:
    return {"type": "GAME_STATE", "payload": {"scene": scene, "interactables": []}}


def pulse(scene: str = "TurnBattleScene") -> dict:
    return {
        "type": "PULSE",
        "payload": {
            "schema": 2,
            "reading": 1,
            "frame": 100,
            "scene": scene,
            "whole": True,
            "active": [
                {
                    "scene": scene,
                    "id": 26168,
                    "path": "CombineSystem/CombineButton",
                    "selector": "CombineSystem[7]/CombineButton[0]",
                    "members": [],
                }
            ],
        },
    }


def rules(sent: list[dict]) -> list[dict]:
    return [
        frame
        for frame in sent
        if frame["type"] == MessageType.SCREEN_SELECTOR_RULE.value
    ]


def answer(channel: QaRunChannel, sent: list[dict], payload: dict):
    """저쪽이 답하는 것처럼, 나간 `SCREEN_SELECTOR_RULE` 의 correlation 을 물고."""
    already = len(rules(sent))

    async def reply() -> None:
        for _ in range(50):
            if len(rules(sent)) > already:
                break
            await asyncio.sleep(0)
        channel.on_screen_selector_result(
            {
                "type": "SCREEN_SELECTOR_RESULT",
                "correlationId": rules(sent)[-1]["messageId"],
                "payload": payload,
            }
        )

    return asyncio.create_task(reply())


# --- 판정이 보이는가 ----------------------------------------------------------


def test_판정이_화면_id_와_가른_selector_를_함께_보여_준다() -> None:
    """id 만으로는 고칠 수 없다. 무엇으로 가르고 있는지가 고칠 대상이다."""
    channel, _, _ = make()
    channel.on_game_state(game_state())
    channel.on_screen_selector_proposal(proposal())

    view = channel.scene.render(0)

    assert "screen 227" in view
    assert "TurnBattleScene" in view
    assert "CombineSystem[7]/CombineButton[0] on" in view
    assert "CombineSystem[7]/CombineZone[1]/Button[2] off" in view


def test_화면이_바뀐_것이_보인다() -> None:
    """직전 화면이 프레임에 실려 온다. 같은 판정을 두 번 그려도 같은 글이 나온다."""
    channel, _, _ = make()
    channel.on_game_state(game_state())
    channel.on_screen_selector_proposal(proposal())

    first = channel.scene.render(0)
    second = channel.scene.render(0)

    assert "reached from screen 226" in first
    # 그린 자리를 장부로 들면 압축 원장과 도구 결과가 서로의 소식을 먹는다.
    assert "reached from screen 226" in second


def test_판독만_오는_런에서도_판정이_보인다() -> None:
    """실측 런은 `GAME_STATE` 가 0장이고 `PULSE` 만 14489장이다.

    그 갈래를 빠뜨리면 블록이 한 번도 안 뜨면서 단위 테스트는 통과한다.
    """
    channel, _, _ = make()
    channel.on_pulse(pulse())
    channel.on_screen_selector_proposal(proposal())

    view = channel.scene.render(0)

    assert channel.scene.scene is None
    assert "screen 227" in view


def test_다른_scene_의_판정은_지금_화면인_척_하지_않는다() -> None:
    """판정은 관측마다 오지 않으므로 값이 남는다. 남은 값을 그리면 그것이 거짓말이 된다."""
    channel, _, _ = make()
    channel.on_screen_selector_proposal(proposal(scene="LobbyScene"))
    channel.on_game_state(game_state(scene="TurnBattleScene"))

    view = channel.scene.render(0)

    assert "screen 227" not in view
    assert "LobbyScene" not in view


def test_scene_을_나갔다_돌아오면_판정이_살아_있다() -> None:
    """씬이 바뀌었다고 판정을 비우면 영영 안 돌아온다.

    저쪽은 같은 `(scene, selector)` 를 두 번 물어보지 않는다.
    """
    channel, _, _ = make()
    channel.on_game_state(game_state(scene="TurnBattleScene"))
    channel.on_screen_selector_proposal(proposal())
    channel.on_game_state(game_state(scene="LobbyScene"))
    assert "screen 227" not in channel.scene.render(0)

    channel.on_game_state(game_state(scene="TurnBattleScene"))

    assert "screen 227" in channel.scene.render(0)


def test_빈_discriminator_는_뭉쳐_있다는_사실로_읽힌다() -> None:
    """`discriminator` 가 비었다는 것은 이 `scene` 의 관측이 전부 한 행에 앉았다는 뜻이다.

    "없음" 으로 흘리면 이 tool 들이 존재하는 이유인 상태가 안 보인다.
    """
    screen_map = ScreenMap()
    screen_map.apply(
        ScreenSelectorProposalPayload.model_validate(proposal(discriminator=[])["payload"])
    )

    block = screen_map.render("TurnBattleScene")

    assert "told apart by: nothing" in block
    assert "one screen row" in block


def test_긴_discriminator_는_잘렸다고_말한다() -> None:
    """조용히 줄인 목록은 그것이 전부인 것으로 읽힌다."""
    memory = SceneMemory()
    entries = [
        {"selector": f"Canvas[{index}]/Button[0]", "active": True}
        for index in range(MAX_DISCRIMINATOR_SHOWN + 3)
    ]
    memory.screen_map.apply(
        ScreenSelectorProposalPayload.model_validate(
            proposal(discriminator=entries)["payload"]
        )
    )

    block = memory.screen_map.render("TurnBattleScene")

    assert "+3 more not listed" in block


# --- 인입 배선 ----------------------------------------------------------------


def test_제안과_결과_프레임을_받아들인다() -> None:
    """모르는 타입은 "unsupported inbound frame" 으로 돌아간다.

    제안은 QA agent 에게 화면 판정을 싣고 오는 유일한 통로라, 거절하면 화면이 영영 안 보인다.
    """

    async def run() -> None:
        service = QaExecutionService(InMemoryQaSessionStore())
        channel, _, _ = make()
        service._channels["s"] = channel

        assert service.deliver("s", proposal()) is True
        assert service.deliver(
            "s",
            {
                "type": "SCREEN_SELECTOR_RESULT",
                "correlationId": "nobody",
                "payload": {"type": "SCREEN_SELECTOR_RULE"},
            },
        ) is True
        assert channel.scene.screen_map.verdict is not None

    asyncio.run(run())


def test_제안에_답하지_않는다() -> None:
    """제안에 답하는 것은 따로 띄우는 판정 agent 다 (ARTEL-656)."""

    async def run() -> None:
        service = QaExecutionService(InMemoryQaSessionStore())
        channel, _, sent = make()
        service._channels["s"] = channel

        service.deliver("s", proposal())

        assert sent == []

    asyncio.run(run())


def test_남의_답이_이_tool_을_풀어_주지_않는다() -> None:
    """한 타입이 `RULE` 과 `VERDICT` 양쪽에 답한다. `type` 을 안 보면 남의 답을 읽는다."""

    async def run() -> None:
        channel, tools, sent = make(timeout=0.2)

        async def stray() -> None:
            for _ in range(50):
                if rules(sent):
                    break
                await asyncio.sleep(0)
            channel.on_screen_selector_result(
                {
                    "correlationId": rules(sent)[-1]["messageId"],
                    "payload": {
                        "type": "SCREEN_SELECTOR_VERDICT",
                        "rejected": [{"reason": "someone else's answer"}],
                    },
                }
            )

        channel.on_game_state(game_state())
        task = asyncio.create_task(stray())
        result = await tools["include_screen_selector"].ainvoke(
            {
                "step": 1,
                "thought": "화면이 다른데 지도가 같다고 한다",
                "match": "path",
                "pattern": "CombineSystem/CombineZone/Zone1",
                "reason": "조합 패널이 열릴 때만 보인다",
            }
        )
        await task

        assert "someone else's answer" not in result
        assert "No answer came back" in result

    asyncio.run(run())


# --- tool 이 내보내는 것 ------------------------------------------------------


def test_include_가_계약대로_된_프레임을_낸다() -> None:
    """payload 모양은 orchestration 의 `ScreenSelectorFrames.kt` 가 정한 것이다."""

    async def run() -> None:
        channel, tools, sent = make()
        channel.on_game_state(game_state())
        task = answer(
            channel,
            sent,
            {
                "type": "SCREEN_SELECTOR_RULE",
                "scene_id": "12",
                "accepted": [
                    {
                        "match": "path",
                        "pattern": "CombineSystem/CombineZone/Zone1",
                        "screen_defining": True,
                    }
                ],
                "rejected": [],
                "folded_screens": 0,
            },
        )
        result = await tools["include_screen_selector"].ainvoke(
            {
                "step": 2,
                "thought": "조합 패널이 열렸는데 지도가 같은 화면이라고 한다",
                "match": "path",
                "pattern": "CombineSystem/CombineZone/Zone1",
                "reason": "조합 패널이 열려 있는 동안에만 보이는 칸이다",
            }
        )
        await task

        frame = rules(sent)[-1]
        assert frame["payload"] == {
            "scene": "TurnBattleScene",
            "entries": [
                {
                    "match": "path",
                    "pattern": "CombineSystem/CombineZone/Zone1",
                    "screen_defining": True,
                    "reason": "조합 패널이 열려 있는 동안에만 보이는 칸이다",
                }
            ],
        }
        # 넣는 답이 과거를 안 가른다는 사실은 결과에도 있어야 한다. 설명만 있으면 그 턴에
        # 무엇이 일어났는지를 모델이 추측한다.
        assert "No screens were folded" in result
        assert "next observation" in result

    asyncio.run(run())


def test_scene_은_인자가_아니라_런이_서_있는_곳에서_온다() -> None:
    """목록은 `scene` 단위다. 인자로 받으면 그 규칙이 모델의 성실함에 걸린다."""

    async def run() -> None:
        channel, tools, sent = make()
        channel.on_pulse(pulse(scene="LobbyScene"))
        task = answer(channel, sent, {"type": "SCREEN_SELECTOR_RULE", "accepted": []})
        await tools["exclude_screen_selector"].ainvoke(
            {
                "step": 1,
                "thought": "화면이 안 바뀌었는데 지도가 새 화면이라고 한다",
                "match": "selector",
                "pattern": "CombineSystem[7]/CombineButton[0]",
                "reason": "타이머가 도는 동안 화면은 그대로였다",
            }
        )
        await task

        assert rules(sent)[-1]["payload"]["scene"] == "LobbyScene"
        assert "scene" not in tools["exclude_screen_selector"].args

    asyncio.run(run())


def test_exclude_는_접힌_화면_수를_전한다() -> None:
    """빼는 방향만 기존 행을 접는다. 몇이 접혔는가가 그 답의 결과다."""

    async def run() -> None:
        channel, tools, sent = make()
        channel.on_game_state(game_state())
        task = answer(
            channel,
            sent,
            {
                "type": "SCREEN_SELECTOR_RULE",
                "scene_id": "12",
                "accepted": [
                    {
                        "match": "path",
                        "pattern": "CombineSystem/CombineZone/Zone1",
                        "screen_defining": False,
                    }
                ],
                "folded_screens": 2,
            },
        )
        result = await tools["exclude_screen_selector"].ainvoke(
            {
                "step": 1,
                "thought": "같은 화면인데 지도가 계속 새 번호를 준다",
                "match": "path",
                "pattern": "CombineSystem/CombineZone/Zone1",
                "reason": "씬이 떠 있는 내내 켜져 있고 두 화면을 가른 적이 없다",
            }
        )
        await task

        assert rules(sent)[-1]["payload"]["entries"][0]["screen_defining"] is False
        assert "2 screen(s)" in result

    asyncio.run(run())


def test_답이_안_오면_실패로_옮겨_적지_않는다() -> None:
    """이 프레임을 모르는 orchestration 은 거절을 이 소켓으로 안 돌려준다.

    그때 "안 됐다" 고 하면 모델이 같은 항목을 계속 다시 보낸다.
    """

    async def run() -> None:
        channel, tools, sent = make()
        channel.on_game_state(game_state())

        result = await tools["include_screen_selector"].ainvoke(
            {
                "step": 1,
                "thought": "본다",
                "match": "selector",
                "pattern": "CombineSystem[7]/CombineButton[0]",
                "reason": "패널이 열릴 때만 나타난다",
            }
        )

        assert rules(sent)
        assert "Do not send the same entry again" in result

    asyncio.run(run())


# --- 거절 경로 ----------------------------------------------------------------


def test_이_scene_에서_본_적_없는_selector_는_거절되고_사유가_온다() -> None:
    """저쪽이 관측된 것과 맞대 보고 거절한다. 그 사유가 모델에게 그대로 가야 한다."""

    async def run() -> None:
        channel, tools, sent = make()
        channel.on_game_state(game_state())
        task = answer(
            channel,
            sent,
            {
                "type": "SCREEN_SELECTOR_RULE",
                "scene_id": "12",
                "accepted": [],
                "rejected": [
                    {
                        "match": "selector",
                        "pattern": "Canvas[2]/nope[9]",
                        "reason": "pattern matches nothing observed in this scene: Canvas[2]/nope[9]",
                    }
                ],
                "folded_screens": 0,
            },
        )
        result = await tools["include_screen_selector"].ainvoke(
            {
                "step": 1,
                "thought": "이것이 화면을 가르는 것 같다",
                "match": "selector",
                "pattern": "Canvas[2]/nope[9]",
                "reason": "패널이 열릴 때만 보였다",
            }
        )
        await task

        assert "The content map stored nothing." in result
        assert "matches nothing observed in this scene" in result
        assert "Canvas[2]/nope[9]" in result

    asyncio.run(run())


def test_다른_scene_의_selector_도_같은_사유로_거절된다() -> None:
    """관측 목록이 `scene` 단위라, 옆 `scene` 의 selector 는 "본 적 없다" 로 떨어진다.

    프레임은 지금 서 있는 `scene` 이름을 싣고 나가므로 `scene` 을 건너뛸 방법 자체가 없다.
    """

    async def run() -> None:
        channel, tools, sent = make()
        channel.on_game_state(game_state(scene="TurnBattleScene"))
        task = answer(
            channel,
            sent,
            {
                "type": "SCREEN_SELECTOR_RULE",
                "scene_id": "12",
                "accepted": [],
                "rejected": [
                    {
                        "match": "path",
                        "pattern": "LobbyCanvas/StartButton",
                        "reason": "pattern matches nothing observed in this scene: LobbyCanvas/StartButton",
                    }
                ],
            },
        )
        result = await tools["include_screen_selector"].ainvoke(
            {
                "step": 1,
                "thought": "로비의 그 버튼이 화면을 가른다",
                "match": "path",
                "pattern": "LobbyCanvas/StartButton",
                "reason": "로비에서 봤다",
            }
        )
        await task

        assert rules(sent)[-1]["payload"]["scene"] == "TurnBattleScene"
        assert "matches nothing observed in this scene" in result

    asyncio.run(run())


def test_사유_없는_호출은_프레임을_쓰지_않고_거절된다() -> None:
    """저쪽도 거절한다. 여기서 먼저 거절하는 것은 고칠 수 있는 거절이기 때문이다."""

    async def run() -> None:
        channel, tools, sent = make()
        channel.on_game_state(game_state())

        result = await tools["include_screen_selector"].ainvoke(
            {
                "step": 1,
                "thought": "본다",
                "match": "selector",
                "pattern": "CombineSystem[7]/CombineButton[0]",
                "reason": "   ",
            }
        )

        assert rules(sent) == []
        assert "`reason` is required" in result

    asyncio.run(run())


def test_셋_중_하나가_아닌_match_는_거절된다() -> None:
    """넷째가 없다. 정규식을 거기 실을 자리를 만들지 않기 위해서다."""

    async def run() -> None:
        channel, tools, sent = make()
        channel.on_game_state(game_state())

        result = await tools["exclude_screen_selector"].ainvoke(
            {
                "step": 1,
                "thought": "본다",
                "match": "regex",
                "pattern": ".*",
                "reason": "전부 무시하고 싶다",
            }
        )

        assert rules(sent) == []
        for kind in SCREEN_SELECTOR_MATCHES:
            assert kind in result

    asyncio.run(run())


def test_상한을_넘는_pattern_은_왕복하지_않는다() -> None:
    async def run() -> None:
        channel, tools, sent = make()
        channel.on_game_state(game_state())

        result = await tools["include_screen_selector"].ainvoke(
            {
                "step": 1,
                "thought": "본다",
                "match": "selector",
                "pattern": "A" * (MAX_PATTERN_LENGTH + 1),
                "reason": "길다",
            }
        )

        assert rules(sent) == []
        assert str(MAX_PATTERN_LENGTH) in result

    asyncio.run(run())


def test_서_있는_scene_을_모르면_고치지_않는다() -> None:
    """`scene` 없이 나간 프레임은 저쪽에서 통째로 거절된다."""

    async def run() -> None:
        _, tools, sent = make()

        result = await tools["include_screen_selector"].ainvoke(
            {
                "step": 1,
                "thought": "본다",
                "match": "selector",
                "pattern": "CombineSystem[7]/CombineButton[0]",
                "reason": "패널이 열릴 때만 보인다",
            }
        )

        assert rules(sent) == []
        assert "Observe the scene first" in result

    asyncio.run(run())


# --- 설명이 지고 있는 것 ------------------------------------------------------


def test_설명이_언제_부르는지와_과거가_안_갈린다는_것을_말한다() -> None:
    """tool 설명이 사용 정책의 단일 출처다 (ARTEL-192).

    이 둘이 빠지면 모델은 이 tool 을 짐작으로 부르거나, 부르고 나서 과거 화면이 갈렸다고
    믿는다. 프롬프트에 적어 메우는 길은 이 저장소가 닫아 두었다.
    """
    _, tools, _ = make()
    include = tools["include_screen_selector"].description
    exclude = tools["exclude_screen_selector"].description

    # 언제 부르는가 — 눈에 보이는 차이와 지도의 불일치.
    assert "content map:" in include
    assert "same screen id" in include
    assert "Do not call it on a hunch" in include
    assert "difference you can SEE" in include

    # 넣어도 과거는 안 갈린다.
    assert "does not un-merge the screens that already merged" in include
    assert "next observation" in include.lower() or "NEXT observation" in include

    # 정규식이 아니다, 그리고 `scene` 을 넘겨 고치지 못한다 — 둘 다 양쪽 설명에 있다.
    for text in (include, exclude):
        assert "never a regular expression" in text
        assert "cannot reach another scene's list" in text
        assert "`reason` is what you saw" in text
        assert "It is required." in text


def test_설명은_json_직렬화_가능한_한_덩어리다() -> None:
    """모델에게 가는 것은 이 문자열이다. 비면 아무도 못 쓰는 tool 이 하나 늘 뿐이다."""
    _, tools, _ = make()

    for name in ("include_screen_selector", "exclude_screen_selector"):
        assert json.dumps(tools[name].description)
        assert len(tools[name].description) > 500
