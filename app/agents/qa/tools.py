"""The tools the QA agent drives the game with.

Each one is a round trip the agent chooses to make, which is what separates this
from the old design: the run advances because the agent asked, not because the
game happened to send something.
"""

from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.qa.arch import ResolvedArch, default_resolved_arch
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
from app.agents.qa.knowledge import (
    EXPAND_KNOWLEDGE_DESCRIPTION,
    FORGET_KNOWLEDGE_DESCRIPTION,
    KNOWLEDGE_RELATIONS,
    KNOWLEDGE_TAGS,
    LINK_KNOWLEDGE_DESCRIPTION,
    MAX_EXPAND_DEPTH,
    RECORD_KNOWLEDGE_DESCRIPTION,
    RESULT_LIMIT,
    SEARCH_KNOWLEDGE_DESCRIPTION,
    SIMILAR_LABEL,
    UNCONFIRMED_WRITE,
    UNLINK_KNOWLEDGE_DESCRIPTION,
    UPDATE_KNOWLEDGE_DESCRIPTION,
    render_entry_label,
    render_expansion,
    render_missing_knowledge_warning,
    render_results,
)
from app.agents.qa.screen import (
    EXCLUDE_SCREEN_SELECTOR_DESCRIPTION,
    INCLUDE_SCREEN_SELECTOR_DESCRIPTION,
    MAX_PATTERN_LENGTH,
    SCREEN_SELECTOR_MATCHES,
    UNCONFIRMED_RULE,
    render_rule_result,
)
from app.qa.channel import (
    KnowledgeRequestFailed,
    QaCancelled,
    QaRunChannel,
    bounded_operator_wait,
    with_operator_messages,
)
from app.qa.envelope import (
    CapabilityActionRecord,
    CapabilityDiscoveredPayload,
    CapabilityVerdictPayload,
    IssuePayload,
    IssueSeverity,
    JsonRpcAction,
    KnowledgeCreatePayload,
    KnowledgeDeletePayload,
    KnowledgeLinkPayload,
    KnowledgeUnlinkPayload,
    KnowledgeUpdatePayload,
    LogCategory,
    MessageType,
    RunResult,
    ScreenSelectorEntry,
    ScreenSelectorRulePayload,
    StatusPayload,
    StepStatus,
)
from app.qa.schemas import QaStepResult


@dataclass(frozen=True)
class PendingCapture:
    """A captured screen waiting to be put in front of the model."""

    capture_id: str
    url: str
    mime_type: str
    caption: str


class QaRunState:
    """What the loop has done so far, for the tools that need to know."""

    def __init__(
        self,
        total_steps: int,
        step_meta: list[tuple[int | None, bool]] | None = None,
    ) -> None:
        self.total_steps = total_steps
        # 스텝별 (case_id, is_verification). report_step이 각 판정에 이를 붙이고, TC 판정(파생)은
        # `case_units`가 이 표로 구간을 잘라 그 구간 검증 스텝의 판정으로 정한다. 비면 단일-계층
        # 폴백(case_id/is_verification 미상) — 구식 호출자·테스트 호환용.
        self.step_meta: list[tuple[int | None, bool]] = step_meta or []
        self.step_results: list[QaStepResult] = []
        self.finished = False
        # The observation the agent last saw, so the next look is a diff.
        self.watermark = 0
        # 마지막 행위가 끝난 Unity 프레임. 판독의 창이 여기서 시작한다 — 타이머가 아니라
        # 행위가 경계다(ARTEL-621). `None` 은 아직 아무 행위도 없었거나, 그 필드를 모르는
        # 옛 SDK 다.
        self.last_action_frame: int | None = None
        # How many times `finish_run` was reached. The first attempt made with
        # steps still unreported is pushed back on; a second one closes the run
        # regardless, so a game that genuinely cannot go on is never trapped.
        self.finish_attempts = 0
        # Attempts, not successes. A game whose SDK does not know the capture action
        # refuses every one of them, and counting only what worked would leave that
        # loop unbounded — the cap has to bind on the failing case too.
        self.captures_attempted = 0
        # Attempts again, and for the same reason: a search Orchestration refuses
        # every time would otherwise be a loop with no bound on it.
        self.knowledge_searches_attempted = 0
        # Attempts for the knowledge writes too. A write is answered since
        # ARTEL-332, so successes COULD be counted now — the cap still counts
        # attempts because silence is a third outcome (see
        # `QaRunChannel.write_knowledge`) and a budget that only bound confirmed
        # writes would be unbounded against an Orchestration that never answers.
        self.knowledge_records_attempted = 0
        self.knowledge_updates_attempted = 0
        self.knowledge_forgets_attempted = 0
        # Attempts once more. A report the far side drops still cost the run a
        # call, and a cap that only counted accepted ones would not bound the
        # loop where every report is being rejected.
        self.issues_attempted = 0
        # What `search_knowledge` has actually shown the agent, id -> summary.
        # A correction or a deletion may only name an id from here. An agent free
        # to pass any id could rewrite or erase an entry it never read, and on the
        # far side that id resolves to a real row — Orchestration cannot tell the
        # difference, so the check only exists if it exists here.
        self.knowledge_seen: dict[str, str] = {}
        # Entries this run deleted and has not written a replacement for, as
        # printable labels, oldest first.
        #
        # `update_knowledge` is what correcting an entry should be, but a run may
        # still delete and then record, and this is the only thing that knows one
        # is halfway through doing so. It is what lets a failing `record_knowledge`
        # say what is missing instead of reporting a bare failure, and what exempts
        # a replacement write from the write cap.
        self.knowledge_deleted_unreplaced: list[str] = []
        # Entries shown only as a one-line neighbour, id -> summary. NEVER popped.
        #
        # Kept apart from `knowledge_seen` because the two license different
        # things. `seen` means read in full and is what `update_knowledge` and
        # `forget_knowledge` require — deletion is the most destructive thing the
        # agent does, and a 120-character line is not having read the entry.
        # `glimpsed` is enough to assert a relation or to expand from, neither of
        # which destroys anything and both of which a summary can justify.
        self.knowledge_glimpsed: dict[str, str] = {}
        self.knowledge_links_attempted = 0
        self.knowledge_unlinks_attempted = 0
        self.knowledge_expands_attempted = 0
        # 이 런이 content map 에 적어 만든 `capability_observation` 의 id → 무엇에 대한
        # 문장이었나 (ARTEL-644).
        #
        # `inferred` 를 적을 때 `based_on` 에 실을 수 있는 값이 이것뿐이고, 저쪽은 **이 런의**
        # observation 이 아니면 거절한다. 그래서 이 표가 없으면 모델이 지어낸 id 를 싣고 왕복
        # 하나를 거절로 쓴다 — `knowledge_seen` 이 있는 이유와 같다.
        self.capability_observations: dict[str, str] = {}
        # 이 런이 `record_new_capability` 로 만든 행의 id → 그 요약.
        #
        # 그 행들은 `capability_key` 가 NULL 이라(키의 산식에 넣을 `entry_id` 가 없다) 나중에
        # verdict 를 찍는 유일한 길이 id 다.
        self.capability_rows_written: dict[str, str] = {}
        # 이 런이 실제로 보낸 조작. method → 마지막으로 보낸 인자.
        #
        # `capability_observation.action_params` 에 재현이 앉는 자리이고, 그 값을 모델에게
        # 받아 적으면 안 된다 — JSON-RPC 인자는 모델이 지어내기 쉬운 모양이고 저쪽은 그것을
        # 읽지 않고 그대로 저장한다. 모델은 "무엇으로 눌렀나" 만 말하고 인자는 이 표가 낸다.
        self.dispatched_action_params: dict[str, list[Any]] = {}
        # Handed to the vision middleware on the next model call. The tool cannot
        # return the image itself — an image block on a tool result is rejected by
        # the chat/completions API every model here is reached through.
        self._pending_captures: list[PendingCapture] = []
        # Set by `compact_context`, read and cleared by the compaction middleware
        # before the next model call. A flag rather than the tool doing the work
        # itself: the tool runs inside the tools node, where its own AIMessage is
        # already in state, and rewriting the message list from there would strand
        # that call without its result. See `app/agents/qa/compaction.py`.
        self.compaction_requested = False
        # How many compactions this run has been through. Read by the thrash guard
        # and reported at the end of the run.
        self.compactions = 0

    def remember_glimpsed(self, neighbours) -> None:
        """Record neighbour lines the agent was shown.

        Without this the agent is printed ids it then cannot use at all — every
        graph tool refuses an endpoint the run has not been shown. An id-less
        neighbour is skipped rather than stored under the empty string, for the
        same reason an id-less hit is.
        """
        for neighbour in neighbours:
            if neighbour.id:
                self.knowledge_glimpsed[neighbour.id] = neighbour.summary

    def remember_dispatch(self, actions: list[JsonRpcAction]) -> None:
        """이 런이 게임에 보낸 조작을 method 별로 마지막 것만 남긴다.

        마지막 것만인 이유는 이 값이 쓰이는 자리 때문이다 — verdict 를 적는 tool 이
        "`button_click` 으로 눌렀다" 는 모델의 말에 인자를 채워 주는 것이고, 그 말이 가리키는
        것은 방금 보낸 것이다. 전부 쌓아 두면 어느 것이 그 말의 대상인지 이쪽이 못 고른다.
        """
        for action in actions:
            self.dispatched_action_params[action.method] = list(action.params)

    def knows_of(self, knowledge_id: str) -> bool:
        """Whether this run has been shown this entry at all, in full or as a line."""
        return knowledge_id in self.knowledge_seen or knowledge_id in self.knowledge_glimpsed

    @property
    def knowledge_writes_attempted(self) -> int:
        """Records and corrections against one allowance.

        Capped together because they fail together: either one spends the run's
        steps putting content into the knowledge base instead of reaching a
        verdict, which is the whole reason `max_records_per_run` exists.
        """
        return self.knowledge_records_attempted + self.knowledge_updates_attempted

    def unreported_steps(self) -> list[int]:
        """Scenario steps with no verdict yet, in order.

        Lives here rather than in the ledger that prints it because it is derived
        from `step_results`, and a second derivation elsewhere is how the ledger
        and the run come to disagree about what is left.
        """
        reported = {result.step for result in self.step_results}
        return [step for step in range(1, self.total_steps + 1) if step not in reported]

    def case_units(self) -> list[dict]:
        """연속 동일 case_id = 한 TC 구간. **TC 판정 = 그 구간의 검증(마지막) 스텝 판정**(2단 판정).

        `step_meta`(권위)로 구간을 자르고, 각 구간의 검증 스텝 판정을 `step_results`에서 찾아
        TC의 passed/message로 삼는다. 중간 스텝의 성공/실패는 각 스텝에 그대로 남아있고(steps),
        TC 판정과 별개다 — 중간이 실패해도 검증이 통과하면 TC는 통과다.
        """
        by_step = {result.step: result for result in self.step_results}
        units: list[dict] = []
        prev_cid: int | None = None
        for index, (cid, is_verification) in enumerate(self.step_meta, start=1):
            if cid is None:
                prev_cid = None
                continue
            if not units or cid != prev_cid:
                units.append(
                    {"case_no": len(units) + 1, "case_id": cid, "steps": [], "verify_step": None}
                )
            unit = units[-1]
            unit["steps"].append(index)
            if is_verification:
                unit["verify_step"] = index
            prev_cid = cid
        for unit in units:
            # 검증 스텝(없으면 구간 마지막 스텝)의 판정이 곧 TC 판정.
            decisive = unit["verify_step"] or (unit["steps"][-1] if unit["steps"] else None)
            result = by_step.get(decisive) if decisive else None
            unit["passed"] = bool(result and result.passed)
            unit["message"] = result.message if result else ""
        return units

    def build_summary(self) -> dict:
        """종단 STATUS에 싣는 2단 요약. steps[]가 원천, cases[]는 파생(구간 검증 스텝).

        finish_run(정상 종료)과 service._send_terminal(중단/실패)이 같은 형태를 내도록 한 곳에서
        만든다 — 두 경로가 다른 요약을 내면 다운스트림이 종료 사유마다 다른 스키마를 보게 된다.
        """
        total = self.total_steps
        steps_passed = sum(1 for result in self.step_results if result.passed)
        cases = self.case_units()
        cases_passed = sum(1 for unit in cases if unit["passed"])
        return {
            "steps": {
                "total": total,
                "passed": steps_passed,
                "failed": total - steps_passed,
                "items": [result.model_dump() for result in self.step_results],
            },
            "cases": {
                "total": len(cases),
                "passed": cases_passed,
                "failed": len(cases) - cases_passed,
                "items": cases,
            },
        }

    def add_pending_capture(self, capture: PendingCapture) -> None:
        self._pending_captures.append(capture)

    def take_pending_captures(self) -> list[PendingCapture]:
        pending = self._pending_captures
        self._pending_captures = []
        return pending


# The one description written out here rather than left as a docstring, because
# it has to name the cap and a docstring cannot interpolate one. An agent told
# only "screenshots are limited" spends them at the first opportunity; the number
# is what makes the budget something it can actually ration.
CAPTURE_SCREEN_DESCRIPTION = """Look at the actual picture on screen, not just the scene text.

Use this when the step is about how something *looks*: a layout that may be
broken, a button that may be covered by other UI, a sprite in the wrong state,
text that may be unreadable. The scene listing cannot express any of that.

Leave `target_id` out for the whole screen. Give one to see just that element's
area, at higher detail.

You get {limit} screenshots for the whole run and no more, so spend them on the
steps where looking is what decides the verdict.

The picture arrives right after this result, as its own message."""


# Interpolated for the same reason as the capture description above: the cap is
# the part the agent has to ration, and a docstring cannot name it.
REPORT_ISSUE_DESCRIPTION = """File a defect you found in the GAME, with the evidence for it.

This is not the step verdict — `report_step` is. Use this when what you saw is
wrong regardless of the scenario: a crash, a button that does nothing, a value
that goes the wrong way, text that overflows its box. A step can fail without
being a defect (the scenario may describe the game wrongly), and a step can pass
while you notice one in passing.

`severity` is one of {severities}, worst first:
- BLOCKER: the game cannot be played past this point.
- CRITICAL: a core feature is broken and nothing works around it.
- MAJOR: an important feature is broken but there is a way around it.
- MINOR: small or local damage.
- TRIVIAL: cosmetic — wording, alignment, a wrong colour.

`expected` and `actual` are what should have happened and what did. `reproduction`
is the shortest list of steps that shows it again, oldest first; write it for
someone who was not here.

You may file {limit} of these in one run. One defect, one call — do not file the
same broken screen again on the next step."""


def render_closing_asks(state: QaRunState) -> str:
    """마지막 스텝을 판정한 자리에서, 이 런이 무엇을 남기고 닫을지 묻는 문구.

    런 전체가 아직 앞에 있고 판정은 끝나서, 무엇이 값진 앎이었고 무엇이 결함이었는지
    되짚을 수 있는 마지막 순간이다.

    도구는 있는데 안 쓴다 — 실측으로 83턴 성공 런에서 `record_knowledge` 0회,
    `report_issue` 0회였다. 시스템 프롬프트가 시키는데도 그렇다. 기록은 이번 런의 판정에
    아무것도 안 보태므로(`report_step` 도 `finish_run` 도 그것 없이 통과한다) 비용만 있고
    돌아오는 것이 없는 행동이고, 무엇보다 **적을 순간이 흐름 안에 없었다**.

    되돌려 보내지 않는다. `finish_run` 에서 한 번 물리는 것도 해 봤는데, 런을 닫는 테스트
    열여덟 개가 걸렸다 — 앞으로 만드는 모든 런 테스트가 그 왕복을 치러야 한다는 뜻이고, 그
    값은 이 문구가 하는 일보다 크다(ARTEL-667).

    무엇을 어디에 적을지는 안 정해 준다. `record_knowledge` 가 anchor 를 부르는 쪽에
    맡기는 이유가 그대로 걸린다 — 어디서나 참인 규칙을 지금 서 있는 화면 아래 넣으면 다음
    화면의 런이 그것을 못 찾는다.

    결함을 묻는 근거는 **이 런이 실패로 판정한 스텝**이다. 실패한 스텝이 하나도 없으면
    결함은 묻지 않는다 — 근거 없이 물으면 agent 가 무엇에 대해 답할지 모르고, 그래도
    답하려고 아무거나 적으면 다음 런에 도움이 안 되는 줄만 쌓인다. 같은 이유로 마지막 줄이
    적을 것이 없다는 것도 답이라고 말한다.

    이미 시도한 런에게도 말한다. 한 번 시도한 것과 그 런이 알아낸 것을 다 적은 것은
    다르다. 대신 문구가 다르다 — 시키는 대신 지금까지 몇 번 시도했는지 세어 주고
    각 시도의 성공 여부와 나머지를 되짚게 한다. 이 counter 둘은 성공 건수가 아니다.
    """
    asks: list[str] = []
    failed = [result.step for result in state.step_results if not result.passed]
    if failed:
        steps = ", ".join(str(step) for step in failed)
        if not state.issues_attempted:
            asks.append(
                f"Steps judged failed this run: {steps}. Issue reports sent: none, so "
                "what failed and why is nowhere but this transcript. A failure "
                "that is a defect in the game goes in with `report_issue`; a step "
                "that failed because the scenario asked for the wrong thing, or "
                "because the run itself went wrong, is not one."
            )
        else:
            asks.append(
                f"Steps judged failed this run: {steps}. Issue reports sent: "
                f"{state.issues_attempted}. If a failure that is a defect in the "
                "game is not among them, `report_issue` still takes it."
            )
    if not state.knowledge_records_attempted:
        asks.append(
            "If this run worked anything out that a later run would otherwise "
            "work out again — how an input is read, what a control actually does, "
            "what a rule costs — write it down with `record_knowledge`."
        )
    else:
        asks.append(
            f"Knowledge recording attempts this run: {state.knowledge_records_attempted}. "
            "Check whether each one succeeded. An attempt is not the same as "
            "everything this run worked out. Read back over the rest of it: what "
            "else would a later run otherwise work out again? That goes in with "
            "`record_knowledge` too."
        )
    listed = "\n".join(f"- {ask}" for ask in asks)
    closer = (
        "Nothing to write is an answer. If there is nothing a later run would "
        "use, write nothing and close the run."
    )
    return f"\n{listed}\n{closer}"


def build_tools(
    channel: QaRunChannel, state: QaRunState, arch: ResolvedArch | None = None
) -> list[BaseTool]:
    arch = arch or default_resolved_arch()

    def _answer(body: str, messages: list[str], screen: bool = True) -> str:
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
        if screen and (channel.scene.pulse.seen or channel.scene.frames > 0):
            view = channel.scene.render(state.watermark, since_action=state.last_action_frame)
            state.watermark = channel.scene.updates
            # 화면이 곧 답인 도구(`observe_scene`)는 앞에 얹을 몸통이 없다.
            body = f"{body}\n\n{view}" if body else view

        # 오퍼레이터의 말이 맨 뒤다. 지금부터 적용되는 지시라 화면보다 나중에 읽혀야 한다.
        return with_operator_messages(body, messages)

    @tool
    async def observe_scene(
        step: int, thought: str, wait_seconds: float = 0.0, current_scene: bool = False
    ) -> str:
        """Look at the game screen. Returns what changed since your last look.

        Use `wait_seconds` when the screen needs time first — a loading screen, an
        animation, a countdown. Always look before acting.

        `current_scene` asks for the whole scene instead: every object being held
        and every value known, with `(changed)` on the ones that moved since your
        last look. Reach for it when the ordinary view cannot answer you — when a
        value you need has not moved since it was last shown, or when you want to
        act on something the view has not been printing and so have no address
        for. It is several times the size of the ordinary view, so ask for it
        when you need it, not every turn.

        `step` is the scenario step you are working on and `thought` is why you
        are looking; both go on the timeline.
        """
        arrived = await channel.look(wait_seconds)
        messages = channel.drain_operator_messages()
        if not arrived:
            return _answer(
                "The game did not answer. It may be loading, or it may be stuck. "
                "You can look again with a longer wait, or judge the step failed.",
                messages,
            )
        # 관측은 행위가 아니다. 마지막 **행위** 이후로 무엇이 쌓였는지를 그대로 보여준다 —
        # 관측할 때마다 경계를 옮기면, 두 번 보는 것만으로 그 사이의 변화를 잃는다.
        #
        # 그 경계를 `_answer` 가 들고 있으므로 여기서 따로 그리지 않는다. 이 도구는 화면이
        # 곧 답이라 몸통이 비어 있고, 화면은 `_answer` 에서 붙는다.
        if not current_scene:
            return _answer("", messages)

        # 전량은 창을 안 탄다. 그래서 `_answer` 의 창 뷰를 끄고 여기서 그린다 — 둘 다 내면
        # 같은 화면이 두 번 실린다(ARTEL-635 에서 이미 한 번 그랬다).
        #
        # 행위 경계(`state.last_action_frame`)는 **안 건드린다.** 관측은 행위가 아니고,
        # 전량을 봤다고 해서 그 사이 무엇이 쌓였는지를 잊어도 되는 것이 아니다.
        view = channel.scene.current_scene()
        state.watermark = channel.scene.updates
        return _answer(view, messages, screen=False)

    async def _run(actions: list[JsonRpcAction], summary: str, step: int) -> str:
        """Every acting tool goes through here: act, then look at what it did.

        Takes a list because a drag is only a drag when its actions ride in one
        batch — the SDK runs a batch strictly in order, so nothing can slip
        between the press and the release.
        """
        # 무엇을 어떤 인자로 보냈는지 남긴다. capability 에 verdict 를 적을 때 재현이
        # 여기서 나온다 — 모델은 method 이름만 말하고 인자는 이 기록이 낸다(ARTEL-644).
        state.remember_dispatch(actions)
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

    @tool
    async def inspect_object(step: int, selector: str, thought: str) -> str:
        """Read every value the game holds on one object.

        The scene block shows what CHANGED and what you can ACT ON. It does not
        list every value, because most of them do not move and a screen with a
        hundred objects would push everything else out of your context.

        Use this when a step turns on a value you cannot see there — an enemy's
        health, a counter, a flag. `selector` is the address printed in the scene
        block, and a partial one matches (`RangedCat` finds `RangedCat(Clone)[17]`).
        """
        found = channel.scene.pulse.inspect(selector)
        messages = channel.drain_operator_messages()
        return _answer(found, messages)

    @tool(description=CAPTURE_SCREEN_DESCRIPTION.format(limit=arch.max_captures_per_run))
    async def capture_screen(step: int, thought: str, target_id: int | None = None) -> str:
        # What the agent reads is CAPTURE_SCREEN_DESCRIPTION above, not this.
        # Returns the capture as a promise: the image itself is handed to the
        # vision middleware and arrives on the next model call.
        if state.captures_attempted >= arch.max_captures_per_run:
            # Refused with the reason, not silently: a run that keeps looking
            # instead of deciding will reach the deadline with nothing reported.
            return (
                f"You have used all {arch.max_captures_per_run} screenshots for this run. "
                "Judge the remaining steps from the scene text."
            )

        state.captures_attempted += 1
        params: list[Any] = [] if target_id is None else [target_id]
        what = "the screen" if target_id is None else f"element {target_id}"
        result = await channel.dispatch_actions(
            [JsonRpcAction(id=1, method="capture_screen", params=params)],
            f"Capturing {what}",
            step,
        )
        messages = channel.drain_operator_messages()

        if result is None or not result.results:
            return _answer(
                "The game did not answer the capture. Try again, or judge from the "
                "scene text.",
                messages,
            )

        item = result.results[0]
        if not item.success:
            # Says what to do instead, not just what went wrong. A game built on an SDK
            # without this action answers "Unsupported method" to every capture, and an
            # agent told only that failed the step and then the whole run — over a
            # screenshot it could have done without.
            return _answer(
                f"The screen could not be captured — {item.error or 'no reason given'}. "
                "Judge this step from the scene text instead, and do not capture again "
                "in this run.",
                messages,
            )

        captured = item.returnValue or {}
        url = captured.get("url")
        if not url:
            return _answer(
                "The game reported a capture but no image to read. Judge from the "
                "scene text.",
                messages,
            )

        caption = f"This is {what} right now."
        if captured.get("clipped"):
            # Worth saying out loud: a cropped-off element is itself a finding, and
            # the agent would otherwise read the partial image as the whole thing.
            caption += " Part of it is off the edge of the screen."

        state.add_pending_capture(
            PendingCapture(
                capture_id=str(captured.get("captureId") or ""),
                url=url,
                mime_type=str(captured.get("mimeType") or "image/jpeg"),
                caption=caption,
            )
        )
        # On the timeline so a reviewer can open exactly what the agent looked at.
        await channel.note(f"Captured {what}: {url}", LogCategory.OBSERVATION, step)

        return _answer(f"Captured {what}. The image follows.", messages)

    @tool(
        description=SEARCH_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_searches_per_run, tags=", ".join(KNOWLEDGE_TAGS)
        )
    )
    async def search_knowledge(
        step: int, thought: str, query: str, tag: str | None = None
    ) -> str:
        # What the agent reads is SEARCH_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Deliberately not routed through `_run`: that path dispatches actions and
        # appends the scene they produced. A search moves nothing on screen, so a
        # scene view here would be the same picture the agent already has, paid for
        # again in context — exactly what `app/agents/qa/context.py` exists to stop.
        if state.knowledge_searches_attempted >= arch.max_searches_per_run:
            return (
                f"You have used all {arch.max_searches_per_run} knowledge searches for "
                "this run. Judge the remaining steps from the scenario and what you "
                "can see."
            )

        topic = (tag or "").strip().upper()
        if topic and topic not in KNOWLEDGE_TAGS:
            # Refused before it goes out. Orchestration rejects an unknown tag
            # outright rather than ignoring it, so sending one would spend a round
            # trip and a search out of the run's budget to be told something this
            # side already knew.
            return (
                f"{tag!r} is not a knowledge topic, so the search was not sent. "
                f"Use one of {', '.join(KNOWLEDGE_TAGS)}, or leave `tag` out."
            )

        state.knowledge_searches_attempted += 1
        answer = await channel.search_knowledge(query, topic or None, RESULT_LIMIT, step)
        messages = channel.drain_operator_messages()
        remaining = arch.max_searches_per_run - state.knowledge_searches_attempted

        if answer is None:
            return with_operator_messages(
                "The knowledge base did not answer in time. Judge this step from "
                f"the scenario and what you can see. {remaining} search(es) left.",
                messages,
            )
        if isinstance(answer, KnowledgeRequestFailed):
            # Said plainly, with what to do instead. A failed lookup is a side
            # errand that failed, not a failed step — an agent told only that
            # something went wrong has been known to fail the step over it.
            return with_operator_messages(
                f"The knowledge search could not run — {answer.reason}. This says "
                "nothing about the game; judge this step from the scenario and what "
                f"you can see. {remaining} search(es) left.",
                messages,
            )
        # Remembered before the result is rendered, because this is what makes an
        # entry writable at all: `update_knowledge` and `forget_knowledge` both
        # refuse an id that never came back from a search in this run. An id-less
        # hit is skipped rather than stored under the empty string — it is not
        # addressable, so it can be neither corrected nor deleted.
        for entry in answer.results:
            if entry.id:
                state.knowledge_seen[entry.id] = entry.summary
            # Neighbours are recorded SEPARATELY, and the difference is the point.
            # `knowledge_seen` means "read in full", and that is the precondition
            # `update_knowledge` and `forget_knowledge` rest on; a clipped one-line
            # summary is not having read something. Putting neighbours in here
            # would let a run delete an entry it has only glimpsed, which is the
            # first regression this feature could ship.
            state.remember_glimpsed(entry.neighbors)
        return with_operator_messages(render_results(answer, remaining), messages)

    @tool(
        description=RECORD_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_records_per_run,
            tags=", ".join(KNOWLEDGE_TAGS),
        )
    )
    async def record_knowledge(
        step: int,
        thought: str,
        tag: str,
        summary: str,
        description: str,
        scene_name: str | None = None,
        screen_id: str | None = None,
    ) -> str:
        # What the agent reads is RECORD_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Not routed through `_run`, for the same reason `search_knowledge` is not:
        # nothing here touches the game, so a scene view on the result would be the
        # picture the agent already has, paid for again in context (ARTEL-180).
        #
        # Every refusal below carries `render_missing_knowledge_warning`. This tool
        # is the second half of a repair as often as it is a first write, and a
        # refusal phrased only as "nothing was recorded" reads as harmless in the
        # one case where it is not.
        outstanding = state.knowledge_deleted_unreplaced

        # The cap does not bind a replacement write. It exists to stop a run
        # narrating into the knowledge base; applied to the second half of a
        # repair it would make the budget itself the thing that loses knowledge.
        #
        # Corrections count against the same allowance — see
        # `QaRunState.knowledge_writes_attempted`.
        if state.knowledge_writes_attempted >= arch.max_records_per_run and not outstanding:
            return (
                f"You have used all {arch.max_records_per_run} knowledge writes for this "
                "run, so nothing was recorded. Carry on with the run and judge the "
                "remaining steps."
            )

        topic = (tag or "").strip().upper()
        if topic not in KNOWLEDGE_TAGS:
            # Refused before it goes out, as with a search's tag. Orchestration
            # rejects an unknown topic, and its rejection never comes back down
            # this socket — so a frame sent anyway would leave the run believing it
            # had written something.
            return (
                f"{tag!r} is not a knowledge topic, so nothing was recorded. Use one "
                f"of {', '.join(KNOWLEDGE_TAGS)} and call this again."
            ) + render_missing_knowledge_warning(outstanding)

        fact = summary.strip()
        detail = description.strip()
        if not fact or not detail:
            # Same reason as the tag: Orchestration rejects a blank one on arrival
            # and says so only on its own timeline.
            return (
                "`summary` and `description` must both say something, so nothing was "
                "recorded. Write them out and call this again."
            ) + render_missing_knowledge_warning(outstanding)

        # The anchor is whatever the agent named, and nothing else. There is no line
        # here that reads the run's current scene, and there must not be: a rule true
        # everywhere would then be filed under whichever screen the run happened to
        # be standing on, and a rule filed that way is one the run on the next screen
        # never finds.
        scene = (scene_name or "").strip() or None
        screen = (screen_id or "").strip() or None
        if scene is None and screen is not None:
            # Orchestration refuses this pair as well. Refused here first for the
            # reason the tag and the blank summary are — and this one is worth the
            # words, because the mistake has an obvious repair the agent can make.
            return (
                "`screen_id` needs the `scene_name` it belongs to, so nothing was "
                "recorded. Name the scene as well and call this again, or leave both "
                "out if this fact is true wherever the player is."
            ) + render_missing_knowledge_warning(outstanding)

        state.knowledge_records_attempted += 1
        try:
            answer = await channel.write_knowledge(
                MessageType.KNOWLEDGE_CREATE,
                KnowledgeCreatePayload(
                    tag=topic,
                    summary=fact,
                    description=detail,
                    scene_name=scene,
                    screen_id=screen,
                ),
            )
        except QaCancelled:
            # The operator ended the run. That is not this tool's to swallow.
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            # Storing knowledge is a side errand to the verdict, so a failed write
            # is reported and the run goes on. It is still stated plainly: an agent
            # told nothing would move on believing the fact was filed.
            return (
                f"The knowledge write could not be sent — {error}. Nothing was recorded."
            ) + render_missing_knowledge_warning(outstanding)

        if isinstance(answer, KnowledgeRequestFailed):
            # A refusal reaches the model since ARTEL-331/332. It used to become an
            # ERROR row on the operator's timeline and nothing else, which meant a
            # frame this side should not have sent was reported here as a success.
            # A deletion still owed is named too — this is the path that loses it.
            return (
                f"The knowledge base refused the entry — {answer.reason}. Nothing was recorded."
            ) + render_missing_knowledge_warning(outstanding)

        replaced = bool(outstanding)
        state.knowledge_deleted_unreplaced = []
        messages = channel.drain_operator_messages()
        remaining = max(arch.max_records_per_run - state.knowledge_writes_attempted, 0)

        # "Recorded" only when Orchestration said so. Silence gets the older,
        # weaker word — the frame left, and that is all this side can claim.
        lines = [
            f'Recorded under {topic}: "{fact}".'
            if answer is not None
            else f'Sent to the knowledge base, filed under {topic}: "{fact}".'
        ]
        if answer is not None and answer.knowledge_id:
            # Into `knowledge_seen`, not `knowledge_glimpsed`. That map is the
            # precondition `update_knowledge` and `forget_knowledge` rest on, and
            # it means "read in full" — which the run wrote itself certainly is.
            # Without this a run has to spend a search to correct its own entry.
            state.knowledge_seen[answer.knowledge_id] = fact
            lines.append(
                f"Its id is {answer.knowledge_id}. Use `update_knowledge` with that "
                "id if you learn this entry is wrong later in the run — you do not "
                "need to search for it first."
            )
        if replaced:
            lines.append(
                "That completes the correction — the entry you deleted has been "
                "replaced, and nothing is outstanding. Next time use "
                "`update_knowledge`: it repairs an entry in one call, and the "
                "replacement keeps the original's id."
            )
        if answer is None:
            lines.append(UNCONFIRMED_WRITE)
        lines.append(f"{remaining} knowledge write(s) left.")
        return with_operator_messages("\n\n".join(lines), messages)

    @tool(
        description=UPDATE_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_records_per_run, tags=", ".join(KNOWLEDGE_TAGS)
        )
    )
    async def update_knowledge(
        step: int,
        thought: str,
        knowledge_id: str,
        tag: str | None = None,
        summary: str | None = None,
        description: str | None = None,
    ) -> str:
        # What the agent reads is UPDATE_KNOWLEDGE_DESCRIPTION, not this.
        #
        # No scene view, for the reason given on `record_knowledge` (ARTEL-180).
        # The write itself is awaited since ARTEL-332 — briefly, and the wait is
        # bounded by `KNOWLEDGE_WRITE_TIMEOUT_SECONDS` rather than the search's,
        # because no answer is a normal outcome rather than a fault.
        #
        # The budget is `max_records_per_run`, shared with `record_knowledge`
        # rather than counted apart, because both fail the run the same way — see
        # `QaRunState.knowledge_writes_attempted`. The constraint that a repair must
        # never be left half done by the budget still holds, from both ends: a
        # refused correction changes nothing, since it is one call and the entry is
        # untouched, and a delete-then-record still has its own exemption above.
        outstanding = state.knowledge_deleted_unreplaced

        def refused(reason: str) -> str:
            """A refusal, with whatever the run still owes appended to it.

            The rule `record_knowledge`'s refusals follow, and it applies here for
            a reason particular to this tool: `record_knowledge` is exempt from the
            cap while a deletion is outstanding, so a budget refusal from HERE is
            the only one a run can meet in the middle of a delete-then-record
            repair. Phrased as a bare "nothing was changed" it would read as
            harmless in exactly the state where it is not.
            """
            return reason + render_missing_knowledge_warning(outstanding)

        if state.knowledge_writes_attempted >= arch.max_records_per_run:
            return refused(
                f"You have used all {arch.max_records_per_run} knowledge writes for "
                "this run, so nothing was changed and the entry stands as it was."
            )

        target = (knowledge_id or "").strip()
        if target not in state.knowledge_seen:
            # The same guard `forget_knowledge` makes, for the same reason: on the
            # far side this id resolves to a real row, and nothing there can tell
            # that the agent never read it. An entry already deleted in this run is
            # gone from `knowledge_seen` too, so a correction cannot resurrect one.
            if target in state.knowledge_glimpsed:
                # Named as a neighbour line but never read in full. Said apart from
                # the case below because otherwise the agent meets a refusal it
                # cannot explain — it can see the id right there in the transcript.
                return refused(
                    f"Nothing was changed: you have seen {knowledge_id!r} only as a "
                    "neighbour line, which is a clipped summary rather than the "
                    "entry. Search for it so you read it in full, then correct it."
                )
            return refused(
                f"Nothing was changed: {knowledge_id!r} is not an entry "
                "`search_knowledge` returned in this run, and you can only correct "
                "what you have read. Search for it first and use the id printed "
                "with the hit."
            )

        # `None` and `""` are different requests and are kept apart all the way
        # down: an omitted field is left alone on the far side, a field sent blank
        # is rejected there. So a blank tag falls into the refusal below rather
        # than being read as "leave the topic alone" — the two spellings must not
        # quietly mean the same thing when the message here says they do not.
        topic = tag.strip().upper() if tag is not None else None
        if topic is not None and topic not in KNOWLEDGE_TAGS:
            # Refused before it goes out, as on a record. Orchestration rejects an
            # unknown topic and says so only on its own timeline, so a frame sent
            # anyway would leave the run believing the entry had been corrected.
            return refused(
                f"{tag!r} is not a knowledge topic, so nothing was changed. Use one "
                f"of {', '.join(KNOWLEDGE_TAGS)}, or leave `tag` out to keep the "
                "topic it already has."
            )

        fact = summary.strip() if summary is not None else None
        detail = description.strip() if description is not None else None
        if (summary is not None and not fact) or (description is not None and not detail):
            return refused(
                "Nothing was changed: `summary` and `description` must say something "
                "when you send them. Leave a field out entirely to keep what the "
                "entry already has, and call this again."
            )
        if topic is None and fact is None and detail is None:
            return refused(
                "Nothing was changed: a correction has to carry at least one of "
                "`tag`, `summary` or `description`. Say what the entry should now "
                "be, or use `forget_knowledge` if it should simply be gone."
            )

        state.knowledge_updates_attempted += 1
        try:
            answer = await channel.write_knowledge(
                MessageType.KNOWLEDGE_UPDATE,
                KnowledgeUpdatePayload(
                    knowledge_id=target, tag=topic, summary=fact, description=detail
                ),
            )
        except QaCancelled:
            # The operator ended the run. That is not this tool's to swallow.
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            # Nothing left the socket, so this entry is exactly as it was. Said
            # plainly all the same: an agent told only that something failed
            # carries on believing the entry is now right. A deletion still owed
            # from earlier is named too, for the reason `refused` gives.
            return refused(
                f"The correction could not be sent — {error}. Nothing was changed "
                "and the entry is still on file exactly as it was."
            )

        if isinstance(answer, KnowledgeRequestFailed):
            return refused(
                f"The knowledge base refused the correction — {answer.reason}. Nothing "
                "was changed and the entry is still on file exactly as it was."
            )

        # Still an entry this run has read, so it stays correctable and deletable —
        # a correction is not a reason to forget having seen it. The stored summary
        # follows the correction because it is what every later label prints: left
        # alone, a `forget_knowledge` after this would name the sentence the agent
        # has just replaced.
        state.knowledge_seen[target] = (
            fact if fact is not None else state.knowledge_seen[target]
        )
        messages = channel.drain_operator_messages()
        remaining = max(arch.max_records_per_run - state.knowledge_writes_attempted, 0)

        changed = ", ".join(
            name
            for name, value in (("tag", topic), ("summary", fact), ("description", detail))
            if value is not None
        )
        closing = UNCONFIRMED_WRITE if answer is None else "Do not send it again in this run."
        # Labelled with the summary the entry now has, not the one it had. Every
        # other write result echoes what was sent, and a sentence quoted right
        # after the word "Corrected" is read as the entry's current text — printing
        # the replaced one here would teach the run the correction had not landed.
        return with_operator_messages(
            f"Corrected {render_entry_label(target, state.knowledge_seen[target])}. "
            f"Sent: {changed}; the rest of the entry is left as it was. It keeps "
            "its id, so this stays readable as a repair rather than as a deletion "
            f"and a new entry.\n\n{closing} {remaining} knowledge write(s) left.",
            messages,
        )

    @tool(description=FORGET_KNOWLEDGE_DESCRIPTION.format(limit=arch.max_forgets_per_run))
    async def forget_knowledge(step: int, thought: str, knowledge_id: str) -> str:
        # What the agent reads is FORGET_KNOWLEDGE_DESCRIPTION, not this.
        #
        # No scene view here either, for the reason given on `record_knowledge`.
        if state.knowledge_forgets_attempted >= arch.max_forgets_per_run:
            return (
                f"You have used all {arch.max_forgets_per_run} knowledge deletion(s) for "
                "this run, so nothing was deleted. If another entry still looks "
                "wrong, say so in `report_step` instead of deleting it."
            )

        target = (knowledge_id or "").strip()
        if target not in state.knowledge_seen:
            # The whole guard against deleting blind. Orchestration resolves this id
            # to a real row and has no way to know the agent never read it, so this
            # check exists here or nowhere. An id already deleted in this run is
            # gone from `knowledge_seen` too, which is what stops a second delete.
            if target in state.knowledge_glimpsed:
                return (
                    f"Nothing was deleted: you have seen {knowledge_id!r} only as a "
                    "neighbour line, which is a clipped summary rather than the "
                    "entry. Deleting on that is exactly what this guard is for — "
                    "search for it, read it in full, and decide then."
                )
            return (
                f"Nothing was deleted: {knowledge_id!r} is not an entry "
                "`search_knowledge` returned in this run, and you can only delete "
                "what you have read. Search for it first and use the id printed with "
                "the hit."
            )

        state.knowledge_forgets_attempted += 1
        try:
            answer = await channel.write_knowledge(
                MessageType.KNOWLEDGE_DELETE, KnowledgeDeletePayload(knowledge_id=target)
            )
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            # Nothing went out, so nothing was deleted and nothing is outstanding.
            # The entry stays in `knowledge_seen`, which leaves it retryable.
            return (
                f"The deletion could not be sent — {error}. Nothing was deleted and "
                "the entry is still on file."
            )

        if isinstance(answer, KnowledgeRequestFailed):
            # Refused, so nothing was deleted — and crucially nothing is outstanding
            # either. Returning before the bookkeeping below is what keeps this out
            # of `knowledge_deleted_unreplaced`, which exists to chase a real loss.
            return (
                f"The knowledge base refused the deletion — {answer.reason}. Nothing "
                "was deleted and the entry is still on file."
            )

        # Taken out of what may be deleted and recorded as outstanding, in that
        # order, before the result is composed: from here on a `record_knowledge`
        # that fails is able to name exactly what is missing.
        label = render_entry_label(target, state.knowledge_seen.pop(target, ""))
        state.knowledge_deleted_unreplaced.append(label)
        messages = channel.drain_operator_messages()
        remaining = max(arch.max_forgets_per_run - state.knowledge_forgets_attempted, 0)

        # Silence is treated as a deletion that probably happened: the entry leaves
        # `knowledge_seen` and joins the outstanding list above either way. The
        # cautious reading is the safe one here — a deletion wrongly believed to
        # have failed leaves the run thinking knowledge is still on file when it
        # may not be, and that is the state this tool's warning exists to prevent.
        unknown = "\n\n" + UNCONFIRMED_WRITE if answer is None else ""
        return with_operator_messages(
            f"Deleted {label}. This cannot be undone from here.{unknown}\n\n"
            "If you deleted it in order to CORRECT it, that was `update_knowledge`, "
            "and what you have now is half a repair: call `record_knowledge` NOW "
            "with the corrected version, before anything else, or this run has "
            "removed the knowledge rather than fixed it.\n\n"
            f"{remaining} deletion(s) left in this run.",
            messages,
        )

    @tool(
        description=LINK_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_links_per_run, relations=", ".join(KNOWLEDGE_RELATIONS)
        )
    )
    async def link_knowledge(
        step: int,
        thought: str,
        from_knowledge_id: str,
        to_knowledge_id: str,
        relation: str,
        note: str,
    ) -> str:
        # What the agent reads is LINK_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Not routed through `_run`, for the same reason the other knowledge tools
        # are not: nothing here touches the game.
        #
        # EVERY check below happens before the frame goes out. Orchestration now
        # answers a refusal (ARTEL-332), so this is no longer the only thing
        # standing between a bad frame and a false success — but it still saves a
        # round trip, and the run's clock is the reason to keep it. The two say the
        # same thing now instead of one of them saying nothing.
        if state.knowledge_links_attempted >= arch.max_links_per_run:
            return (
                f"You have used all {arch.max_links_per_run} knowledge links for this "
                "run, so nothing was linked. Spend the rest of the run judging steps."
            )

        kind = (relation or "").strip().upper()
        if kind not in KNOWLEDGE_RELATIONS:
            return (
                f"{relation!r} is not a knowledge relation, so nothing was sent. "
                f"Use one of {', '.join(KNOWLEDGE_RELATIONS)} — and if none of them "
                "fits, do not link these two at all."
            )

        reason = (note or "").strip()
        if not reason:
            # The far side stores `note` NOT NULL and would drop this frame in
            # silence. Refused here so the agent learns the link did not happen.
            return (
                "Nothing was linked: `note` is required. It is the only record of "
                "why you thought the connection was real, and of any condition it "
                "holds under."
            )

        source = (from_knowledge_id or "").strip()
        target = (to_knowledge_id or "").strip()
        if source == target:
            return "Nothing was linked: an entry cannot be related to itself."
        for endpoint in (source, target):
            if not state.knows_of(endpoint):
                return (
                    f"Nothing was linked: {endpoint!r} is not an entry this run has "
                    "been shown. Search for it first and use the id printed with the "
                    "hit or with a neighbour line."
                )

        state.knowledge_links_attempted += 1
        try:
            answer = await channel.write_knowledge(
                MessageType.KNOWLEDGE_LINK,
                KnowledgeLinkPayload(
                    from_knowledge_id=source,
                    to_knowledge_id=target,
                    relation=kind,
                    note=reason,
                ),
            )
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            return f"The link could not be sent — {error}. Nothing was linked."

        if isinstance(answer, KnowledgeRequestFailed):
            # This is the refusal the local checks above were standing in for. They
            # stay: catching a bad relation here still saves a round trip, and the
            # two now say the same thing rather than one of them saying nothing.
            return f"The knowledge base refused the link — {answer.reason}. Nothing was linked."

        messages = channel.drain_operator_messages()
        remaining = arch.max_links_per_run - state.knowledge_links_attempted
        opening = "Sent" if answer is None else "Linked"
        closing = UNCONFIRMED_WRITE if answer is None else "Do not send it again."
        return with_operator_messages(
            f"{opening}: {source} {kind.lower()} {target}. {closing}\n\n"
            f"{remaining} link(s) left in this run.",
            messages,
        )

    @tool(description=UNLINK_KNOWLEDGE_DESCRIPTION.format(limit=arch.max_unlinks_per_run))
    async def unlink_knowledge(
        step: int,
        thought: str,
        from_knowledge_id: str,
        to_knowledge_id: str,
        relation: str,
    ) -> str:
        # What the agent reads is UNLINK_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Validated locally for the same reason `link_knowledge` is: a round trip
        # saved, on a run that has a clock.
        if state.knowledge_unlinks_attempted >= arch.max_unlinks_per_run:
            return (
                f"You have used all {arch.max_unlinks_per_run} knowledge unlink(s) for "
                "this run, so nothing was removed. If another link still looks wrong, "
                "say so in `report_issue` instead."
            )

        kind = (relation or "").strip().upper()
        if kind not in KNOWLEDGE_RELATIONS:
            return (
                f"{relation!r} is not a knowledge relation, so nothing was sent. "
                f"Name the relation as it was printed to you, one of "
                f"{', '.join(KNOWLEDGE_RELATIONS)}."
            )

        source = (from_knowledge_id or "").strip()
        target = (to_knowledge_id or "").strip()
        for endpoint in (source, target):
            if not state.knows_of(endpoint):
                return (
                    f"Nothing was removed: {endpoint!r} is not an entry this run has "
                    "been shown, so you have not seen the link you are removing."
                )

        state.knowledge_unlinks_attempted += 1
        try:
            answer = await channel.write_knowledge(
                MessageType.KNOWLEDGE_UNLINK,
                KnowledgeUnlinkPayload(
                    from_knowledge_id=source, to_knowledge_id=target, relation=kind
                ),
            )
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            return f"The unlink could not be sent — {error}. Nothing was removed."

        if isinstance(answer, KnowledgeRequestFailed):
            # "is not linked" arrives here, and it is worth telling the model: it
            # means the relation it believed in was never there. The local checks
            # above cannot see that — they only know the endpoints were shown.
            return f"The knowledge base refused the unlink — {answer.reason}. Nothing was removed."

        messages = channel.drain_operator_messages()
        remaining = arch.max_unlinks_per_run - state.knowledge_unlinks_attempted
        opening = (
            f"Sent: removing {source} {kind.lower()} {target}."
            if answer is None
            else f"Removed: {source} {kind.lower()} {target}."
        )
        closing = UNCONFIRMED_WRITE if answer is None else "Do not send it again."
        return with_operator_messages(
            f"{opening} {closing}\n\n{remaining} unlink(s) left in this run.",
            messages,
        )

    @tool(
        description=EXPAND_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_expands_per_run,
            relations=", ".join(KNOWLEDGE_RELATIONS),
            similar=SIMILAR_LABEL,
        )
    )
    async def expand_knowledge(
        step: int, thought: str, knowledge_id: str, depth: int = 1
    ) -> str:
        # What the agent reads is EXPAND_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Three outcomes handled exactly as `search_knowledge` handles them, and
        # for the same reason: none of timeout, refusal or empty answer is a reason
        # to fail the step.
        if state.knowledge_expands_attempted >= arch.max_expands_per_run:
            return (
                f"You have used all {arch.max_expands_per_run} knowledge expansions "
                "for this run. The neighbours already printed with your searches are "
                "what you have."
            )

        target = (knowledge_id or "").strip()
        if not state.knows_of(target):
            return (
                f"{target!r} is not an entry this run has been shown, so nothing was "
                "expanded. Search for it first and use the id printed with the hit."
            )

        state.knowledge_expands_attempted += 1
        # Clamped here as well as on the far side. Orchestration clamps rather than
        # refuses, so this only saves a round trip — but it also keeps the number
        # the agent is told about and the number it gets from drifting apart.
        answer = await channel.expand_knowledge(
            target, max(1, min(depth, MAX_EXPAND_DEPTH)), include_similar=True, step=step
        )
        messages = channel.drain_operator_messages()
        remaining = arch.max_expands_per_run - state.knowledge_expands_attempted

        if answer is None:
            return with_operator_messages(
                "The knowledge base did not answer in time. Carry on with what you "
                f"already have. {remaining} expansion(s) left.",
                messages,
            )
        if isinstance(answer, KnowledgeRequestFailed):
            return with_operator_messages(
                f"The expansion could not run — {answer.reason}. This says nothing "
                f"about the game. {remaining} expansion(s) left.",
                messages,
            )
        # Same split as a search: what the expansion showed is glimpsed, not read.
        state.remember_glimpsed(answer.neighbors)
        return with_operator_messages(render_expansion(answer, remaining), messages)

    async def _write_screen_selector_rule(
        match: str, pattern: str, reason: str, screen_defining: bool
    ) -> str:
        """두 tool 이 지나는 한 자리 (ARTEL-657).

        둘은 `screen_defining` 하나만 다르고, 저장되는 것도 같은 표의 같은 행 모양이다.
        가르면 거절 처리와 결과 문장이 두 벌이 되고, 언제나 한쪽만 고쳐진다.

        **`scene` 을 인자로 받지 않는다.** 목록은 `scene` 단위이고, agent 는 지금 서 있는
        `scene` 에 대해서만 근거를 갖는다 — 인자로 받으면 그 규칙이 모델의 성실함에 걸리고,
        여기서 채우면 구조로 걸린다.

        정규식인지를 여기서 판별하지 않는다. `.*` 는 어떤 selector 와도 글자 그대로 같지
        않으므로 저쪽의 "이 `scene` 에서 본 적 없다" 검사에 그대로 걸리고, 메타문자로
        걸러 보려던 판은 실측 selector 에 `Card(Clone)[37]` 처럼 괄호가 들어 있어 멀쩡한
        항목을 거절했다. `match` 의 의미(index 지우기, 마디 경계)를 여기서 또 구현하는 것도
        같은 이유로 안 한다 — 그 규칙은 이미 Kotlin 과 SQL 에 두 벌 있고, 세 번째 벌이
        어긋나는 순간 화면이 조용히 갈린다.
        """
        scene = (channel.scene.scene or channel.scene.pulse.scene or "").strip()
        if not scene:
            return (
                "The run has not been told which scene it is standing on yet, so "
                "nothing was changed. Observe the scene first."
            )

        kind = (match or "").strip().lower()
        if kind not in SCREEN_SELECTOR_MATCHES:
            return (
                f"{match!r} is not one of {', '.join(SCREEN_SELECTOR_MATCHES)}, so "
                "nothing was changed. Pick one and call this again."
            )

        target = (pattern or "").strip()
        if not target:
            return "`pattern` must name a selector, so nothing was changed."
        if len(target) > MAX_PATTERN_LENGTH:
            return (
                f"`pattern` is longer than {MAX_PATTERN_LENGTH} characters, so "
                "nothing was changed."
            )

        why = (reason or "").strip()
        if not why:
            # 저쪽도 거절한다. 여기서 먼저 거절하는 것은 왕복 하나를 아끼기 위해서이고,
            # 그보다 이 거절이 고칠 수 있는 것이기 때문이다 — 무엇을 봤는지 쓰면 된다.
            return (
                "`reason` is required, so nothing was changed. Write what you saw in "
                "one sentence — an entry nobody can retrace is one nobody can later "
                "decide to remove — and call this again."
            )

        try:
            answer = await channel.write_screen_selector_rule(
                ScreenSelectorRulePayload(
                    scene=scene,
                    entries=[
                        ScreenSelectorEntry(
                            match=kind,
                            pattern=target,
                            screen_defining=screen_defining,
                            reason=why,
                        )
                    ],
                )
            )
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - 지도를 고치다 런이 끝나면 안 된다
            return f"The change could not be sent — {error}. Nothing was changed."

        messages = channel.drain_operator_messages()
        if isinstance(answer, KnowledgeRequestFailed):
            return with_operator_messages(
                f"The content map refused the change — {answer.reason}. Nothing was "
                "changed.",
                messages,
            )
        if answer is None:
            return with_operator_messages(UNCONFIRMED_RULE, messages)
        return with_operator_messages(render_rule_result(answer), messages)

    @tool(description=INCLUDE_SCREEN_SELECTOR_DESCRIPTION)
    async def include_screen_selector(
        step: int, thought: str, match: str, pattern: str, reason: str
    ) -> str:
        # What the agent reads is INCLUDE_SCREEN_SELECTOR_DESCRIPTION, not this.
        #
        # 화면을 안 돌려준다. 이 호출은 게임을 건드리지 않으므로 화면을 실으면 에이전트가
        # 이미 들고 있는 것을 문맥에 한 번 더 사는 것이다 — 지식 tool 들과 같은 판단이다
        # (ARTEL-180). 무엇이 달라졌는지는 다음 관측의 `content map:` 줄에서 보인다.
        return await _write_screen_selector_rule(
            match, pattern, reason, screen_defining=True
        )

    @tool(description=EXCLUDE_SCREEN_SELECTOR_DESCRIPTION)
    async def exclude_screen_selector(
        step: int, thought: str, match: str, pattern: str, reason: str
    ) -> str:
        # What the agent reads is EXCLUDE_SCREEN_SELECTOR_DESCRIPTION, not this.
        return await _write_screen_selector_rule(
            match, pattern, reason, screen_defining=False
        )

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

    @tool
    async def click_button(step: int, target_id: int, thought: str) -> str:
        """Click a button. `target_id` must be an id from the scene you just saw.

        `step` is the scenario step this belongs to and `thought` is why you are
        clicking; both go on the timeline.
        """
        return await _run(
            [JsonRpcAction(id=1, method="button_click", params=[target_id])],
            f"Clicking {target_id}",
            step,
        )

    @tool
    async def enter_text(step: int, target_id: int, value: str, thought: str) -> str:
        """Type into a text field. `target_id` must be an id from the current scene."""
        return await _run(
            [JsonRpcAction(id=1, method="enter_text", params=[target_id, value])],
            f"Typing into {target_id}",
            step,
        )

    @tool
    async def press_key(step: int, key_code: str, duration_seconds: float, thought: str) -> str:
        """Press a key — no target needed, so this works on a screen with nothing
        clickable, such as a dialogue or cutscene that advances on any key.

        `key_code` is a Unity KeyCode name, e.g. "Space", "Return", "Escape".
        `duration_seconds` must be greater than zero.
        """
        return await _run(
            [JsonRpcAction(id=1, method="key_click", params=[key_code, duration_seconds])],
            f"Pressing {key_code}",
            step,
        )

    @tool
    async def move_pointer(step: int, x: float, y: float, thought: str) -> str:
        """Move the pointer to a point on the screen, without pressing anything.

        `x` and `y` are screen pixels, taken from the scene exactly as printed:
        an element's `@ x,y` is its centre, and it belongs here unchanged — no
        conversion of any kind. Use this to hover, or to put the pointer
        somewhere a target id cannot address — a map, a canvas, an inventory slot.
        """
        return await _run(
            [JsonRpcAction(id=1, method="move_mouse", params=[x, y])],
            f"Moving the pointer to ({x}, {y})",
            step,
        )

    @tool
    async def click_at(step: int, x: float, y: float, thought: str, button: int = 0) -> str:
        """Click a point on the screen, for something the scene gives no id for.

        Coordinates are screen pixels, taken from the scene unchanged, as with
        `move_pointer`. `button` is 0 for left, 1 for right, 2 for middle.

        Prefer this over pressing and releasing yourself: the move, the press and
        the release go to the game as ONE batch, which the game runs strictly in
        order, so the click cannot be interrupted or left with the button down.

        `click_button` is the one to use when the scene DOES give an id — it
        presses what the game wired the button to, rather than a point that may
        be covered by something else.
        """
        return await _run(
            [
                # 누르기는 좌표를 안 받는다. 포인터가 있는 자리에 떨어지므로 먼저 옮긴다 —
                # `drag_pointer` 가 같은 이유로 같은 순서를 쓴다.
                JsonRpcAction(id=1, method="move_mouse", params=[x, y]),
                JsonRpcAction(id=2, method="mouse_down", params=[button]),
                JsonRpcAction(id=3, method="mouse_up", params=[button]),
            ],
            f"Clicking at ({x}, {y})",
            step,
        )

    @tool
    async def double_click_at(
        step: int, x: float, y: float, thought: str, button: int = 0
    ) -> str:
        """Double-click a point, for something that only a double-click does.

        Coordinates and `button` are as in `click_at`. Both presses ride ONE
        batch, which the game runs strictly in order, so nothing lands between
        them — two separate `click_at` calls are two turns apart and the game
        reads them as two single clicks.

        Use `click_at` twice when the game wants two clicks. This one is for the
        gesture a game treats as its own: opening an item, equipping from a list.
        """
        return await _run(
            [
                # 누르기는 좌표를 안 받는다. 포인터가 있는 자리에 떨어지므로 먼저 옮긴다.
                JsonRpcAction(id=1, method="move_mouse", params=[x, y]),
                JsonRpcAction(id=2, method="mouse_down", params=[button]),
                JsonRpcAction(id=3, method="mouse_up", params=[button]),
                JsonRpcAction(id=4, method="mouse_down", params=[button]),
                JsonRpcAction(id=5, method="mouse_up", params=[button]),
            ],
            f"Double-clicking at ({x}, {y})",
            step,
        )

    @tool
    async def hold_mouse_button(step: int, thought: str, button: int = 0) -> str:
        """Press a mouse button and keep it down, at wherever the pointer now is.

        `button` is 0 for left, 1 for right, 2 for middle. The press happens at
        the current pointer position — move there first with `move_pointer`.

        This is for input the game reads as HELD — charging, a long press,
        anything behind `Input.GetMouseButton`. For a plain click use `click_at`,
        and for a plain drag `drag_pointer`: both ride one batch and cannot be
        left half-done.

        Nothing releases this for you. Call `release_mouse_button` before you
        judge the step, or every later step runs with the button still down.
        """
        return await _run(
            [JsonRpcAction(id=1, method="mouse_down", params=[button])],
            f"Holding mouse button {button}",
            step,
        )

    @tool
    async def release_mouse_button(step: int, thought: str, button: int = 0) -> str:
        """Let go of a mouse button held by `hold_mouse_button`.

        `button` must be the one you pressed: 0 for left, 1 for right, 2 for
        middle. The release lands at wherever the pointer now is, which is what
        decides where a drag drops.
        """
        return await _run(
            [JsonRpcAction(id=1, method="mouse_up", params=[button])],
            f"Releasing mouse button {button}",
            step,
        )

    @tool
    async def hold_key(step: int, key_code: str, thought: str) -> str:
        """Press a key and keep it down until you release it.

        `key_code` is a Unity KeyCode name, e.g. "W", "LeftShift", "Space". Use
        this for movement and modifiers — anything the game reads as "is it held
        right now" rather than "was it pressed". `press_key` is the one-shot.

        Nothing releases this for you. Call `release_key` before you judge the
        step, or the game keeps seeing the key down for the rest of the run.
        """
        return await _run(
            [JsonRpcAction(id=1, method="key_down", params=[key_code])],
            f"Holding {key_code}",
            step,
        )

    @tool
    async def set_input_axis(step: int, axis_name: str, value: float, thought: str) -> str:
        """Drive a named input axis, for a game that reads axes rather than keys.

        `axis_name` is a Unity Input Manager axis and is CASE SENSITIVE —
        "Horizontal", "Vertical", "Jump" are the stock ones. `value` runs from
        -1 to 1: 1 and -1 are the two directions, 0 is centred. A value outside
        that range is refused, and so is an axis the game has not set up, so a
        misspelled name comes back as an error rather than as silence.

        Use this when `hold_key` does nothing. A game that reads
        `Input.GetAxis("Horizontal")` cannot see a held key at all: the key tool
        reports success and the character does not move.

        Nothing centres this for you. Call it again with 0 before you judge the
        step, or every step after it runs with the axis pushed over.
        """
        return await _run(
            [JsonRpcAction(id=1, method="set_axis", params=[axis_name, value])],
            f"Setting axis {axis_name} to {value}",
            step,
        )

    @tool
    async def set_input_button(step: int, axis_name: str, pressed: bool, thought: str) -> str:
        """Hold or release a named input button, for a game that reads buttons by name.

        In Unity a button IS an axis: "Jump" is an axis entry whose positive side
        is a key, and the game may read it with `GetButton("Jump")` instead of
        checking the key itself. `axis_name` is that name, CASE SENSITIVE, and an
        axis the game has not set up comes back as an error.

        `pressed=True` holds it, `pressed=False` lets go. Release is what reports
        the button coming up, so a game watching for that edge needs the second
        call and not a value of 0.

        Nothing releases this for you. Call it with `pressed=False` before you
        judge the step, or the game keeps seeing the button down for the rest of
        the run.
        """
        return await _run(
            [JsonRpcAction(id=1, method="set_button", params=[axis_name, pressed])],
            f"{'Holding' if pressed else 'Releasing'} button {axis_name}",
            step,
        )

    @tool
    async def release_key(step: int, key_code: str, thought: str) -> str:
        """Let go of a key held by `hold_key`. `key_code` must be the same one."""
        return await _run(
            [JsonRpcAction(id=1, method="key_up", params=[key_code])],
            f"Releasing {key_code}",
            step,
        )

    @tool
    async def drag_pointer(
        step: int,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        thought: str,
        button: int = 0,
    ) -> str:
        """Drag from one point on the screen to another and drop there.

        Coordinates are screen pixels, taken from the scene unchanged, as with
        `move_pointer`. `button` is 0 for left, 1 for right, 2 for middle.

        Prefer this over pressing and releasing yourself: the press, the move and
        the release go to the game as ONE batch, which the game runs strictly in
        order, so the drag cannot be interrupted or left with the button down.
        """
        return await _run(
            [
                # The press takes no coordinates — it lands wherever the pointer
                # already is, so the drag has to start by moving there.
                JsonRpcAction(id=1, method="move_mouse", params=[from_x, from_y]),
                JsonRpcAction(id=2, method="mouse_down", params=[button]),
                JsonRpcAction(id=3, method="move_mouse", params=[to_x, to_y]),
                JsonRpcAction(id=4, method="mouse_up", params=[button]),
            ],
            f"Dragging from ({from_x}, {from_y}) to ({to_x}, {to_y})",
            step,
        )

    @tool
    async def pause_game_time(step: int, thought: str) -> str:
        """Freeze game time, so the screen stops changing while you read it.

        Use this when the thing you have to judge does not stay still — a hit
        effect, a countdown, a toast that disappears, a cutscene that plays past
        the moment the step is about. Clicking, typing and observing all keep
        working while time is frozen, because they do not run on game time.

        Nothing unfreezes this for you. Call `resume_game_time` before you report
        the step, or every step after it runs against a stopped game.
        """
        return await _run(
            [JsonRpcAction(id=1, method="pause_time")],
            "Pausing game time",
            step,
        )

    @tool
    async def resume_game_time(step: int, thought: str) -> str:
        """Let game time run again, at the speed it had before the pause.

        Fails if the game was not paused by `pause_game_time` — the speed a game
        chose for itself is not yours to overwrite.
        """
        return await _run(
            [JsonRpcAction(id=1, method="resume_time")],
            "Resuming game time",
            step,
        )

    @tool
    async def reset_game(step: int, thought: str, clear_player_prefs: bool = False) -> str:
        """Put the game back to the state the run started in.

        For a step that needs a clean game and no path back to one — a tutorial
        that plays once a session, a level already cleared, a wrong branch taken
        three screens ago. Cheaper than asking the operator to restart, and it
        keeps the run alive.

        It reloads the game's first scene, so everything on screen now is gone,
        and so is whatever the game was keeping across scene loads: managers,
        score, inventory. A `pause_game_time` freeze and any held key or mouse
        button are released first, so the fresh game starts with nothing pressed.
        Every target id you have is dead afterwards; observe before you act again.

        `clear_player_prefs=True` also deletes the game's PlayerPrefs — the small
        key/value store a game keeps its "tutorial seen" flag, its difficulty and
        volume settings, and its high score in. The SDK's own entries are kept,
        so the run itself survives. Ask for it only when the thing standing in
        your way outlives a restart: an intro or tutorial the game plays once per
        install rather than once per session, a setting saved by an earlier run,
        a high score the step is judging. A gate that lasts only the session is
        already gone after a plain reset, and the flag buys you nothing there. Do
        not ask for it when the step's precondition is *having* progress — the
        wipe deletes the very thing that step needs.

        The wipe is irreversible. There is no restore, and every later step and
        every later scenario in this run inherits the emptied store.

        Even with the flag on, the game's own save files are untouched. A game
        that writes its progress to a file of its own comes back holding it, so a
        step that depends on a fresh save file still needs the operator. An
        emptied store is also not a promise that the game is in a first-run
        state: a manager destroyed by the reload can write its keys straight back
        in `OnDestroy`.

        A game built on an SDK older than this flag ignores it and resets scene
        state only, and this tool cannot tell — the reset reports success either
        way. So when a step depended on the wipe and the game still behaves as
        though the data is there, report the step on what you actually saw
        instead of resetting again; the retry does the same thing.
        """
        # 플래그가 꺼져 있으면 params를 아예 비운다. 기본 호출의 wire를 지금과 byte 단위로
        # 같게 두어야 이 파라미터를 모르는 옛 SDK가 아무 변화도 보지 않는다.
        params: list[Any] = [{"clearPlayerPrefs": True}] if clear_player_prefs else []
        return await _run(
            [JsonRpcAction(id=1, method="reset_game", params=params)],
            "Resetting the game",
            step,
        )

    @tool
    async def wait_for_operator(
        thought: str, timeout_seconds: float = 60.0, step: int | None = None
    ) -> str:
        """Stop and wait until the operator says something.

        For when you cannot go on without a person: the step is ambiguous, the
        game is in a state the scenario does not cover, or you asked them
        something with `reply_to_operator` and the answer decides what you do
        next. The run makes no progress while you are here, so ask the question
        first and only then wait for it.

        Returns what they said, or tells you nobody answered in time — silence is
        not a failure, and you decide what to do with it. But do not settle in on
        it: waiting is capped per call, and a couple of full waits is the whole
        run's clock, spent on nothing.
        """
        waited = bounded_operator_wait(timeout_seconds)
        messages = await channel.wait_for_operator(timeout_seconds)
        if not messages:
            return (
                f"The operator said nothing within {waited:g}s. Decide for "
                "yourself whether to wait again, carry on with what you have, or "
                "judge the step failed."
            )
        return _answer("The operator answered.", messages)

    @tool
    async def report_step(
        step: int,
        passed: bool,
        message: str,
        thought: str,
        used_knowledge_ids: list[str] = [],
    ) -> str:
        """Record the verdict for one scenario step, with the evidence for it.

        Call this once per step, right after you have observed the result of that
        step's action. For a step that verifies an expected result, `passed` is
        whether that result occurred (this is also its test case's verdict); for
        any other step, `passed` is whether you carried the action out. `message`
        should cite what you saw, and `thought` is how you reached the verdict.

        `used_knowledge_ids` is for knowledge base entries that actually bore on
        THIS verdict — ones you read and then judged differently because of. Give
        the ids as they were printed to you, whether by a search or as a neighbour
        line. Leave it empty when the step was decided by what you could see;
        that is the ordinary case and nothing is lost by saying so.
        """
        # The empty list default is never mutated — the ids are read once, below.
        # It is spelled as a literal rather than as `None` because this is a tool
        # schema the model fills in: an optional array is something it can simply
        # omit, while a nullable one invites it to send `null` and then wonder
        # whether that meant "none" or "unknown".
        # `knows_of`, NOT `knowledge_seen`. An entry shown only as a one-line
        # neighbour can still be what a verdict rested on, and citing it destroys
        # nothing. `knowledge_seen` is the bar for `update_knowledge` and
        # `forget_knowledge` because those DO destroy something, and a 120-character
        # line is not having read the entry — that boundary is deliberate and this
        # tool sits on the other side of it.
        #
        # Duplicates are folded first: citing one entry twice is one citation, and
        # counting it twice would make "how much knowledge this verdict used" a
        # function of how the model happened to phrase the list.
        cited: list[str] = []
        rejected: list[str] = []
        for entry in dict.fromkeys(used_knowledge_ids):
            (cited if state.knows_of(entry) else rejected).append(entry)
        # 이 스텝이 어느 TC에 속하고 그 구간의 검증 스텝인지를 판정에 붙인다(2단 판정). step_meta가
        # 없으면(구식 호출자) 미상으로 둔다.
        case_id, is_verification = (
            state.step_meta[step - 1] if 0 <= step - 1 < len(state.step_meta) else (None, False)
        )
        state.step_results.append(
            QaStepResult(
                step=step,
                passed=passed,
                message=message,
                case_id=case_id,
                is_verification=is_verification,
            )
        )
        # The verdict frame stays a PER-STEP one: `result` is left null, so
        # Orchestration's routeStatus logs it and the run goes on. Citations ride
        # along here rather than in a frame of their own precisely so they cannot
        # change that — a second frame type would be a second thing to get wrong
        # about ending the run.
        await channel.emit(
            MessageType.STATUS,
            StatusPayload(
                status=StepStatus.COMPLETED if passed else StepStatus.FAILED,
                step=step,
                case_id=case_id,
                is_verification=is_verification,
                message=message,
                used_knowledge_ids=cited,
                rejected_knowledge_id_count=len(rejected),
            ),
        )
        remaining = state.total_steps - len(state.step_results)
        # Said out loud, not dropped. The verdict itself is already recorded, so
        # this is not a refusal — but an agent told nothing would carry on
        # believing the entry was credited, and the ids it invents are exactly
        # what nobody would otherwise notice.
        note = (
            ""
            if not rejected
            else (
                f"\n\n{len(rejected)} of the ids you cited are not entries this run "
                f"has been shown, so they were not recorded: {rejected}. The verdict "
                "stands. Cite only ids printed to you by a search or a neighbour line."
            )
        )
        if remaining <= 0:
            # 무엇을 남길지 묻는 자리이자 이유는 `render_closing_asks` 가 들고 있다. 여기서
            # 말하는 것은 그 자리가 여기라는 것뿐이다 — 매 스텝마다 붙이면 표가 뜻을 잃고,
            # `finish_run` 은 이미 닫는 쪽으로 기운 뒤다.
            return _answer(
                "Recorded. This step report does not close the run. That was the "
                "last step — when you are done with the follow-up work below, "
                f"call `finish_run` yourself:{render_closing_asks(state)}{note}",
                channel.drain_operator_messages(),
            )
        # The verdict is recorded either way; what differs is the pull to keep
        # going. A failure is where the loop is most tempted to call it a day, so
        # that is where the next move has to be spelled out rather than implied.
        body = f"Recorded. {remaining} step(s) left — continue with step {step + 1}."
        if not passed:
            body = f"{body} A failed step is not a reason to stop."
        return _answer(f"{body}{note}", channel.drain_operator_messages())

    @tool(
        description=REPORT_ISSUE_DESCRIPTION.format(
            severities="/".join(s.value for s in IssueSeverity),
            limit=arch.max_issues_per_run,
        )
    )
    async def report_issue(
        step: int,
        severity: str,
        title: str,
        expected: str,
        actual: str,
        reproduction: list[str],
        thought: str,
    ) -> str:
        if state.issues_attempted >= arch.max_issues_per_run:
            return (
                f"You have filed all {arch.max_issues_per_run} issues this run "
                "allows. Nothing was sent. Carry the remaining findings in the "
                "run summary instead."
            )
        # Both required fields are checked here rather than left to Orchestration,
        # and for the same reason: a frame with a blank title or an unknown
        # severity is dropped there without a reply, so an agent that got either
        # wrong would go on believing it had reported the defect.
        if not title.strip():
            return (
                "An issue needs a title — one line naming the defect — so nothing "
                "was filed. Call this again with one."
            )
        try:
            checked = IssueSeverity(severity.strip().upper())
        except ValueError:
            allowed = "/".join(s.value for s in IssueSeverity)
            return (
                f"'{severity}' is not a severity, so nothing was filed. Call this "
                f"again with one of {allowed}."
            )
        state.issues_attempted += 1
        await channel.emit(
            MessageType.ISSUE,
            IssuePayload(
                title=title,
                severity=checked,
                step=step,
                expected=expected,
                actual=actual,
                reproduction=reproduction,
            ),
        )
        remaining = arch.max_issues_per_run - state.issues_attempted
        return _answer(
            f"Filed as {checked.value}. {remaining} issue(s) left this run.",
            channel.drain_operator_messages(),
        )

    @tool
    async def finish_run(passed: bool, summary: str, thought: str) -> str:
        """End the run. Call this once, after the last step has been reported.

        A step with no verdict yet is worth attempting before you close: the run
        was opened to find out about all of them. Calling this with steps still
        unreported sends you back to them once.

        `thought` is how you reached the overall verdict; it goes on the timeline.
        """
        state.finish_attempts += 1

        # A step the agent never attempted is the failure this whole change is
        # about, so closing over one costs a round trip. Only the first, though:
        # the second call closes whatever the state, because a run the game has
        # abandoned still has to be able to end.
        unreported = state.unreported_steps()
        if unreported and state.finish_attempts == 1:
            listed = ", ".join(str(step) for step in unreported)
            return (
                f"{len(unreported)} step(s) still have no verdict: {listed}. Go "
                "attempt them — a step you have not tried may still pass. If the "
                "game truly cannot go on, report them failed with the reason, then "
                "call `finish_run` again."
            )

        state.finished = True
        await channel.emit(
            MessageType.STATUS,
            StatusPayload(
                status=StepStatus.COMPLETED,
                result=RunResult.PASSED if passed else RunResult.FAILED,
                message=summary,
                summary=state.build_summary(),
            ),
        )
        return "The run is closed."

    @tool
    async def reply_to_operator(message: str, thought: str, step: int | None = None) -> str:
        """Answer the operator. Use when they asked something, not for progress.

        `thought` is why you are answering this way; it goes on the timeline so a
        reviewer can see the reasoning behind what the operator was told.
        """
        await channel.say(message, step)
        return _answer("Sent.", channel.drain_operator_messages())

    # Each name below is the decorated tool, not the function: `@tool` takes the
    # name off the function itself, so a tool can no longer end up filed under a
    # name string that drifted away from what it is called here.
    tools: list[BaseTool] = [
        observe_scene,
        inspect_object,
        search_knowledge,
        record_knowledge,
        update_knowledge,
        forget_knowledge,
        link_knowledge,
        unlink_knowledge,
        expand_knowledge,
        include_screen_selector,
        exclude_screen_selector,
        list_scene_capabilities,
        record_capability_verdict,
        record_new_capability,
        click_button,
        enter_text,
        press_key,
        move_pointer,
        click_at,
        double_click_at,
        hold_mouse_button,
        release_mouse_button,
        hold_key,
        release_key,
        set_input_axis,
        set_input_button,
        drag_pointer,
        pause_game_time,
        resume_game_time,
        reset_game,
        wait_for_operator,
        report_step,
        report_issue,
        finish_run,
        reply_to_operator,
    ]

    # A run without vision is not offered the tool at all. Left in, it would be
    # called, cost a game round trip, and produce an image nothing can look at —
    # and the agent would have no way to know why looking did not help. This is
    # also why the tool set is part of the arch fingerprint: a run with the tool
    # and a run without it are two different agents, not one agent configured.
    if arch.vision:
        tools.append(capture_screen)
    return tools
