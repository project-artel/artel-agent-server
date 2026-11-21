from types import SimpleNamespace

import asyncio

from langchain_core.messages import ToolMessage

from app.agents.qa.scenario_cheats import QaScenarioCheatsMiddleware, scenario_cheat_steps
from app.agents.qa.tools.state import QaRunState
from app.qa.game_functions import UsedCheat

STEPS = [
    "게임 기능 set_title_state(hasSave=false, finishedTutorial=true) 로 타이틀 화면을 다시 불러온다",
    "'New Game' 버튼을 눌러 집에 들어간다",
    "게임 기능 give_item(item=Redberry, count=2) 와 게임 기능 set_water(water=150) 로 물과 열매를 맞춘다",
    "인벤토리의 Redberry 를 탭하고 'Use' 를 누른다",
]
META = [(None, False), (None, False), (None, False), (81, True)]


def _state() -> QaRunState:
    return QaRunState(total_steps=len(STEPS), step_meta=list(META), step_texts=list(STEPS))


class _Cheats:
    """`run_cheat` 처럼 보낸 호출을 `state.used_cheats` 에 남긴다."""

    def __init__(self, fail: set[str] = frozenset()) -> None:
        self.sent: list[tuple[int, str, dict]] = []
        self.fail = fail
        self.state: QaRunState | None = None

    async def __call__(self, step, name, thought, arguments, target):
        self.sent.append((step, name, arguments))
        ok = name not in self.fail
        self.state.used_cheats.append(UsedCheat(name=name, args=arguments, target=target, success=ok))
        return f"ok — {name} done" if ok else f"{name} failed: unavailable"


def _middleware(state, cheats, names=("set_title_state", "give_item", "set_water")):
    cheats.state = state
    return QaScenarioCheatsMiddleware(state, lambda: list(names), cheats)


def _request(tool: str, **args):
    return SimpleNamespace(tool_call={"name": tool, "args": args, "id": "call-1"})


async def _handler(request):
    return ToolMessage(content="tool answer", tool_call_id=request.tool_call["id"], name=request.tool_call["name"])


def test_판정이_없는_단계의_호출만_모은다():
    async def run():
        steps = scenario_cheat_steps(_state())
        assert sorted(steps) == [1, 3]
        assert [c.name for c in steps[3]] == ["give_item", "set_water"]

    asyncio.run(run())


def test_단계에_닿으면_그_단계까지의_호출을_먼저_보내고_결과_앞에_적는다():
    async def run():
        cheats = _Cheats()
        middleware = _middleware(_state(), cheats)

        result = await middleware.awrap_tool_call(_request("click", step=2, target="New Game"), _handler)

        assert cheats.sent == [(1, "set_title_state", {"hasSave": False, "finishedTutorial": True})]
        assert result.content.startswith("Step 1: the engine ran")
        assert result.content.endswith("tool answer")

    asyncio.run(run())


def test_start_test_case_는_그_case_의_첫_단계까지_보낸다():
    async def run():
        cheats = _Cheats()
        middleware = _middleware(_state(), cheats)

        await middleware.awrap_tool_call(_request("start_test_case", case_id=81), _handler)

        assert [(step, name) for step, name, _ in cheats.sent] == [(1, "set_title_state"), (3, "give_item"), (3, "set_water")]

    asyncio.run(run())


def test_한_번_보낸_단계는_다시_보내지_않는다():
    async def run():
        cheats = _Cheats()
        middleware = _middleware(_state(), cheats)

        await middleware.awrap_tool_call(_request("click", step=1), _handler)
        result = await middleware.awrap_tool_call(_request("click", step=2), _handler)

        assert len(cheats.sent) == 1
        assert result.content == "tool answer"

    asyncio.run(run())


def test_agent_가_같은_단계에서_같은_호출을_다시_부르면_보내지_않는다():
    async def run():
        cheats = _Cheats()
        middleware = _middleware(_state(), cheats)
        called = []

        async def handler(request):
            called.append(request)
            return await _handler(request)

        result = await middleware.awrap_tool_call(
            _request("call_game_function", step=1, name="set_title_state",
                     arguments={"hasSave": False, "finishedTutorial": True}),
            handler,
        )

        assert called == []
        assert len(cheats.sent) == 1
        assert "already ran `set_title_state`" in result.content

    asyncio.run(run())


def test_인자가_다른_호출은_agent_의_호출대로_보낸다():
    async def run():
        cheats = _Cheats()
        middleware = _middleware(_state(), cheats)
        called = []

        async def handler(request):
            called.append(request)
            return await _handler(request)

        await middleware.awrap_tool_call(
            _request("run_cheat", step=1, name="set_title_state", arguments={"hasSave": True}), handler,
        )

        assert len(called) == 1

    asyncio.run(run())


def test_빌드에_없는_이름은_보내지_않고_agent_에게_맡긴다():
    async def run():
        cheats = _Cheats()
        middleware = _middleware(_state(), cheats, names=("give_item", "set_water"))

        result = await middleware.awrap_tool_call(_request("click", step=1), _handler)

        assert cheats.sent == []
        assert "not sent: set_title_state" in result.content
        assert "Do not call them again" not in result.content

    asyncio.run(run())


def test_앞_호출이_실패하면_같은_단계의_뒤_호출은_보내지_않는다():
    async def run():
        cheats = _Cheats(fail={"give_item"})
        middleware = _middleware(_state(), cheats)

        await middleware.awrap_tool_call(_request("click", step=3), _handler)

        assert [name for _, name, _ in cheats.sent] == ["set_title_state", "give_item"]

    asyncio.run(run())


def test_치트가_없는_run_은_보내지_않는다():
    async def run():
        middleware = QaScenarioCheatsMiddleware(_state(), lambda: [], None)

        result = await middleware.awrap_tool_call(_request("click", step=1), _handler)

        assert "this run has no cheats" in result.content

    asyncio.run(run())


def test_middleware_는_cheat_축이_켜진_run_에만_붙는다():
    from app.agents.qa.arch import QaArchSpec, resolve_arch
    from app.agents.qa.runner import middleware_names_for

    model = "openai/gpt-6-luna"
    assert "scenario_cheats" in middleware_names_for(resolve_arch(QaArchSpec(cheats="on"), model))
    assert "scenario_cheats" not in middleware_names_for(resolve_arch(QaArchSpec(), model))
    assert "scenario_cheats" not in middleware_names_for(
        resolve_arch(QaArchSpec(cheats="on", scenario_cheats="off"), model)
    )
