"""한 QA 런이 도구 호출 사이에 들고 가는 상태.

각 도구를 몇 번 썼는지, 어떤 지식을 이미 스쳐 봤는지, 아직 보고되지 않은 스텝이
무엇인지가 여기 모인다. 도구가 각자 세지 않고 이 객체에 묻는 이유는, 세는 자리가
도구마다 흩어지면 도구가 늘 때마다 하나씩 빠지기 때문이다.
"""

from dataclasses import dataclass
from typing import Any

from app.qa.envelope import JsonRpcAction
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
