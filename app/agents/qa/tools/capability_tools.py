"""플레이가 알아낸 것을 content map 의 `capability` 로 적는 도구 셋.

문구와 렌더 함수, 그리고 값의 vocabulary 는 `app/agents/qa/capability.py` 가 들고 있다.

셋 다 지금 서 있는 `scene` 을 모델에게 받지 않고 `_standing_scene` 이 채운다. 재현에 실을
조작 인자도 마찬가지로 `ToolContext.run` 이 남긴 기록에서 나온다 — 모델은 method 이름만
말한다(ARTEL-644).
"""

from langchain_core.tools import BaseTool, tool

from app.agents.qa.capability import (
    CAPABILITY_INTERACTIONS,
    CAPABILITY_ORIGINS,
    CAPABILITY_VERDICTS,
    INPUT_PHASES,
    LIST_SCENE_CAPABILITIES_DESCRIPTION,
    MAX_RATIONALE_LENGTH,
    MAX_SUMMARY_LENGTH,
    RECORD_CAPABILITY_VERDICT_DESCRIPTION,
    RECORD_NEW_CAPABILITY_DESCRIPTION,
    UNCONFIRMED_CAPABILITY_WRITE,
    render_capability_search,
    render_capability_write_result,
)
from app.agents.qa.tools.tool_context import ToolContext
from app.qa.channel import KnowledgeRequestFailed, QaCancelled, with_operator_messages
from app.qa.envelope import (
    CapabilityActionRecord,
    CapabilityDiscoveredPayload,
    CapabilityVerdictPayload,
    MessageType,
)


def build_capability_tools(ctx: ToolContext) -> list[BaseTool]:
    # 아래 tool 이 closure 로 잡는 것. 되묶는 이유는 `tool_context.py` 에 있다.
    channel, state = ctx.channel, ctx.state
    def _standing_scene() -> str:
        """지금 서 있는 `scene` 이름. 없으면 빈 문자열.

        capability 쓰기 셋이 전부 이 값을 쓴다. **모델에게 안 받는다** — 저쪽은 agent 가 서
        있지 않은 `scene` 의 행에 찍힌 verdict 를 거절하고, 그 규칙을 인자로 받으면 모델의
        성실함에 걸리지만 여기서 채우면 구조로 걸린다. `_write_screen_selector_rule` 과 같은
        판단이다.

        `pulse` 에서도 읽는다. `GAME_STATE` 없이 `pulse` 만 오는 게임에서는 `scene` 이 끝까지
        비어 있다.
        """
        return (channel.scene.scene or channel.scene.pulse.scene or "").strip()

    def _action_record(method: str) -> CapabilityActionRecord | None:
        """모델이 이름 댄 method 를, 이 런이 실제로 그것에 보낸 인자와 함께 싣는다.

        이 런이 보낸 적 없는 method 면 `None` 이다. 지어낸 재현을 `capability_observation`
        에 앉히느니 그 칸을 비우는 편이 낫다 — 그 표는 다음 사람이 재현을 읽는 자리이고,
        거기 적힌 것이 틀리면 아무도 그것을 의심하지 않는다.

        `attempts` 를 안 싣는다. 그 칸의 뜻은 "첫 메서드가 거절당해 바꿔 성공한 횟수" 인데,
        이 런의 dispatch 중 무엇이 이 capability 의 재시도였는지 가릴 방법이 없다. 저쪽
        기본값 1 이 근거 없는 수보다 낫다.
        """
        name = (method or "").strip()
        if not name or name not in state.dispatched_action_params:
            return None
        return CapabilityActionRecord(
            method=name, params=state.dispatched_action_params[name]
        )

    def _remember_write(payload) -> None:
        """받아들여진 쓰기가 남긴 id 를 이 런의 기억에 넣는다.

        `observation_id` 는 `inferred` 가 딛고 설 수 있는 유일한 값이고, `capability_id` 는
        키 없는 행 — agent 가 만든 행 — 을 나중에 지목하는 유일한 길이다.
        """
        if payload.observation_id:
            state.capability_observations[payload.observation_id] = payload.capability_id
        if payload.created and payload.capability_id:
            state.capability_rows_written[payload.capability_id] = payload.capability_id

    async def _write_capability(message_type: MessageType, payload) -> str:
        """쓰기 둘이 지나는 한 자리. 보내고, 답을 모델이 읽는 문장으로 옮긴다 (ARTEL-644).

        **어느 경우에도 런이 안 죽는다.** 저쪽은 거절을 값으로 돌려주고, 그래도 새는 예외는
        여기서 문장으로 바뀐다. 지도 쓰기 하나가 실패했다고 시나리오가 멈추면 이 tool 은
        런이 지는 위험이지 보태는 것이 아니다.

        `None` 을 실패로 옮기지 않는다. 이 프레임을 모르는 orchestration 은 라우터에서
        프레임을 떨어뜨리고 그 거절이 이 소켓으로 안 돌아오는데, 그때 "안 됐다" 고 하면
        모델이 같은 문장을 계속 다시 보낸다.
        """
        try:
            answer = await channel.write_capability(message_type, payload)
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - 지도를 적다 런이 끝나면 안 된다
            return f"The write could not be sent — {error}. Nothing was recorded."

        messages = channel.drain_operator_messages()
        if isinstance(answer, KnowledgeRequestFailed):
            return with_operator_messages(
                f"The content map refused it — {answer.reason}. Nothing was recorded. "
                "This says nothing about the game; carry on with the step.",
                messages,
            )
        if answer is None:
            return with_operator_messages(UNCONFIRMED_CAPABILITY_WRITE, messages)
        _remember_write(answer)
        return with_operator_messages(render_capability_write_result(answer), messages)

    def _rationale_problem(rationale: str) -> str | None:
        """`rationale` 이 계약을 못 지키면 무엇을 고치면 되는지.

        저쪽도 거절하고 DB 의 CHECK 가 한 번 더 막는다. 여기서 먼저 거절하는 것은 왕복
        하나를 아끼려는 것이자, 이 거절이 고칠 수 있는 것이기 때문이다 — 무엇을 봤는지
        쓰면 된다.
        """
        if not rationale:
            return (
                "`rationale` is required, so nothing was recorded. Write what you saw in "
                "one or two sentences, with the identifiers in it — a verdict nobody can "
                "retrace is one nobody can later decide was wrong — and call this again."
            )
        if len(rationale) > MAX_RATIONALE_LENGTH:
            return (
                f"`rationale` is longer than {MAX_RATIONALE_LENGTH} characters, so nothing "
                "was recorded. Shorten it to what you actually saw."
            )
        return None

    @tool(description=RECORD_CAPABILITY_VERDICT_DESCRIPTION)
    async def record_capability_verdict(
        step: int,
        thought: str,
        verdict: str,
        rationale: str,
        capability_key: str = "",
        capability_id: str = "",
        action_method: str = "",
    ) -> str:
        # What the agent reads is RECORD_CAPABILITY_VERDICT_DESCRIPTION, not this.
        #
        # 화면을 안 돌려준다. 이 호출은 게임을 안 건드리므로 화면을 실으면 에이전트가 이미
        # 들고 있는 것을 문맥에 한 번 더 사는 것이다 — 지식 tool 들과 같은 판단(ARTEL-180).
        scene = _standing_scene()
        if not scene:
            return (
                "The run has not been told which scene it is standing on yet, so nothing "
                "was recorded. Observe the scene first."
            )

        judgement = (verdict or "").strip().lower()
        if judgement not in CAPABILITY_VERDICTS:
            return (
                f"{verdict!r} is not one of {', '.join(CAPABILITY_VERDICTS)}, so nothing "
                "was recorded. Pick one and call this again."
            )

        why = (rationale or "").strip()
        problem = _rationale_problem(why)
        if problem is not None:
            return problem

        key = (capability_key or "").strip()
        row_id = (capability_id or "").strip()
        if bool(key) == bool(row_id):
            # 저쪽의 `needs exactly one of capability_key or capability_id` 를 먼저 본다.
            # 둘 다 보내는 것이 흔한 실수이고, 그 프레임은 아무것도 안 적고 돌아온다.
            return (
                "Name the capability with exactly one of `capability_key` or "
                "`capability_id`, not both and not neither, so nothing was recorded. The "
                "key is the value in square brackets on a capability line; use the id only "
                "for a row you created yourself in this run."
            )

        return await _write_capability(
            MessageType.CAPABILITY_VERDICT,
            CapabilityVerdictPayload(
                scene=scene,
                verdict=judgement,
                rationale=why,
                capability_key=key or None,
                capability_id=row_id or None,
                action=_action_record(action_method),
            ),
        )

    @tool(description=RECORD_NEW_CAPABILITY_DESCRIPTION)
    async def record_new_capability(
        step: int,
        thought: str,
        origin: str,
        summary: str,
        interaction: str,
        rationale: str,
        given_text: str = "",
        input_key: str = "",
        input_phase: str = "",
        control_path: str = "",
        control_label: str = "",
        verdict: str = "",
        based_on: list[str] | None = None,
        action_method: str = "",
    ) -> str:
        # What the agent reads is RECORD_NEW_CAPABILITY_DESCRIPTION, not this.
        scene = _standing_scene()
        if not scene:
            return (
                "The run has not been told which scene it is standing on yet, so nothing "
                "was recorded. Observe the scene first."
            )

        source = (origin or "").strip().lower()
        if source not in CAPABILITY_ORIGINS:
            return (
                f"{origin!r} is not one of {', '.join(CAPABILITY_ORIGINS)}, so nothing was "
                "recorded. `observed` means you pressed it and watched the result; "
                "anything short of that is `inferred`."
            )

        line = " ".join((summary or "").split())
        if not line:
            return "`summary` must say what this capability is, so nothing was recorded."
        if len(line) > MAX_SUMMARY_LENGTH:
            return (
                f"`summary` is longer than {MAX_SUMMARY_LENGTH} characters, so nothing was "
                "recorded. One capability is one test-case line; if it does not fit, it is "
                "more than one capability."
            )

        kind = (interaction or "").strip().lower()
        if kind not in CAPABILITY_INTERACTIONS:
            return (
                f"{interaction!r} is not one of {', '.join(CAPABILITY_INTERACTIONS)}, so "
                "nothing was recorded. Use `none` for something that happens rather than "
                "something you press."
            )

        key_name = (input_key or "").strip()
        if (kind == "press") != bool(key_name):
            # `ck_capability_press_needs_key` 를 여기서 먼저 본다. DB 가 막기는 하지만 그
            # 실패는 제약 이름이 실린 메시지라 무엇을 고쳐야 하는지 읽을 수 없다.
            return (
                "`interaction: press` requires `input_key`, and no other interaction may "
                "carry one, so nothing was recorded."
            )

        phase = (input_phase or "").strip().lower()
        if phase and phase not in INPUT_PHASES:
            return (
                f"{input_phase!r} is not one of {', '.join(INPUT_PHASES)}, so nothing was "
                "recorded."
            )

        why = (rationale or "").strip()
        problem = _rationale_problem(why)
        if problem is not None:
            return problem

        judgement = (verdict or "").strip().lower()
        if judgement and judgement not in CAPABILITY_VERDICTS:
            return (
                f"{verdict!r} is not one of {', '.join(CAPABILITY_VERDICTS)}, so nothing "
                "was recorded."
            )

        grounds = [str(item).strip() for item in (based_on or []) if str(item).strip()]

        if source == "observed" and not judgement:
            return (
                "`origin: observed` requires a `verdict` of works or fails, so nothing was "
                "recorded. `observed` means you pressed it and watched the result, so "
                "there is a result to report — and the verdict is what carries your "
                "rationale into a row. If you did not watch a result, write it as "
                "`inferred` with the observations it stands on."
            )
        if source == "inferred" and judgement:
            return (
                "`origin: inferred` cannot carry a verdict, so nothing was recorded. An "
                "inference is not a sighting. If you watched it happen, write it as "
                "`observed`."
            )
        if source == "inferred" and not grounds:
            # 이슈가 이름을 댄 경우다. 저쪽도 거절하지만, 여기서 먼저 거절하는 것이
            # 중요하다 — 거절 사유가 무엇을 고치면 되는지를 말할 수 있는 자리가 여기고,
            # 이 실수는 프레임을 하나 쓰기 전에 고칠 수 있는 것이다.
            return (
                "`origin: inferred` requires `based_on`, so nothing was recorded. An "
                "inference that names no observation cannot be retraced, which makes it "
                "indistinguishable from a guess once it is in the map. Put the observation "
                "ids this run was given back by an earlier capability write in `based_on`; "
                "each successful write prints one. If you have none, you have not observed "
                "enough to write this yet — go and watch it, then record it as `observed`."
            )

        unknown = [item for item in grounds if item not in state.capability_observations]
        if unknown:
            # 이 런이 받은 적 없는 id 는 저쪽이 거절한다(`based_on` 은 이 런의 observation
            # 이어야 한다). 여기서 먼저 거절하는 것은 `knowledge_seen` 과 같은 이유다 —
            # 이 런이 무엇을 받았는지 아는 곳이 여기 말고 없다.
            known = ", ".join(sorted(state.capability_observations)) or "none yet"
            return (
                f"`based_on` names {', '.join(unknown)}, which this run was never given "
                "back by a capability write, so nothing was recorded. This run's "
                f"observation ids are: {known}. Name one of those, or record what you "
                "watched as `observed` first and stand this inference on the observation "
                "that write returns."
            )

        return await _write_capability(
            MessageType.CAPABILITY_DISCOVERED,
            CapabilityDiscoveredPayload(
                scene=scene,
                origin=source,
                summary=line,
                interaction=kind,
                rationale=why,
                given_text=" ".join((given_text or "").split()) or None,
                input_key=key_name or None,
                input_phase=phase or None,
                control_path=(control_path or "").strip() or None,
                control_label=(control_label or "").strip() or None,
                verdict=judgement or None,
                action=_action_record(action_method) if judgement else None,
                based_on=grounds,
            ),
        )

    @tool(description=LIST_SCENE_CAPABILITIES_DESCRIPTION)
    async def list_scene_capabilities(
        step: int, thought: str, contains: str = "", offset: int = 0
    ) -> str:
        # What the agent reads is LIST_SCENE_CAPABILITIES_DESCRIPTION, not this.
        #
        # 아무 프레임도 안 나간다. 씬 문맥은 런 시작에 한 번 받아 메모리에 있고, 이 tool 은
        # 그 중 지금 씬의 것을 뒤진다 — 블록이 자리 때문에 못 그린 나머지를 당겨 오는 것이
        # 이 tool 의 전부다(ARTEL-680 이 목록을 469 행으로 넓혔다).
        scene = _standing_scene()
        if not scene:
            return (
                "The run has not been told which scene it is standing on yet, so there is "
                "nothing to look up. Observe the scene first."
            )
        context = channel.scene.scene_context
        entry = context.entry_for(scene) if context is not None else None
        if entry is None:
            return (
                f"The project has no content map entry for {scene}, so there is nothing "
                "here to look up. Anything you watch happen on this scene is new — "
                "`record_new_capability` is where it goes."
            )
        return render_capability_search(
            scene, entry.all_capabilities(), contains, offset
        )


    return [
        list_scene_capabilities,
        record_capability_verdict,
        record_new_capability,
    ]
