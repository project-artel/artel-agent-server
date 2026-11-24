"""The verdict: judges a step after play stops, with the run's model.

It reads the step (what to do and the result to verify), why play stopped, the turns played —
the screen's scene, `done()` and what Jev said about it, the action picked, how its macro ended
and what it sent — and the values the planner named in the step's ```check block, read from
`STATE_SET` when play started, at the start of every turn and when it stopped (`StepPlay.evidence`).
The play's own record and the game's values are the evidence, with one picture of the screen when
play stopped if one could be taken (knob `screen_images`). It
answers `Verdict` — whether it passed, why in a sentence, the values that decided it, and an
insight (from prompt v4, `GameVerdict`'s `game_insight`: what the game showed in the step, apart from
the verdict) — through `structured`, the repo's one place that picks the structured-output method per
route. An answer that cannot be read is asked for once more; a second one fails the step with
`EVALUATOR-ERROR`.

Sending the verdict is `send.py`'s job, so a caller can judge without sending — a probe
script replaying a saved play does exactly that.
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, field_validator

from app.agents.planner_executer.evidence import names_of, points_of, readings
from app.agents.planner_executer.executer import StepPlay, read_only_observations
from app.agents.planner_executer.target import evaluator_note
from app.agents.planner_executer.prompts import (
    EVALUATOR_ROLE,
    LANGUAGE_PLACEHOLDER,
    SCENE_EVALUATOR_ROLE,
    case_view,
    prompt_text,
    this_step,
)
from app.agents.qa.prompt import LANGUAGE_DIRECTIVES
from app.agents.scenario import OutputLanguage
from app.llm.chat_model import structured
from app.qa.channel import QaCancelled

if TYPE_CHECKING:
    from app.agents.planner_executer.screen_capture import ScreenImage
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

# Tries per step: the first answer, and one more when it cannot be read.
ATTEMPTS = 2
# Turns shown; a long step keeps its last ones, where it stopped.
MAX_TURNS_SHOWN = 60
# Turns shown per case when a scene's cases are judged together; the request holds every case's.
MAX_TURNS_SHOWN_PER_CASE = 20
# Cases per scene judgement. A plan may verify up to `free_batch_size` cases; each case brings its
# turns and its end screen, so they are asked about a few at a time.
CASES_PER_JUDGEMENT = 5

# 근거 값에는 이름 수·변화 수 상한을 두지 않는다(pe13). 80개 상한은 판독값 끝에 붙는 화면 글자부터 잘랐다 — 로컬 try 31
# step 8~10 묶음은 이름이 139개였고, evaluator 는 `HintText[0]` 이 켜진 것만 보고 그 글자("강조된 적을 클릭해…")는 못 봐
# step 9 를 근거 없음으로 떨어뜨렸다. 묶음 하나의 근거 전체가 40KB 남짓이다.
# A `flag(...)`'s observed values as one turn line shows them.
MAX_FLAG_CHARS = 300

_JSON = re.compile(r"\{.*\}", re.DOTALL)

# Sends the system and user text and returns the answer: a `Verdict`, a dict or a JSON string.
Judge = Callable[[str, str], Awaitable[Any]]


class Verdict(BaseModel):
    passed: bool
    # 판정 이유: 사람이 읽는 한두 문장.
    reason: str
    # 판정 상세 이유: 판정을 가른 값을 `STATE_SET` 기록 그대로 — 이름, 언제 무엇에서 무엇으로.
    state_detail: str
    # 이 step 에서 배운 것: 게임이나 계획, 근거에 대해 다음 step·런이 알아 둘 것. 없으면 빈 글이다.
    insight: str
    # 근거가 판정에 충분했나(묶음 3). 어떤 게임 값으로도 결과를 알 수 없어 실패로 둔 것이면 거짓이다 — 게임 결함과 관측 부족을
    # 가른다.
    evidence_sufficient: bool = True


class ItemCheck(BaseModel):
    """기대 항목 하나의 판정(prompt v8). 무엇을 기대했고, 무엇을 보았고, 무엇이 그것을 보였나."""

    expected: str
    observed: str
    met: Literal["yes", "no", "unknown"]
    # 항목이 플레이어가 보는 것(`screen`)을 말하는가, 게임 안의 값(`game`)을 말하는가.
    about: Literal["screen", "game"] = "game"
    # 판정의 근거가 된 것들. `turn` 은 행동의 끝(입력 결과, `wait_until` 의 성공·실패)이다. 하나만 고르게 했더니 켜진 카드와
    # 그 글자를 함께 본 항목이 `game_value` 하나로 적혀 화면 근거 없음으로 떨어졌다(로컬 try 32 step 7).
    shown_by: list[Literal["screen_text", "picture", "game_value", "scene", "turn", "none"]] = ["none"]

    @field_validator("shown_by", mode="before")
    @classmethod
    def _one_or_many(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


class GameVerdict(BaseModel):
    """prompt v4 부터의 답. `insight` 대신 `game_insight` 를 쓴다 — 어떤 값이 더 나은 근거였는지는 `reason` 에 들어가고, 이 칸은
    이 step 에서 게임이 보인 모습에 대한 평가다(통과했어도 남은 문제). v3 까지의 런은 `Verdict` 로 그대로 묻는다."""

    passed: bool
    reason: str
    state_detail: str
    game_insight: str = ""
    evidence_sufficient: bool = True


class CheckedVerdict(GameVerdict):
    """prompt v8 부터의 답: 기대 항목마다 `checks` 를 함께 낸다. 통과는 모델이 아니라 `settle_checks` 가 정한다."""

    checks: list[ItemCheck] = []
    # 이 step 에서 시작하는 TC 의 사전 조건이 step 의 행동 때 성립했나(pe16). 사전 조건이 없거나 기록으로 알 수 없으면 `None`.
    precondition_held: bool | None = None


def settle_checks(checks: list[ItemCheck]) -> tuple[list[ItemCheck], bool | None]:
    """항목 판정을 기록 규칙으로 고치고 통과 여부를 정한다. 항목이 없으면 `None` 이다(모델의 `passed` 를 쓴다).

    화면에 보이는 것을 말하는 항목은 게임 값으로 보인 것이 아니다. 값의 이름이 같은 것처럼 들려도 게임은 다른 것을 쥘 수 있다
    — 로컬 try 27·28 은 대사의 목표 문장(`streamingText`)이 바뀐 것을 "끝까지 표시됨" 으로 읽고, 화면 글자는 끝까지 부분
    문장이었는데 통과시켰다. 그런 항목은 `unknown` 으로 낮춘다. 통과는 모든 항목이 `yes` 일 때뿐이다.
    """
    if not checks:
        return checks, None
    settled = [
        check.model_copy(update={"met": "unknown"})
        if check.met == "yes" and check.about == "screen" and set(check.shown_by) <= {"game_value", "none"}
        else check
        for check in checks
    ]
    return settled, all(check.met == "yes" for check in settled)


@dataclass(frozen=True)
class StepVerdict:
    """`Evaluator.judge` 의 답. `message` 는 `reason` 에 멈춤 이유를 붙인 것이다."""

    passed: bool
    message: str
    state_detail: str = ""
    insight: str = ""
    evidence_sufficient: bool = True
    # evaluator 가 본 화면 한 장의 capture id. 그림 없이 판정했으면 `None` 이다.
    screen_capture_id: str | None = None
    # 이 step 에서 게임이 보인 모습에 대한 평가(prompt v4 부터). 옛 `insight` 와 섞지 않는다.
    game_insight: str = ""
    # 기대 항목별 판정(prompt v8). 근거가 없는 항목(`unknown`)은 `missing` 에 따로 둔다 — runner 가 판정 step 을 더 돌게 한다.
    checks: tuple[dict[str, Any], ...] = ()
    missing: tuple[str, ...] = ()
    contradicted: tuple[str, ...] = ()
    # 사전 조건이 깨진 채 진행됐다(pe16). 통과가 아니지만 게임 결함도 아니다 — 판정을 실패와 따로 읽게 한다.
    precondition_unmet: bool = False

    def details(self) -> dict[str, Any]:
        """step `STATUS` 의 `verdict`."""
        details: dict[str, Any] = {
            "state_detail": self.state_detail, "insight": self.insight, "evidence_sufficient": self.evidence_sufficient,
        }
        if self.screen_capture_id is not None:
            details["screen_capture_id"] = self.screen_capture_id
        if self.game_insight:
            details["game_insight"] = self.game_insight
        if self.checks:
            details["checks"] = list(self.checks)
        if self.precondition_unmet:
            details["precondition"] = "unmet"
        return details


def stop_note(stopped: str, language: OutputLanguage) -> str:
    """판정 이유 끝에 붙는 멈춤 사유. 이유가 run 의 언어로 쓰이므로 머리말도 맞춘다(PR #257 에서 가져옴). 사유 자체는
    Executer 의 진단이라 그대로 둔다."""
    if stopped == "done":
        return ""
    note = STOP_NOTES.get(stopped, {}).get(language.value if language == OutputLanguage.ko else "en", stopped)
    return f" (executer 중단: {note})" if language == OutputLanguage.ko else f" (executer stopped: {note})"


def precondition_head(language: OutputLanguage) -> str:
    """사전 조건 불충족 판정의 이유 앞에 붙는 말. 실패와 섞이지 않게 사람이 먼저 읽는 자리에 둔다."""
    return "사전 조건 불충족: " if language == OutputLanguage.ko else "Precondition not met: "


def overrule_note(said: bool, passed: bool, checks: list[ItemCheck], language: OutputLanguage) -> str:
    """모델은 통과라 했는데 항목 판정이 통과를 막았을 때 이유 끝에 붙는 글. 판정과 이유가 어긋나 보이지 않게 한다."""
    if said == passed or passed:
        return ""
    unmet = [check.expected for check in checks if check.met != "yes"]
    if language == OutputLanguage.ko:
        return " (근거로 확인되지 않은 기대 항목: " + "; ".join(unmet) + ")"
    return " (expected results not shown by the evidence: " + "; ".join(unmet) + ")"


# 코드가 정한 멈춤의 설명(pe11). 키는 executer 의 멈춤 이유다. 나머지 이유는 진단 문장 그대로 붙인다.
STOP_NOTES: dict[str, dict[str, str]] = {
    "later_done": {
        "en": "a later step's done condition became true, so this step ended there",
        "ko": "뒤 step 의 완료 조건이 참이 되어 여기서 끝남",
    },
    "result_in_earlier_step": {
        "en": "this step's result happened while an earlier step was playing; no input was sent in this step",
        "ko": "이 step 의 결과가 앞 step 을 하는 중에 났고, 이 step 에서는 입력을 보내지 않음",
    },
    "passed_in_earlier_step": {
        "en": "an earlier step went past this step to a later step's result; no input was sent in this step",
        "ko": "앞 step 이 이 step 을 지나 뒤 step 의 결과까지 감. 이 step 에서는 입력을 보내지 않음",
    },
    "done_at_start": {
        "en": "the done condition held from the start; no input was sent",
        "ko": "완료 조건이 시작부터 참이라 입력을 보내지 않음",
    },
}


class Evaluator:
    def __init__(self, config: RunConfig, judge: Judge | None = None) -> None:
        self._config = config
        # v4 부터 판정 이유를 run 의 언어로 쓴다. 그 전 버전은 placeholder 가 없어 이 값을 쓰지 않는다.
        self._system = prompt_text(
            EVALUATOR_ROLE, config.prompt_version, **{LANGUAGE_PLACEHOLDER: LANGUAGE_DIRECTIVES[config.language]}
        )
        self._judge = judge or self._ask_model
        # 답의 모양은 prompt 가 묻는 대로다. v4 부터 evaluator 글이 `game_insight` 를, v8 부터 `checks` 를 묻는다.
        self._schema: type[Verdict] | type[GameVerdict] = (
            CheckedVerdict if '"checks"' in self._system else GameVerdict if '"game_insight"' in self._system else Verdict
        )

    async def judge(
        self,
        scenario: QaScenario,
        step: int,
        play: StepPlay,
        earlier: list[str] | None = None,
        image: "ScreenImage | None" = None,
        segment: list[int] | None = None,
    ) -> StepVerdict:
        """The step's verdict. The message names the stop when it was not `done`. `earlier` is the run's verdicts
        before this step, one line each — a result an earlier step reached is told apart from one that never came.
        `segment` is the steps one plan played together when `play` is the whole group's record (pe9)."""
        user = request(scenario, step, play, earlier, segment)
        if image is not None:
            user += (
                f"\nA picture of the screen when step {step}'s own turns ended is attached.\n"
                if segment and len(segment) > 1
                else "\nA picture of the screen when play stopped is attached.\n"
            )
        self._image = image
        problem = ""
        for _ in range(ATTEMPTS):
            try:
                verdict = _read(await self._judge(self._system, user), self._schema)
            except QaCancelled:
                raise
            except Exception as error:  # noqa: BLE001 - an unreadable answer is asked again
                problem = f"{type(error).__name__}: {str(error)[:200]}"
                continue
            note = stop_note(play.stopped, self._config.language)
            checks, settled = settle_checks(list(getattr(verdict, "checks", []) or []))
            passed = verdict.passed if settled is None else settled
            unmet = getattr(verdict, "precondition_held", None) is False
            if unmet:
                # 사전 조건이 깨진 채 한 행동의 결과는 그 TC 의 판정이 아니다.
                passed = False
            missing = tuple(check.expected for check in checks if check.met == "unknown")
            overruled = "" if unmet else overrule_note(verdict.passed, passed, checks, self._config.language)
            head = precondition_head(self._config.language) if unmet else ""
            return StepVerdict(
                passed, head + verdict.reason + overruled + note, verdict.state_detail, getattr(verdict, "insight", ""),
                verdict.evidence_sufficient and not missing, image.capture_id if image is not None else None,
                game_insight=getattr(verdict, "game_insight", ""),
                checks=tuple(check.model_dump() for check in checks),
                missing=missing,
                contradicted=tuple(check.expected for check in checks if check.met == "no"),
                precondition_unmet=unmet,
            )
        return StepVerdict(False, f"EVALUATOR-ERROR: no readable verdict in {ATTEMPTS} answers ({problem})")

    async def _ask_model(self, system: str, user: str) -> Verdict | GameVerdict:
        chain = structured(self._config.model, self._schema, self._config.reasoning)
        image = getattr(self, "_image", None)
        content = [{"type": "text", "text": user}, image.block()] if image is not None else user
        return await chain.ainvoke([SystemMessage(system), HumanMessage(content=content)])


class CaseVerdict(BaseModel):
    case_id: int
    passed: bool
    reason: str


class SceneVerdicts(BaseModel):
    verdicts: list[CaseVerdict]


class SceneEvaluator:
    """Judges every case played in one scene of a FREE scenario in one answer (ARTEL-1149)."""

    def __init__(self, config: RunConfig, judge: Judge | None = None) -> None:
        self._config = config
        self._system: str | None = None
        self._judge = judge or self._ask_model

    async def judge(
        self, scenario: QaScenario, played: list[tuple[int, StepPlay]], first_screen: str
    ) -> dict[int, tuple[bool, str]]:
        """`{step: (passed, message)}` for each played case, asked `CASES_PER_JUDGEMENT` at a time in
        play order. Each group starts from the screen the case before it left."""
        if self._system is None:
            self._system = prompt_text(SCENE_EVALUATOR_ROLE, self._config.prompt_version)
        results: dict[int, tuple[bool, str]] = {}
        start = first_screen
        for at in range(0, len(played), CASES_PER_JUDGEMENT):
            group = played[at : at + CASES_PER_JUDGEMENT]
            results.update(await self._judge_group(scenario, group, start))
            start = group[-1][1].end_screen
        return results

    async def _judge_group(
        self, scenario: QaScenario, played: list[tuple[int, StepPlay]], first_screen: str
    ) -> dict[int, tuple[bool, str]]:
        """One question for a few cases. A case the answers leave out is asked for once more with
        the rest; one still missing fails with `EVALUATOR-ERROR`."""
        by_case = {scenario.steps[step - 1].case_id: (step, play) for step, play in played}
        results: dict[int, tuple[bool, str]] = {}
        problem = ""
        for _ in range(ATTEMPTS):
            waiting = [(step, play) for step, play in played if step not in results]
            if not waiting:
                break
            try:
                verdicts = _read_scene(await self._judge(self._system, scene_request(scenario, waiting, first_screen)))
            except QaCancelled:
                raise
            except Exception as error:  # noqa: BLE001 - an unreadable answer is asked again
                problem = f"{type(error).__name__}: {str(error)[:200]}"
                continue
            for verdict in verdicts.verdicts:
                if verdict.case_id not in by_case:
                    continue
                step, play = by_case[verdict.case_id]
                note = stop_note(play.stopped, self._config.language)
                results[step] = (verdict.passed, verdict.reason + note)
            problem = "the answer left cases out"
        for step, _ in played:
            if step not in results:
                results[step] = (False, f"EVALUATOR-ERROR: no readable verdict for the case in {ATTEMPTS} answers ({problem})")
        return results

    async def _ask_model(self, system: str, user: str) -> SceneVerdicts:
        chain = structured(self._config.model, SceneVerdicts, self._config.reasoning)
        return await chain.ainvoke([SystemMessage(system), HumanMessage(user)])


def scene_request(scenario: QaScenario, played: list[tuple[int, StepPlay]], first_screen: str) -> str:
    """What the scene evaluator reads: the screen before the first case, then each case's play."""
    parts = [f"Screen before the first case began:\n{first_screen}\n"]
    for step, play in played:
        shown = play.turns[-MAX_TURNS_SHOWN_PER_CASE:]
        skipped = len(play.turns) - len(shown)
        turns = "\n".join(turn_line(turn) for turn in shown) or "  (no turn was played)"
        parts.append(
            f"Case:\n{json.dumps(case_view(scenario, step), ensure_ascii=False, indent=2)}\n\n"
            f"Why play stopped: {play.stopped}\n\n"
            f"Turns played ({len(play.turns)}"
            + (f", the first {skipped} left out" if skipped else "")
            + f"):\n{turns}\n\n"
            f"Screen when play stopped:\n{play.end_screen}\n"
        )
    return "\n---\n\n".join(parts)


def read_json(answer: Any) -> dict:
    """The JSON object in what the model returned — a dict as it is, a string by its first `{…}`."""
    if isinstance(answer, dict):
        return answer
    match = _JSON.search(str(answer))
    if match is None:
        raise ValueError(f"no JSON object in {str(answer)[:120]!r}")
    return json.loads(match.group(0))


def _read_scene(answer: Any) -> SceneVerdicts:
    """`SceneVerdicts` from what the model returned. Raises when it is not one."""
    if isinstance(answer, SceneVerdicts):
        return answer
    return SceneVerdicts.model_validate(read_json(answer))


def request(
    scenario: QaScenario, step: int, play: StepPlay, earlier: list[str] | None = None, segment: list[int] | None = None
) -> str:
    """What the evaluator reads about one step. `segment` is the steps one plan played together (pe9); the record is then
    the whole group's, and the note says why the step's own turns are not where its result has to be."""
    shown = play.turns[-MAX_TURNS_SHOWN:]
    skipped = len(play.turns) - len(shown)
    turns = "\n".join(turn_line(turn) for turn in shown) or "  (no turn was played)"
    history = (
        "Earlier steps of this run and their verdicts:\n" + "\n".join(f"  {line}" for line in earlier) + "\n\n"
        if earlier
        else ""
    )
    return (
        history
        + f"The step:\n{this_step(scenario, step)}\n\n"
        + precondition_note(getattr(play, "precondition", None))
        + evaluator_note(getattr(play, "target", None))
        + segment_note(step, segment)
        + f"Why play stopped: {play.stopped}\n\n"
        f"Turns played ({len(play.turns)}"
        + (f", the first {skipped} left out" if skipped else "")
        + f"):\n{turns}\n\n"
        + ran_code(play, shown)
        + f"{evidence_text(play)}\n"
    )


def precondition_note(checked: dict | None) -> str:
    """case 시작 때 사전 조건을 화면과 대조한 결과(pe16). 대조하지 않았으면 빈 글이다."""
    if not checked:
        return ""
    status = checked.get("status")
    said = {
        "held": "it held",
        "unmet": "it did not hold",
        "unknown": "the screen could not tell either way",
    }.get(status, "it was not checked")
    rewritten = (
        " The plan was then written again to make it hold before the step's own move; judge from the turns whether it did."
        if checked.get("rewritten") else ""
    )
    return (
        f"The test case's precondition, checked against the screen when this case started: \"{checked.get('condition')}\" — "
        f"{said}.{rewritten}\n\n"
    )


def ran_code(play: StepPlay, turns: list[dict]) -> str:
    """planner·editor 가 쓴 행동 중 위 turn 들에서 돈 것의 코드(pe13). 돈 것이 없으면 빈 글이다."""
    ran: list[str] = []
    for turn in turns:
        name = str(turn.get("picked") or "").split(" @ ")[0]
        if name in play.action_sources and name not in ran:
            ran.append(name)
    if not ran:
        return ""
    blocks = "\n\n".join(f"```python\n{play.action_sources[name]}\n```" for name in ran)
    return (
        "Code of the actions the plan wrote that ran above. An action that ended `passed` reached the end of its code, "
        "so every `require` and `wait_until` it met on the way held; one that failed names in its turn the line that "
        f"stopped it.\n{blocks}\n\n"
    )


def segment_note(step: int, segment: list[int] | None) -> str:
    """한 계획이 함께 돈 step 들을 묶음째 판정할 때 붙는 글. 묶음이 아니면 빈 글이다."""
    if not segment or len(segment) < 2:
        return ""
    steps = ", ".join(str(number) for number in segment)
    return (
        f"Steps {steps} were played under one plan, one after another, and the record below is the whole group's. "
        "Where one step ended and the next began is the executer's guess, so a step's result often shows up in the "
        f"turns marked with a neighbouring step. Judge step {step} on the whole record: it passed if, anywhere in the "
        "record and whichever step's turns it falls in, the state the step starts from held, the step's move was made, "
        "and what its expected result describes followed that move. A change that a different move caused does not "
        "count for this step. It failed if the record never shows its move followed by that result.\n\n"
    )


def evidence_text(play: StepPlay) -> str:
    """The active scene and the ```check values as the Evaluator's prompts draw them: each name once, with the
    readings where it changed. The first and the last reading are always shown, so a value that held still says so."""
    if play.state_source == "pulse_memory":
        return (
            "Game observations from PulseMemory (the SDK readings, not the executer's verdict):\n"
            f"Screen before play began:\n{play.start_screen or '(no reading available)'}\n\n"
            f"Screen when play stopped:\n{play.end_screen or '(no reading available)'}"
        )
    check = play.check
    named = []
    if check is not None and check.paths:
        named.append("paths " + ", ".join(check.paths))
    if check is not None and check.members:
        named.append("members " + ", ".join(check.members))
    asked = f"the plan named {'; '.join(named)}" if named else "the plan named no values (no ```check block)"
    head = (
        f"Game values ({asked}; the active scene is always read), read from the game state at the start, "
        "before each turn and at the end"
    )
    read = readings(play.evidence)
    failed = [f"{entry['at']}: {entry['error']}" for entry in play.evidence if "error" in entry]
    lines: list[str] = []
    if not read:
        lines.append("  (no reading succeeded)")
    names = names_of(read)
    for name in names:
        lines.append(f"  {name}: {_series(name, read)}")
    if play.evidence_unknown:
        lines.append(
            "  names the plan listed that matched nothing in any reading — a wrong name, or an object that never came; "
            "not by itself a sign the result is missing: " + ", ".join(play.evidence_unknown)
        )
    incomplete = [entry["at"] for entry in read if entry.get("clipped") or entry.get("omitted")]
    if incomplete:
        lines.append(
            "  readings that could not cover every object (a size limit or the server left some out): "
            + ", ".join(incomplete)
        )
    if failed:
        lines.append("  readings that failed: " + "; ".join(failed))
    text = f"{head}:\n" + "\n".join(lines)
    if play.changes:
        more = f"\n  ({play.changes_left_out} more changes left out)" if play.changes_left_out else ""
        text += (
            "\nEverything else in the game that changed from the start to the end of play, whatever the plan named:\n"
            + "\n".join(f"  {line}" for line in play.changes)
            + more
        )
    return text


def _series(name: str, read: list[dict]) -> str:
    """`start 30 → turn 4: 22 → end 0`. A name missing from a complete reading is `(absent)`, from an incomplete
    one `(not read)` (`evidence.points_of`)."""
    points = points_of(name, read)
    last_at = read[-1]["at"]
    if len(points) == 1:
        return f"{points[0][1]} (unchanged from {read[0]['at']} to {last_at})"
    parts = [f"{at}: {value}" for at, value in points]
    if points[-1][0] != last_at:
        parts.append(f"{last_at}: {points[-1][1]}")
    return " → ".join(parts)


def turn_line(turn: dict) -> str:
    """One turn of a play as the Evaluator's prompts draw it."""
    plan = f"plan {turn['plan']} · " if turn.get("plan") else ""
    if turn.get("segment_step"):
        plan = f"step {turn['segment_step']} · {plan}"
    parts = [f"  {plan}turn {turn['turn']} on {turn.get('scene')}: done()={turn.get('done')}"]
    if turn.get("done_error"):
        parts.append(f"(done() could not be read: {turn['done_error']})")
    if "goal_ask" in turn:
        parts.append(f"Jev on the goal being reached: {turn['goal_ask']}")
    if turn.get("later_done"):
        parts.append("a later step's done() became true: " + ", ".join(f"step {number}" for number in turn["later_done"]))
    if turn.get("scene_left"):
        parts.append(f"the scene changed from {turn['scene_left'].get('from')} to {turn['scene_left'].get('to')}")
    if "picked" in turn:
        parts.append(
            f"the only option ran: {turn['picked']}" if turn.get("picked_by") == "only_option" else f"Jev picked {turn['picked']}"
        )
    if "outcome" in turn:
        parts.append(f"→ {turn['outcome']}" + (f": {turn['reason']}" if turn.get("reason") else ""))
        sent = turn.get("sent") or []
        parts.append(f"sent {len(sent)} input{'s' if len(sent) != 1 else ''}" if sent else "sent no input")
        parts.extend(read_only_observations(turn))
        if turn.get("effect"):
            parts.append(f"its effect: {turn['effect']}")
        if turn.get("result"):
            parts.append(f"engine result: {turn['result']}")
    for flagged in turn.get("flags") or []:
        if isinstance(flagged, dict) and flagged.get("message"):
            observed = flagged.get("observed")
            parts.append(
                f"flag: {flagged['message']}"
                + (f" {json.dumps(observed, ensure_ascii=False, default=str)[:MAX_FLAG_CHARS]}" if observed else "")
            )
    if turn.get("repair"):
        parts.append(
            f"the editor rewrote the macros around {turn['repair']['action']}"
            if turn["repair"].get("replaced")
            else f"the editor's rewrite around {turn['repair']['action']} was refused"
        )
    if turn.get("jev_error"):
        parts.append(f"Jev did not answer: {turn['jev_error']}")
    return "; ".join(parts)


def _read(answer: Any, schema: type[Verdict] | type[GameVerdict] = Verdict) -> Verdict | GameVerdict:
    """The verdict in `schema`'s shape from what the model returned. Raises when it is not one."""
    if isinstance(answer, schema):
        return answer
    if isinstance(answer, BaseModel):
        return schema.model_validate(answer.model_dump())
    return schema.model_validate(read_json(answer))
