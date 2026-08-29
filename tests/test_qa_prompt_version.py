"""A QA run can be pinned to one prompt version, and says which it used.

Comparing two prompts means being able to ask for one of them per run, and being
able to tell afterwards which run got which. Both halves are load-bearing: a
per-run override nobody records is an A/B test with no result.
"""

import asyncio
import logging

import pytest

from app.agents.qa import runner as runner_module
from app.agents.qa.runner import QaRunner
from app.agents.qa.tools import QaRunState, build_tools
from app.api.qa_sessions import OpenQaSessionRequest
from app.qa.schemas import QaCaseRef, QaRunScenario, QaScenario, QaStep
from app.prompts import load_prompt
from app.prompts.loader import resolve_version, roles_in
from app.qa.channel import QaRunChannel
from app.qa.run_config import resolve_run_config
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore


async def _ignore(_frame: dict) -> None:
    """A send that goes nowhere, for the tools that only need to be built."""
    return None


def make_scenario() -> QaScenario:
    return QaScenario(
        title="튜토리얼",
        description="튜토리얼 진입을 확인한다",
        steps=[
            QaStep(
                action="시작 버튼을 누른다",
                case_id=1,
                case=QaCaseRef(
                    id=1,
                    precondition="타이틀 화면",
                    test_step="시작",
                    expected="튜토리얼 화면으로 넘어간다",
                ),
            )
        ],
    )


def open_request(**extra) -> dict:
    return {
        "context": {
            "qa_try_id": 7,
            "game_instance_id": 1,
            "test_scenario_id": 1,
            "scenario": make_scenario().model_dump(),
        },
        **extra,
    }


# --- the API contract ---------------------------------------------------------


def test_a_request_without_a_prompt_version_still_parses() -> None:
    """Every existing caller omits the field; none of them may start failing."""
    request = OpenQaSessionRequest.model_validate(open_request())

    assert request.prompt_version is None


def test_a_request_may_name_a_prompt_version() -> None:
    request = OpenQaSessionRequest.model_validate(open_request(prompt_version="v1"))

    assert request.prompt_version == "v1"


# --- the path from the request to the runner ----------------------------------


class RecordingRunner:
    def __init__(self) -> None:
        self.ran = asyncio.Event()

    async def run_with_deadline(self, channel, scenario, scene_context=None):
        self.ran.set()
        return None, None


async def _run_session(prompt_version: str | None) -> str | None:
    seen: list[str | None] = []
    runner = RecordingRunner()

    def factory(*, config):
        seen.append(config.prompt_version)
        return runner

    service = QaExecutionService(
        store=InMemoryQaSessionStore(), runner_factory=factory
    )
    session_id, _run_config = await service.open(
        qa_run_id=7,
        game_instance_id=1,
        scenarios=[
            QaRunScenario(qa_try_id=7, test_scenario_id=1, scenario=make_scenario())
        ],
        prompt_version=prompt_version,
    )

    async def send(_frame: dict) -> None:
        return None

    await service.run(session_id, send)
    assert runner.ran.is_set()
    return seen[0]


def test_the_requested_version_reaches_the_runner() -> None:
    assert asyncio.run(_run_session("v1")) == "v1"


def test_omitting_the_version_resolves_it_before_the_run() -> None:
    """The runner never sees the alias, only the version it stands for.

    `None` means "the newest", which is a name whose meaning changes the day a
    new version directory is added. Settling it at session open is what keeps a
    finished run attributable to the prompt it actually used.
    """
    assert asyncio.run(_run_session(None)) == resolve_version("qa_run")


# --- what the run leaves behind -----------------------------------------------


class SilentAgent:
    """An agent that ends the run without emitting anything."""

    def astream(self, *_args, **_kwargs):
        async def updates():
            return
            yield {}  # pragma: no cover - makes this an async generator

        return updates()


class StubChatModel:
    """러너가 만드는 모델 자리에 서지만, 모델은 아니다.

    `create_agent`도 스텁이라 이것을 호출하는 곳은 없다. 생성만 통과하면 된다.
    `SummarizationMiddleware.__init__`이 아무것도 호출하기 전에 요약 모델을
    `with_retry()`로 감싸기 때문이다.
    """

    def with_retry(self, *_args, **_kwargs):
        return self


@pytest.fixture
def stubbed_agent(monkeypatch):
    monkeypatch.setattr(
        runner_module, "build_chat_model", lambda model, reasoning=None, **_: StubChatModel()
    )
    monkeypatch.setattr(
        runner_module, "create_agent", lambda **_kwargs: SilentAgent()
    )


def test_the_run_start_log_names_the_prompt_version(stubbed_agent, caplog) -> None:
    """Without it, a trace shows what the model read but not which candidate it was."""

    async def send(_frame: dict) -> None:
        return None

    scenario = make_scenario()
    channel = QaRunChannel(qa_try_id=7, send=send)

    with caplog.at_level(logging.INFO, logger="app.agents.qa.runner"):
        asyncio.run(
            QaRunner(resolve_run_config(prompt_version="v1")).run(
                channel, scenario, QaRunState(total_steps=1)
            )
        )

    starting = [
        record.getMessage()
        for record in caplog.records
        if "[QA] run starting" in record.getMessage()
    ]
    assert len(starting) == 1
    assert "'prompt_version': 'v1'" in starting[0]
    # The system prompt still reaches the log, rendered rather than templated.
    assert "You are a QA agent executing an approved test scenario" in starting[0]
    assert "{language_directive}" not in starting[0]


def test_v2_adds_the_new_tools_and_keeps_v1_intact() -> None:
    """The new guidance goes in a new version, not on top of the old one.

    v1 is the frozen copy of the Python constants, and
    `tests/test_prompts_v1_regression.py` pins it. Editing it in place would make
    a run tagged `prompt_version=v1` unreproducible.
    """
    v1 = load_prompt("qa_run", "system", "v1").body
    v2 = load_prompt("qa_run", "system", "v2").body

    for tool in ("pause_game_time", "resume_game_time", "wait_for_operator"):
        assert tool not in v1
        assert tool in v2

    # Everything v1 said, v2 still says: the additions are insertions, not a rewrite.
    for paragraph in v1.split("\n\n"):
        assert paragraph in v2


def test_v3_shortens_what_the_tools_already_say_without_dropping_a_rule() -> None:
    """v3 stops repeating the tool descriptions. It must not stop stating the rules.

    The paragraphs it condensed were duplicates of the tool docstrings, and a
    duplicate is only safe to remove while the other copy is still there. So the
    check spans both halves: what a tool leaves behind, its own description now
    has to name the way out of, and the rules no single tool owns are still said
    out loud in the prompt.

    The pairs are the ones worth pinning. Nothing prompts `release_key` or
    `resume_game_time` except having called its partner, so a description that
    stops naming the undo leaves the run to end with a key down or time stopped —
    and that poisons every step after, not just the one that set it.
    """
    v2 = load_prompt("qa_run", "system", "v2").body
    v3 = load_prompt("qa_run", "system", "v3").body

    channel = QaRunChannel(qa_try_id=7, send=_ignore)
    tools = {tool.name: tool for tool in build_tools(channel, QaRunState(total_steps=1))}

    for holder, undo in (
        ("hold_mouse_button", "release_mouse_button"),
        ("hold_key", "release_key"),
        ("pause_game_time", "resume_game_time"),
    ):
        assert undo in tools[holder].description, (
            f"{holder} leaves state behind without naming {undo}"
        )

    # Waiting is the other way a run ends with nothing to show. v2 closed that
    # off in the prompt ("do not wait forever on silence"); v3 dropped the
    # sentence, so the tool has to say the run pays for it.
    assert "run's clock" in tools["wait_for_operator"].description

    # Cross-tool rules have no single owner, so no tool description can carry them.
    assert "VERBATIM" in v3
    assert "before you report" in v3
    assert "`on screen:`" in v3

    # And the shortening has to have actually happened.
    assert len(v3) < len(v2)


def test_the_default_qa_version_is_v14() -> None:
    """A run that names no version has to get the newest prompt.

    This is also the trap in adding a version: `resolve_version` returns the
    highest-numbered directory, so creating one silently repoints every run that
    has not pinned `qa_prompt_version`. That is why each version ships in the
    same change as the tools it talks about — a prompt that names
    `set_input_axis` before the tool exists teaches the agent to reach for
    something that is not there.
    """
    assert resolve_version("qa_run") == "v14"


def test_v12_drops_the_screen_map_and_says_what_a_screen_anchors() -> None:
    """The map moved to Orchestration's `content_map`, so the prompt stops asking for one.

    A deletion is the one prompt change that cannot be checked by "everything the
    old version said, the new one still says", so what it leaves behind has to be
    pinned instead: the section is gone, the two sentences that leaned on it no
    longer name a route, and the criterion that replaces it is stated — a fact true
    only here says where, a fact true everywhere says nothing.

    The tool descriptions carry the same rule (ARTEL-192 makes them the single
    source for how to CALL a tool), and `tests/test_qa_knowledge_graph.py` pins
    that half. This half is the habit that spans `record_knowledge` and
    `link_knowledge`, which no one description owns.
    """
    v11 = load_prompt("qa_run", "system", "v11").body
    v12 = load_prompt("qa_run", "system", "v12").body

    assert "### The screen map" in v11
    assert "### The screen map" not in v12
    assert "LEADS_TO" not in v12

    # The opening that leaned on the deleted section, and the section that took its
    # place. "the clearest case" was the sentence pointing back at the screen map.
    assert "### Structuring the rest of what you know" in v12
    assert "the clearest case" not in v12
    assert "The list of screens and the routes between them are NOT that." in v12
    assert "Say which screen or scene it holds on when you record it" in v12
    assert "names no screen at all" in v12

    # Breakage is still reported rather than unlinked; only the routes went.
    assert "removing a link because the build is broken" in v12


# v12 문단 중 v13 이 **의도적으로** 고쳐 쓴 것들. 여기 없는 문단은 한 글자도 바뀌면 안 된다.
#
# 전부 같은 이유다: ARTEL-621 이 매 모델 호출 뒤에 붙던 `<<current scene>>` 꼬리를 없앴는데
# v12 본문이 그것을 그대로 가르치고 있다. v13 이 배포 즉시 기본값이 되므로, 없는 블록을
# 가리키는 문장을 그대로 실으면 에이전트가 오지 않는 것을 기다린다.
V13_REWRITES_FROM_V12 = (
    "The last thing in every message you receive is a block marked `<<current scene>>`.",
    "Inside that block, a value is written as its whole history, oldest first:",
    "That block ends with the last handful of things the game itself ran,",
    "Your conversation may be compacted when it grows long:",
)


def test_v13_teaches_the_scene_context_block_and_keeps_the_rest_of_v12() -> None:
    """v13 is v12 plus one section, and the section's job is a boundary.

    The block itself carries the same boundary (see
    `tests/test_qa_scene_context.py`), and it is said twice on purpose: a list
    sitting in front of the agent is read as complete, and each of the two places
    it is said can be lost on its own — the prompt to compaction, the block to a
    lookup that answered nothing.
    """
    v12 = load_prompt("qa_run", "system", "v12").body
    v13 = load_prompt("qa_run", "system", "v13").body

    assert "<<scene context>>" not in v12
    assert "### What is already known about this scene" in v13

    # The boundary, and the two things that must not be mistaken for what they
    # are not: an id line is not the entry, and a map path is not a target.
    assert "It is anchored knowledge only, and that is a hard boundary." in v13
    assert '"nothing is filed under this scene alone"' in v13
    assert "never the entry itself" in v13
    assert "not something to aim at" in v13

    # 고쳐 쓴 네 문단 말고는 v12 그대로다. 목록으로 예외를 세우는 것은, 아무 문단이나
    # 조용히 사라지는 것을 막으면서 고친 자리는 이름을 대게 하려는 것이다.
    for paragraph in v12.split("\n\n"):
        if paragraph.startswith(V13_REWRITES_FROM_V12):
            assert paragraph not in v13, "고쳐 쓴다고 해 놓고 옛 문단이 남아 있다"
            continue
        assert paragraph in v13


V14_REWRITES_FROM_V13 = (
    "A capability line is what the map recorded, not what is on the screen in front of you.",
)


def test_v14_puts_the_scene_view_above_the_block_and_keeps_the_rest_of_v13() -> None:
    """잘린 목록이 관측을 이겼다. v14 는 순서를 문단 끝에서 절 앞으로 옮긴다 (ARTEL-679).

    v13 도 같은 말을 하기는 했다 — `Treat the list as where to look first, and the
    scene as what is true.` 가 문단의 마지막 문장으로 있었다. 그런데 블록 자신의
    제목은 `the content map says this can be done here` 라는 단정형이고, 스테이지에서
    둘이 부딪혔을 때 단정형이 이겼다. 같은 시나리오를 도는 두 런 중 블록이 붙지 않은
    쪽은 scene view 가 제공하는 `Return` 을 찾아 썼고, 잘린 목록 여덟 줄이 전부
    방향키였던 쪽은 `Return` 을 한 번도 누르지 않았다.

    그래서 두 가지를 더 말한다. 순서를 먼저 말하고, 목록에 없다는 것이 못 한다는
    뜻이 아님을 말한다 — 후자가 필요한 이유는 content map 이 드래그를 표현할 수단이
    아예 없어서다. 어느 빌드의 어느 씬도 드래그를 목록에 싣지 못하는데, scene view 는
    그것을 `can do — pointer:` 로 평범하게 말한다.

    블록 자신도 같은 말을 한다(`tests/test_qa_scene_context.py`). 두 곳에 두는 이유는
    v13 이 경계를 두 번 그은 이유와 같다 — 프롬프트는 압축에, 블록은 빈 조회에 각각
    사라질 수 있다.
    """
    v13 = load_prompt("qa_run", "system", "v13").body
    v14 = load_prompt("qa_run", "system", "v14").body

    # 순서, 그리고 목록의 침묵이 뜻하지 않는 것.
    assert "**The scene view outranks that block, always.**" in v14
    assert "**A capability missing from that block does not mean you cannot do it.**" in v14
    assert "dragging, in particular, appears in no scene's list on any build" in v14

    # v13 이 이미 하던 말은 그대로 남는다. 이것을 지우고 새 문장으로 갈음하면,
    # 같은 말을 두 번 하지 않게 된 대신 한 번도 안 하게 될 위험이 생긴다.
    assert "Treat the list as where to look first, and the scene as what is true." in v14
    assert "not something to aim at" in v14

    # 고쳐 쓴 한 문단 말고는 v13 그대로다.
    for paragraph in v13.split("\n\n"):
        if paragraph.startswith(V14_REWRITES_FROM_V13):
            assert paragraph not in v14, "고쳐 쓴다고 해 놓고 옛 문단이 남아 있다"
            continue
        assert paragraph in v14


def test_v13_stops_teaching_the_live_view_ARTEL_621_deleted() -> None:
    """없는 블록을 가리키는 문장이 v13 에 남아 있으면 안 된다.

    꼬리는 매 모델 호출 뒤에 붙었다 사라지는 메시지였고, 그것이 프롬프트 접두를 매 턴
    깨뜨려 캐시를 못 쓰게 만들고 있었다. 화면은 이제 도구 결과 안의 씬 뷰가 내고, 압축은
    원장이 그것을 다시 말한다(ARTEL-622).
    """
    v13 = load_prompt("qa_run", "system", "v13").body

    assert "<<current scene>>" not in v13
    assert "`<<scene view N>>`" in v13
    assert "changed since your last look:" in v13
    assert "the game ran since your last look:" in v13
    # 압축이 화면을 다시 말한다는 것. 종전에는 꼬리가 그 보장이었다.
    assert "That block restates the screen as it stands" in v13


def test_v13_defines_the_same_roles_as_v12() -> None:
    """Reading a screen did not change, so the vision half did not either."""
    assert roles_in("qa_run", "v13") == roles_in("qa_run", "v12")
    assert load_prompt("qa_run", "vision_directive", "v13").body == (
        load_prompt("qa_run", "vision_directive", "v12").body
    )


def test_v12_defines_the_same_roles_as_v11() -> None:
    """A version directory missing a role breaks only the runs that need it.

    `vision_directive` is loaded from the resolved version, so a v12 without it
    raises at run time for a vision run and never for a text-only one.
    """
    assert roles_in("qa_run", "v12") == roles_in("qa_run", "v11")
    # Reading a screen did not change, so the file did not either.
    assert load_prompt("qa_run", "vision_directive", "v12").body == (
        load_prompt("qa_run", "vision_directive", "v11").body
    )


def test_v9_structures_the_body_and_adds_the_knowledge_base_section() -> None:
    """v9 is v8's sentences under headings, plus the one section that is new.

    The screen-map habit spans `observe_scene`, `record_knowledge` and
    `link_knowledge`, and no single tool description is its home — which is why
    it goes in the system prompt without contradicting ARTEL-192, whose rule is
    that how to CALL one tool stays in that tool's description.
    """
    v8 = load_prompt("qa_run", "system", "v8").body
    v9 = load_prompt("qa_run", "system", "v9").body

    assert "## The knowledge base" in v9
    assert "### The screen map" in v9
    assert "### Removing a link" in v9
    assert "## The knowledge base" not in v8
    # v8's body survives: a sentence carried over verbatim proves it is the same
    # prompt with structure, not a rewrite.
    assert "A failed step does NOT end the run." in v8
    assert "Report it failed with what you saw" in v9


def test_v11_teaches_the_axis_fallback_and_ties_it_to_the_knowledge_base() -> None:
    """A tool the prompt never mentions is a tool the agent will not reach for.

    The agent cannot tell whether a game reads `GetKey` or `GetAxis` — there is
    no runtime API for the binding, so the SDK cannot tell it either. Trying and
    watching the screen is the only way, which makes the fallback a habit
    spanning `hold_key`, `set_input_axis` and `record_knowledge`, and no single
    tool description is its home. What is pinned here is the recording half: an
    agent that works it out and does not write it down makes every later run pay
    the same wasted round trip.
    """
    v10 = load_prompt("qa_run", "system", "v10").body
    v11 = load_prompt("qa_run", "system", "v11").body

    assert "set_input_axis" in v11
    assert "set_input_axis" not in v10
    assert "record_knowledge" in v11
    assert "Horizontal" in v11
    # An axis is state you set, like a held key. v10 already said that about keys;
    # v11 has to extend it rather than leave a second way to poison later steps.
    assert "return it to 0" in v11

    # One section added, nothing from v10 dropped.
    for paragraph in v10.split("\n\n"):
        assert paragraph in v11


def test_v11_defines_the_same_roles_as_v10() -> None:
    """A version directory missing a role breaks only the runs that need it.

    `vision_directive` is loaded from the resolved version, so a v11 without it
    raises at run time for a vision run and never for a text-only one. Nothing
    else in the suite would notice: the lock records what is on disk, and the
    body assertions only read `system`.
    """
    assert roles_in("qa_run", "v11") == roles_in("qa_run", "v10")


def test_v8_is_v7_and_marks_the_tool_set_that_changed_under_it() -> None:
    """A version whose body did not move, which is the point of it.

    `update_knowledge` arrived and the "delete, then record" repair instruction
    left with it (ARTEL-257), but none of that is prompt text: ARTEL-192 put the
    usage policy for these tools in their descriptions and left the system prompt
    alone. What still has to happen is that runs before and after the change fall
    in different buckets — Orchestration files `qa_try.prompt_version` from the
    resolved version this returns — so the version is bumped and the body is not.
    """
    v7 = load_prompt("qa_run", "system", "v7").body
    v8 = load_prompt("qa_run", "system", "v8").body

    assert v8 == v7
    assert load_prompt("qa_run", "vision_directive", "v8").body == (
        load_prompt("qa_run", "vision_directive", "v7").body
    )
    # The note is the only place the reason for an identical version is written.
    assert "update_knowledge" in load_prompt("qa_run", "system", "v8").note


def test_v4_teaches_the_live_view_and_the_value_paths() -> None:
    """The view and the history lists are useless if the agent cannot read them:
    `100 → 80 → 60   [obs 4, 7, 11]` says nothing to a model never told what the
    arrows and the bracket are."""
    v3 = load_prompt("qa_run", "system", "v3").body
    v4 = load_prompt("qa_run", "system", "v4").body

    assert "<<current scene>>" in v4
    assert "[obs 4, 7, 11]" in v4
    assert "moved:" in v4
    assert "(earlier changes trimmed)" in v4

    # One paragraph added, nothing from v3 dropped.
    for paragraph in v3.split("\n\n"):
        assert paragraph in v4


def test_v6_says_a_failed_step_does_not_end_the_run() -> None:
    """Every version up to v5 said how to work, none what to do when it did not work.

    A run that stops at the first failure never reaches the steps it was opened
    to find out about, so both halves are pinned: the scenario is intent rather
    than a script, and a verdict of failed is something to record and move past.
    """
    v5 = load_prompt("qa_run", "system", "v5").body
    v6 = load_prompt("qa_run", "system", "v6").body

    assert "A failed step does NOT end the run" in v6
    assert "intent, not a script" in v6
    assert "Never simply stop" in v6

    # Two paragraphs added, nothing from v5 dropped.
    for paragraph in v5.split("\n\n"):
        assert paragraph in v6


def test_v7_separates_a_game_defect_from_a_step_verdict() -> None:
    """`report_issue` is worth nothing if the agent files step failures into it.

    The two come apart in both directions and the prompt has to say so: a step
    can fail because the scenario is wrong about the game, and a step can pass
    while a real defect goes by. What is pinned is that distinction, not the
    tool's own description — the cap and the severity ladder live there.
    """
    v6 = load_prompt("qa_run", "system", "v6").body
    v7 = load_prompt("qa_run", "system", "v7").body

    assert "report_issue" in v7
    assert "wrong about the GAME rather than about the step" in v7
    assert "one call per distinct defect" in v7

    # One paragraph added, nothing from v6 dropped.
    for paragraph in v6.split("\n\n"):
        assert paragraph in v7


def test_v10_adds_the_citation_section_and_keeps_v9_intact() -> None:
    """The citation guidance is a new version, not an edit to a released one.

    It also has to stay UNPRESSURED. Pushing a model to cite more buys citations
    of whatever is at hand, and the known under-reporting bias becomes a
    contamination whose direction nobody knows — so the section says what counts,
    says most steps cite nothing, and stops there.
    """
    v9 = load_prompt("qa_run", "system", "v9").body
    v10 = load_prompt("qa_run", "system", "v10").body

    assert "### Saying what you used" in v10
    assert "used_knowledge_ids" in v10 and "used_knowledge_ids" not in v9
    # An empty list is stated as a complete answer, not as a failure to comply.
    assert "an empty list is a complete answer" in v10
    # Everything v9 said, v10 still says: the addition is an insertion, not a rewrite.
    for paragraph in v9.split("\n\n"):
        assert paragraph in v10
