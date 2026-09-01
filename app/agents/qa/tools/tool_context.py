"""도구 한 벌이 만들어질 때 넘겨받는 것, 그리고 결과를 내는 두 자리.

`app/agents/qa/context.py` 와는 다른 것이다. 그쪽은 모델에게 보내는 메시지 목록과 그
압축을 다루고, 여기는 도구 하나가 실행되는 동안 손에 쥐고 있는 것을 다룬다.

`answer` 와 `run` 이 여기 있는 이유는 여러 주제의 도구가 함께 쓰기 때문이다. `answer`
는 화면과 오퍼레이터의 말을 붙이는 유일한 자리이고, `run` 은 게임을 실제로 움직이는
도구가 전부 지나가는 자리다.
"""

from dataclasses import dataclass

from app.agents.qa.arch import ResolvedArch
from app.agents.qa.tools.state import QaRunState
from app.qa.channel import QaRunChannel, with_operator_messages
from app.qa.envelope import JsonRpcAction


@dataclass(frozen=True)
class ToolContext:
    """도구를 만들 때 넘기는 것 전부. 도구는 이것만 보고 자기 일을 한다."""

    channel: QaRunChannel
    state: QaRunState
    arch: ResolvedArch

    def answer(self, body: str, messages: list[str], screen: bool = True) -> str:
        """모든 도구 결과가 지나는 자리. 화면과 오퍼레이터의 말을 여기서 붙인다.

        **화면을 붙이는 자리가 하나여야 한다.** 도구마다 따로 붙이면 다음에 도구가 늘 때
        또 빠지고, 실제로 그렇게 빠져 있었다 — `report_step` 이 화면 없이 답하고 있었다.
        **스텝이 통과했는지를 정하는 그 턴에 화면이 없었다**(ARTEL-635).

        종전에는 꼬리가 도구와 무관하게 매 턴 화면을 줘서 이 구멍이 없었다. ARTEL-621 이
        그 꼬리를 없앤 것은 옳았지만 — 프롬프트 접두를 매 턴 깨뜨려 캐시를 못 쓰게 하고
        있었다 — 도구 결과가 화면을 싣는지는 보지 않았다.

        경계는 **마지막 행위**다. 관측이 그것을 옮기면 안 된다. 두 번 보는 것만으로 그
        사이의 변화를 잃는다.

        판독이 아직 없으면 조용하다. `render` 는 그때 안내 문구를 내는데, 그것을 화면인 척
        얹으면 에이전트가 빈 화면을 실제 화면으로 읽는다.

        **부르는 쪽은 화면을 직접 그리지 않는다.** 두 번 그리면 같은 것이 두 번 실린다 —
        판독이 유일한 출처인 지금 `render` 는 워터마크가 아니라 **마지막 행위**를 경계로
        삼으므로, 같은 결과 안에서 두 번째 호출이 첫 번째와 똑같은 것을 낸다.

        `screen=False` 는 지식창고를 다루는 도구들이다. ARTEL-180 이 그것을 정하면서 이유를
        적어 두었다 — 검색은 화면을 바꾸지 않으므로 화면을 돌려주면 문맥을 다시 쓰는 일이다.
        그 논거가 지금도 산다: 델타가 "마지막 행위 이후"라, 검색을 두 번 하면 두 번째가 첫
        번째와 같은 것을 반복한다. 화면이 필요하면 `observe_scene` 이 있다.
        """
        # 아래는 `build_tools` 안의 `_answer` 를 그대로 옮긴 것이다. 이름만 되묶는다.
        channel, state = self.channel, self.state
        if screen and (channel.scene.pulse.seen or channel.scene.frames > 0):
            view = channel.scene.render(state.watermark, since_action=state.last_action_frame)
            state.watermark = channel.scene.updates
            # 화면이 곧 답인 도구(`observe_scene`)는 앞에 얹을 몸통이 없다.
            body = f"{body}\n\n{view}" if body else view

        # 오퍼레이터의 말이 맨 뒤다. 지금부터 적용되는 지시라 화면보다 나중에 읽혀야 한다.
        return with_operator_messages(body, messages)

    async def run(self, actions: list[JsonRpcAction], summary: str, step: int) -> str:
        """Every acting tool goes through here: act, then look at what it did.

        Takes a list because a drag is only a drag when its actions ride in one
        batch — the SDK runs a batch strictly in order, so nothing can slip
        between the press and the release.
        """
        # 아래는 `build_tools` 안의 `_run` 을 그대로 옮긴 것이다. 이름만 되묶는다.
        channel, state = self.channel, self.state
        _answer = self.answer
        result, looked = await channel.act_and_look(actions, summary, step)
        messages = channel.drain_operator_messages()

        if result is None:
            return _answer(
                "The game reported no result. It may still have run — observe the "
                "scene to find out what actually happened.",
                messages,
            )

        methods = {action.id: action.method for action in actions}
        lines = []
        for item in result.results:
            # 에이전트가 부르지 않은 것은 거른다. 지금은 배치에 우리가 끼우는 것이
            # 없으므로(ARTEL-516 이 꼬리 `scan_scene` 을 뺐다) 걸릴 것이 없지만, 게임이
            # 배치에 없던 id 로 답하면 그것을 액션 결과인 척 옮기지 않는다.
            if item.id not in methods:
                continue
            outcome = "ok" if item.success else f"FAILED — {item.error or 'no reason given'}"
            # Named, because a drag comes back as four lines and an unlabelled
            # failure would not say which part of it went wrong.
            lines.append(f"  {methods[item.id]}: {outcome}")
        body = "\n".join(lines) or "  (the game returned no outcome for this action)"

        # 이 행위가 끝난 프레임. 그보다 뒤에 잡힌 판독만이 이 행위의 결과다(ARTEL-621).
        # 없으면 그 필드를 모르는 옛 SDK 이고, 렌더가 종전의 창으로 돌아간다.
        state.last_action_frame = result.frame

        # 화면 자체는 `_answer` 가 붙인다. 여기서 그리면 같은 것이 두 번 실린다. 아래
        # 갈래들이 하는 일은 그 화면을 **어떻게 읽을지**를 말하는 것뿐이다.
        if not looked and channel.scene.pulse.seen:
            # 판독이 흐르는데 새로 온 것이 없다 = 화면이 움직이지 않았다. SDK 는 움직인
            # 것이 없으면 판독을 아예 내지 않으므로 침묵이 곧 "그대로"다(ARTEL-516).
            #
            # 그래도 화면은 그린다(`_answer` 가). 여기서 감추면 액션이 아무것도 바꾸지
            # 않았다는 것을 판정하려는 스텝이 볼 것을 잃는다 — 그것이야말로 보여 줘야 하는
            # 결과다. 이 줄은 그 화면을 어떻게 읽을지를 말한다.
            body = f"{body}\n\nNothing on the screen moved."
        elif not looked:
            # 판독을 한 번도 못 봤다 = 그릴 화면이 아예 없다. 화면을 `_answer` 에 맡기면
            # GAME_STATE 프레임이 남아 있는 빌드에서 "화면을 안 준다"고 말한 바로 밑에 옛
            # 화면을 붙이게 된다.
            return _answer(
                f"{body}\n\nThe game is not reporting the screen at all. "
                "Observe again, or judge the step from the outcome above.",
                messages,
                screen=False,
            )
        return _answer(body, messages)
