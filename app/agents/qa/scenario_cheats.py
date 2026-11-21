"""시나리오 단계에 적힌 게임 기능을 workflow 에서도 엔진이 부른다.

`planner_executer` 와 같은 규칙이다(`app/qa/scenario_cheats.py`): 판정이 없는 단계(`case_id` 없음)에
"게임 기능 `set_progress(resumeStage=1)` 로 …" 처럼 적힌 호출은 모델이 고르지 않고 엔진이 그대로 보낸다.
같은 시나리오가 두 엔진에서 같은 상태로 시작해야, 두 엔진의 결과를 비교할 수 있다.

workflow 는 단계를 하나씩 넘기는 루프가 없다. 대신 agent 가 부르는 tool 마다 지금 하고 있는 `step` 을
넘긴다(`start_test_case` 는 그 test case 의 첫 단계). 그 `step` 이 처음으로 어떤 단계에 닿으면, 그 단계까지
아직 보내지 않은 호출을 단계 순서대로 먼저 보내고 tool 을 실행한다. 단계 글이 "호출 뒤 조작" 순서이므로
호출이 먼저다.

엔진이 보낸 호출을 agent 가 같은 단계에서 다시 부르면 보내지 않는다. 같은 치트를 두 번 보내면 상태가
두 번 바뀐다(`give_item` 이 두 개를 준다).
"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from app.agents.qa.tools.state import QaRunState
from app.qa.scenario_cheats import ScenarioCheat, cheats_in

# 엔진이 보낸 호출을 agent 가 다시 부를 때 가로채는 tool. 둘 다 `name`·`arguments` 를 받는다.
CHEAT_TOOLS = frozenset({"run_cheat", "call_game_function"})

RunCheat = Callable[[int, str, str, dict[str, Any], str | None], Awaitable[str]]


def scenario_cheat_steps(state: QaRunState) -> dict[int, list[ScenarioCheat]]:
    """판정이 없는 단계 중 게임 기능 호출이 적힌 단계와 그 호출. 단계 번호는 1부터다."""
    steps: dict[int, list[ScenarioCheat]] = {}
    for step, text in enumerate(state.step_texts, start=1):
        if state.case_id_at(step) is not None:
            continue
        cheats = cheats_in(text)
        if cheats:
            steps[step] = cheats
    return steps


def step_of_call(state: QaRunState, name: str, args: dict[str, Any]) -> int | None:
    """tool 호출이 가리키는 단계. `step` 인자가 없으면 `start_test_case` 의 test case 첫 단계다."""
    step = args.get("step")
    if isinstance(step, int) and not isinstance(step, bool):
        return step
    if name == "start_test_case" and isinstance(args.get("case_id"), int):
        return state.step_of_case(args["case_id"])
    return None


class QaScenarioCheatsMiddleware(AgentMiddleware):
    """agent 가 단계에 닿으면 그 단계까지의 시나리오 게임 기능을 엔진이 먼저 보낸다."""

    def __init__(self, state: QaRunState, cheat_names: Callable[[], list[str]], run_cheat: RunCheat | None) -> None:
        super().__init__()
        self._state = state
        self._cheat_names = cheat_names
        self._run_cheat = run_cheat
        self._pending = scenario_cheat_steps(state)
        # 단계 번호 → 엔진이 보낸 (이름, 인자). 같은 단계에서 agent 가 다시 부르면 막는 데 쓴다.
        self._sent: dict[int, list[tuple[str, dict[str, Any]]]] = {}

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        args = call.get("args") or {}
        step = step_of_call(self._state, call["name"], args)
        notes = await self._send_through(step) if step is not None else []

        repeat = self._repeat_of_sent(call["name"], args, step)
        if repeat is not None:
            return ToolMessage(content="\n".join([*notes, repeat]), tool_call_id=call["id"], name=call["name"])

        result = await handler(request)
        if notes and isinstance(result, ToolMessage):
            result = _with_notes(result, notes)
        return result

    async def _send_through(self, step: int) -> list[str]:
        notes: list[str] = []
        for number in sorted(n for n in self._pending if n <= step):
            cheats = self._pending.pop(number)
            sent: list[tuple[str, dict[str, Any]]] = []
            lines: list[str] = []
            for cheat in cheats:
                ok, line = await self._send(number, cheat)
                lines.append(line)
                if ok:
                    sent.append((cheat.name, cheat.args))
                else:
                    # 하나가 안 되면 그 뒤 호출은 앞 호출이 만든 상태를 전제하므로 보내지 않는다. agent 가 이어서 한다.
                    break
            self._sent[number] = sent
            notes.append(
                f"Step {number}: the engine ran the game functions this scenario step names, before your call. "
                + " ".join(lines)
                + (" Do not call them again for this step." if sent else "")
            )
        return notes

    async def _send(self, step: int, cheat: ScenarioCheat) -> tuple[bool, str]:
        call = f"{cheat.name}({cheat.args})"
        if self._run_cheat is None:
            return False, f"not sent: {call} — this run has no cheats, so do this step yourself."
        if cheat.name not in self._cheat_names():
            return False, f"not sent: {call} — no cheat of this name in this build, so do this step yourself."
        before = len(self._state.used_cheats)
        answer = await self._run_cheat(step, cheat.name, "scenario step", dict(cheat.args), None)
        # 성공은 `run_cheat` 가 남긴 기록으로 판단한다. 답 글은 성공과 실패가 같은 모양으로 시작한다. 보내기 전에
        # 거절된 호출(상한, 허용 목록)은 기록이 없으니 실패다.
        sent = self._state.used_cheats[before:]
        ok = bool(sent) and sent[-1].success
        return ok, f"{'sent' if ok else 'failed'} {call} — {answer}"

    def _repeat_of_sent(self, name: str, args: dict[str, Any], step: int | None) -> str | None:
        if name not in CHEAT_TOOLS or step is None:
            return None
        if (args.get("name"), args.get("arguments") or {}) not in self._sent.get(step, []):
            return None
        return (
            f"The engine already ran `{args.get('name')}` with these arguments for step {step}. "
            "Nothing was sent again; go on with the rest of the step."
        )


def _with_notes(result: ToolMessage, notes: list[str]) -> ToolMessage:
    head = "\n".join(notes)
    if isinstance(result.content, str):
        return result.model_copy(update={"content": f"{head}\n\n{result.content}"})
    return result.model_copy(update={"content": [{"type": "text", "text": head}, *result.content]})


__all__ = ["CHEAT_TOOLS", "QaScenarioCheatsMiddleware", "scenario_cheat_steps", "step_of_call"]
