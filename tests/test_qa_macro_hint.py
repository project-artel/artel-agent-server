"""Jev 가 고른 macro hint 를 재는 테스트 (ARTEL-1026). 네트워크 없음.

세 층이다. `jev.choose` 의 요청과 실패, 후보와 hint 문장, 그리고 실제 tool 로 손 조작을 보냈을 때
결과 문장의 어디에 hint 가 앉는가.
"""

import asyncio

import pytest

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.llm.models import LLMModel
from app.qa import jev
from app.qa.channel import QaRunChannel
from app.qa.macro_hint import candidates, hint_counts, hint_line
from app.qa.pulse import PulseReading
from app.qa.scene_context import SceneContext, SceneMacro, SceneMacroParameter, SceneMacroScreen


def scene_macro(name: str, summary: str | None, *scenes: str, params=()) -> SceneMacro:
    return SceneMacro(
        name=name,
        summary=summary,
        parameters=[SceneMacroParameter(name=p, type=t) for p, t in params],
        screens=[SceneMacroScreen(scene_name=one) for one in scenes],
    )


FIGHT = scene_macro(
    "fight_stage", "Fight one stage to the clear screen.\n\nMore.", "Battle",
    params=[("enemy", "object")],
)
SHOP = scene_macro("buy_potion", "Buy a potion.", "Shop")


class FakeJev:
    def __init__(self, choice: str | None = None, error: Exception | None = None) -> None:
        self.bodies: list[dict] = []
        self.choice = choice
        self.error = error

    async def __call__(self, body: dict) -> dict:
        self.bodies.append(body)
        if self.error:
            raise self.error
        return {"answers": {"pick": {"choice": self.choice}}}


# --- jev.choose ------------------------------------------------------------------------


def test_choose_sends_a_choice_question_and_returns_the_pick() -> None:
    fake = FakeJev("b")

    picked = asyncio.run(jev.choose("which?", ["a", "b"], descriptions={"a": "first"}, post=fake))

    assert picked == "b"
    body = fake.bodies[0]
    assert body["model"] == "typesafe/jev-1.13"
    question = body["questions"]["pick"]
    assert question["type"] == "choice" and question["instructions"] == "which?"
    assert question["criteria"]["a"]["what"] == "first"
    assert question["criteria"]["b"]["what"] == "b"


def test_choose_fails_open_on_error_timeout_and_unknown_choice() -> None:
    assert asyncio.run(jev.choose("q", ["a"], post=FakeJev(error=RuntimeError("x")))) is None
    assert asyncio.run(jev.choose("q", ["a"], post=FakeJev("zzz"))) is None

    async def slow(body: dict) -> dict:
        await asyncio.sleep(1)
        return {}

    assert asyncio.run(jev.choose("q", ["a"], timeout=0.01, post=slow)) is None
    assert asyncio.run(jev.choose("q", [], post=FakeJev("a"))) is None


# --- 후보와 문장 -------------------------------------------------------------------------------


def test_candidates_are_the_macros_on_the_current_scene_capped_at_eight() -> None:
    many = [scene_macro(f"m{i}", "d", "Battle") for i in range(12)]

    assert [one.name for one in candidates("Battle", {}, [SHOP, FIGHT])] == ["fight_stage"]
    assert len(candidates("Battle", {}, many)) == 8
    assert candidates("", {}, many) == []


def test_hint_line_names_the_macro_its_parameters_and_the_first_docstring_line() -> None:
    fake = FakeJev("fight_stage")

    line = asyncio.run(
        hint_line("Win the battle", "Battle", {}, [FIGHT, SHOP], choose=_with(fake))
    )

    assert line == (
        "A registered macro fits this step: fight_stage(enemy: object) — Fight one stage to "
        "the clear screen. Call run_macro instead of acting by hand."
    )
    assert list(fake.bodies[0]["questions"]["pick"]["criteria"]) == ["fight_stage", "none"]
    assert "Win the battle" in fake.bodies[0]["questions"]["pick"]["instructions"]


def test_no_hint_when_jev_says_none_the_step_is_empty_or_no_macro_fits() -> None:
    fake = FakeJev("none")
    assert asyncio.run(hint_line("x", "Battle", {}, [FIGHT], choose=_with(fake))) is None
    assert asyncio.run(hint_line("  ", "Battle", {}, [FIGHT], choose=_with(fake))) is None
    assert asyncio.run(hint_line("x", "Shop", {}, [FIGHT], choose=_with(fake))) is None
    assert len(fake.bodies) == 1


def _with(fake: FakeJev):
    async def choose(instruction, options, **kwargs):
        return await jev.choose(instruction, options, post=fake, **kwargs)

    return choose


# --- tool 결과 안에서의 자리 -----------------------------------------------------------------------


def make(hint: bool = True, step_texts=("Win the battle",), macros=(FIGHT,)):
    async def send(frame: dict) -> None:
        pass

    channel = QaRunChannel(qa_try_id=9, send=send, action_timeout=0.05, write_timeout=0.05)
    channel.scene.pulse.apply(
        PulseReading.model_validate({
                "scene": "Battle",
                "whole": True,
                "active": [{"selector": "Root[0]/Hp[1]", "id": 7, "text": "Hp 10"}],
            })
    )
    channel.scene.scene_context = SceneContext(macros=list(macros))
    state = QaRunState(total_steps=1, step_texts=list(step_texts))
    arch = resolve_arch(
        QaArchSpec(
            vision=VisionMode.on, phase_cycle=PhaseCycleMode.off, macros="on", macro_hint=hint
        ),
        LLMModel.gpt_6_luna,
    )
    tools = {one.name: one for one in build_tools(channel, state, arch)}
    return channel, state, tools


async def press(tools, step: int = 1) -> str:
    return await tools["press_key"].ainvoke(
        {"step": step, "thought": "t", "key_code": "Space", "duration_seconds": 0.1}
    )


def test_the_third_hand_action_carries_the_hint_before_the_scene_view(monkeypatch) -> None:
    fake = FakeJev("fight_stage")
    monkeypatch.setattr(jev, "_post", fake)
    channel, state, tools = make()

    async def scenario():
        return [await press(tools) for _ in range(5)]

    results = asyncio.run(scenario())

    assert all("A registered macro fits" not in one for one in results[:2])
    third = results[2]
    assert "A registered macro fits this step: fight_stage(enemy: object)" in third
    view_at = third.find("<<pulse>>")
    assert view_at != -1 and third.index("A registered macro fits") < view_at
    assert all("A registered macro fits" not in one for one in results[3:])
    assert len(fake.bodies) == 1  # 한 step 에 한 번
    assert hint_counts(state) == (1, 0)


def test_the_hint_sits_before_operator_messages(monkeypatch) -> None:
    monkeypatch.setattr(jev, "_post", FakeJev("fight_stage"))
    channel, state, tools = make()

    async def scenario():
        await press(tools)
        await press(tools)
        channel._operator_messages.append("hurry")
        return await press(tools)

    result = asyncio.run(scenario())
    assert result.index("A registered macro fits") < result.index("<<pulse>>")
    assert result.index("<<end pulse>>") < result.index("The operator said")


@pytest.mark.parametrize("why", ["knob_off", "macro_called", "no_text", "jev_none", "jev_error"])
def test_no_hint_when(why, monkeypatch) -> None:
    fake = FakeJev("none") if why == "jev_none" else FakeJev(error=RuntimeError("down"))
    monkeypatch.setattr(jev, "_post", fake if why.startswith("jev") else FakeJev("fight_stage"))
    channel, state, tools = make(
        hint=why != "knob_off", step_texts=("",) if why == "no_text" else ("Win the battle",)
    )
    if why == "macro_called":
        state.macro_calls.record_call(
            step=1, name="x", arguments={}, scene="Battle", scene_after="Battle",
            passed=True, paused=False,
        )

    async def scenario():
        return [await press(tools) for _ in range(4)]

    assert all("A registered macro fits" not in one for one in asyncio.run(scenario()))
    assert hint_counts(state) == (0, 0)


def test_followed_counts_a_hinted_step_that_later_called_run_macro(monkeypatch) -> None:
    monkeypatch.setattr(jev, "_post", FakeJev("fight_stage"))
    channel, state, tools = make()

    async def scenario():
        for _ in range(3):
            await press(tools)

    asyncio.run(scenario())
    state.macro_calls.record_call(
        step=1, name="fight_stage", arguments={}, scene="Battle", scene_after="Battle",
        passed=True, paused=False,
    )

    assert hint_counts(state) == (1, 1)
