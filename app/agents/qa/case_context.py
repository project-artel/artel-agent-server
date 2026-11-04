"""실행 당시 TC 원문과 모델이 읽는 TC 진행 상태를 보존한다."""

import json
import re
from uuid import uuid4
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, HumanMessage

if TYPE_CHECKING:
    from app.agents.qa.tools.state import QaRunState
    from app.qa.channel import QaRunChannel

TC_DETAIL_BUDGET = 4_000
TC_LEDGER_ID = "qa-tc-ledger"
# 지난 장부를 접은 자리. 장부는 바뀔 때마다 새로 덧붙이고 지난 것은 지우지 않는다 — 대화 중간의 메시지를
# 지우거나 고치면 그 뒤가 전부 프롬프트 캐시에서 빠진다(런 60: 호출 81번 중 캐시를 읽은 것이 손에 꼽았다).
SUPERSEDED_LEDGER = "[Earlier TC ledger, superseded by the newest AUTHORITATIVE TC LEDGER further down.]"
# 지난 장부와 지난 TC 상세가 이만큼 쌓였을 때만 한 번에 접는다. 접을 때마다 그 뒤가 캐시에서 빠지므로,
# 매번 접지 않고 묶어서 접어 캐시가 깨지는 횟수를 줄인다.
CONTEXT_FOLD_BATCH = 4
DETAIL_START = "<<test case details "
DETAIL_END = "<<end test case details>>"
DETAIL_PATTERN = re.compile(re.escape(DETAIL_START) + r"(?P<id>\d+|plan)>>\n.*?" + re.escape(DETAIL_END), re.DOTALL)


def is_ledger(message: BaseMessage) -> bool:
    """TC 장부 메시지인가. 장부마다 id 가 달라서(덧붙이므로) 앞머리로 가린다."""
    return bool(message.id) and message.id.startswith(TC_LEDGER_ID)


def case_ids(state: "QaRunState") -> list[int]:
    return list(dict.fromkeys(case_id for case_id, _ in state.step_meta if case_id is not None))


def detail_block(case_id: int | str, content: str) -> str:
    return f"{DETAIL_START}{case_id}>>\n{content}\n{DETAIL_END}"


def render_case(case_id: int, state: "QaRunState", channel: "QaRunChannel") -> str:
    if case_id not in case_ids(state):
        return f"TC {case_id} is not part of this scenario. Use an ID from the TC ledger."
    snapshot = channel.evaluation.snapshots.get(case_id)
    if snapshot is None:
        return f"TC {case_id}: the run did not receive a specification snapshot. Do not invent its requirements."
    document = dict(snapshot)
    if review := state.case_reviews.get(case_id):
        document["human_review"] = review
    text = detail_block(case_id, json.dumps(document, ensure_ascii=False, indent=2))
    if review:
        text += "\nHuman review is a warning about earlier judgement, not the answer. Judge from this run's observations."
    return text


def render_case_ledger(state: "QaRunState", channel: "QaRunChannel") -> str:
    ids = case_ids(state)
    latest = {result.case_id: result.verdict for result in state.case_attempt_results}
    open_ids = [case_id for case_id in ids if case_id in channel.evaluation.attempts]
    current = state.current_case_id if state.current_case_id in open_ids else None
    rows = []
    for case_id in ids:
        snapshot = channel.evaluation.snapshots.get(case_id, {})
        title = (snapshot.get("test_step") or "").splitlines()
        rows.append({"case_id": case_id, "title": title[0][:160] if title else "",
                     "scene": snapshot.get("scene"), "latest_verdict": latest.get(case_id)})
    document = {
        "all_case_ids": ids,
        "without_report_ids": [case_id for case_id in ids if case_id not in latest],
        "reported_ids": [case_id for case_id in ids if case_id in latest],
        "inconclusive_ids": [case_id for case_id in ids if latest.get(case_id) == "INCONCLUSIVE"],
        "open_attempt_ids": open_ids,
        "current_case_id": current,
        "test_cases": rows,
    }
    text = (
        "AUTHORITATIVE TC LEDGER — generated from run data, never summarized. "
        "This replaces any TC list or progress claimed by the conversation summary. "
        "Reported means a verdict was recorded, not that the TC passed. "
        "Use get_test_case(case_id) to read the original run snapshot; start_test_case also returns it. "
        "Do not ask the operator to repeat a specification available through this tool.\n"
        + json.dumps(document, ensure_ascii=False, indent=2)
    )
    if current is not None:
        text += "\nCurrent TC specification (original run snapshot):\n" + render_case(current, state, channel)
    return text


def fold_case_details(messages: list[BaseMessage], current_case_id: int | None = None, *, for_summary: bool = False) -> list[BaseMessage]:
    """오래된 상세만 바꾸고 원래 message와 snapshot은 변경하지 않는다."""
    latest = None
    if not for_summary:
        for index, message in enumerate(messages):
            if is_ledger(message) or not isinstance(message.content, str):
                continue
            for match in DETAIL_PATTERN.finditer(message.content):
                if match.group("id") != "plan":
                    latest = (index, match.start())
    result = []
    for index, message in enumerate(messages):
        if is_ledger(message):
            if not for_summary:
                result.append(message)
            continue
        if not isinstance(message.content, str):
            result.append(message)
            continue

        def replace(match):
            identifier = match.group("id")
            # 작은 초기 계획은 일반 호출에서는 유지하고 summary 입력에서만 제외한다.
            keep = not for_summary and (
                identifier == "plan" or identifier == str(current_case_id) or latest == (index, match.start())
            )
            if keep:
                return match.group(0)
            return f"[TC {identifier} details folded; use get_test_case(case_id) for the original snapshot.]"

        content = DETAIL_PATTERN.sub(replace, message.content)
        result.append(message if content == message.content else message.model_copy(update={"content": content}))
    return result


class QaCaseContextMiddleware(AgentMiddleware):
    """상태가 바뀔 때만 새 ledger 를 덧붙여 graph 에 저장한다.

    **지난 ledger 를 지우지 않는다.** 예전에는 지난 것을 지우고 끝에 새로 붙였는데, 대화 중간의 메시지가
    사라지면 그 뒤가 전부 프롬프트 캐시에서 빠지고 thinking block 도 무효가 된다. 지난 ledger 와 지난 TC
    상세는 [CONTEXT_FOLD_BATCH] 개가 쌓였을 때 한 번에 접는다.
    """

    def __init__(self, state: "QaRunState", channel: "QaRunChannel") -> None:
        self.state = state
        self.channel = channel

    async def abefore_model(self, state, runtime):
        messages = state["messages"]
        content = render_case_ledger(self.state, self.channel)
        ledgers = [message for message in messages if is_ledger(message)]
        appended = None
        if not ledgers or ledgers[-1].content != content:
            appended = HumanMessage(id=f"{TC_LEDGER_ID}-{uuid4().hex[:12]}", content=content)
        # 새 것을 붙이면 지금 가장 새 ledger 도 지난 것이 된다.
        superseded = ledgers if appended is not None else ledgers[:-1]
        stale_ledgers = [message for message in superseded if message.content != SUPERSEDED_LEDGER]
        folded = fold_case_details(messages, self.state.current_case_id)
        stale_details = sum(
            len(DETAIL_PATTERN.findall(old.content)) - len(DETAIL_PATTERN.findall(new.content))
            for old, new in zip(messages, folded)
            if new is not old and isinstance(old.content, str)
        )
        changes: list[BaseMessage] = []
        if len(stale_ledgers) + stale_details >= CONTEXT_FOLD_BATCH:
            changes += [new for old, new in zip(messages, folded) if new is not old]
            changes += [message.model_copy(update={"content": SUPERSEDED_LEDGER}) for message in stale_ledgers]
        if appended is not None:
            changes.append(appended)
        return {"messages": changes} if changes else None
