"""capture 답의 점검 목록과 scene 첫 진입 capture 줄 (ARTEL-1221)."""

import asyncio

from app.agents.qa.arch import QaArchSpec, ScreenCaptureMode, VisionMode, resolve_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools.observation_tools import CAPTURE_CHECKLIST
from app.qa.channel import QaRunChannel


def make(vision: VisionMode = VisionMode.on, capture=ScreenCaptureMode.on_demand):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=0.05, write_timeout=0.05)
    state = QaRunState(total_steps=2)
    arch = resolve_arch(
        QaArchSpec(vision=vision, screen_capture=capture), "openai/gpt-6-luna"
    )
    tools = {tool.name: tool for tool in build_tools(channel, state, arch)}
    return channel, state, tools, sent


def enter(channel: QaRunChannel, scene: str, reading: int) -> None:
    channel.on_pulse(
        {"type": "PULSE", "payload": {"reading": reading, "frame": reading * 10, "scene": scene, "whole": True}}
    )


def test_the_first_view_of_each_scene_asks_for_one_capture() -> None:
    async def run() -> None:
        channel, state, tools, _sent = make()
        enter(channel, "TitleScene", 1)

        first = await tools["observe_scene"].ainvoke({"step": 1, "thought": "look"})
        again = await tools["observe_scene"].ainvoke({"step": 1, "thought": "look"})
        enter(channel, "StoryScene", 2)
        story = await tools["observe_scene"].ainvoke({"step": 2, "thought": "look"})

        assert "First view of scene `TitleScene` this run: call `capture_screen` once" in first
        assert "First view of scene" not in again
        assert "First view of scene `StoryScene`" in story
        assert state.scenes_entered == {"TitleScene", "StoryScene"}

    asyncio.run(run())


def test_no_line_without_vision_or_when_pictures_arrive_on_their_own() -> None:
    async def run() -> None:
        for vision, capture in (
            (VisionMode.off, ScreenCaptureMode.on_demand),
            (VisionMode.on, ScreenCaptureMode.every_call),
        ):
            channel, _state, tools, _sent = make(vision, capture)
            enter(channel, "TitleScene", 1)
            looked = await tools["observe_scene"].ainvoke({"step": 1, "thought": "look"})
            assert "First view of scene" not in looked, (vision, capture)

    asyncio.run(run())


def test_a_capture_answer_carries_the_checklist() -> None:
    async def run() -> None:
        channel, _state, tools, sent = make()

        async def answer_capture() -> None:
            while not any(frame["type"] == "ACTION" for frame in sent):
                await asyncio.sleep(0)
            action = next(frame for frame in sent if frame["type"] == "ACTION")
            channel.on_action_result(
                {
                    "type": "ACTION_RESULT",
                    "correlationId": action["messageId"],
                    "payload": {
                        "results": [
                            {
                                "id": 1,
                                "success": True,
                                "returnValue": {"url": "http://minio/c.png", "width": 8, "height": 8},
                            }
                        ]
                    },
                }
            )

        answered, _ = await asyncio.gather(
            tools["capture_screen"].ainvoke({"step": 1, "thought": "hand"}), answer_capture()
        )
        assert "Captured the screen. The image follows." in answered
        assert CAPTURE_CHECKLIST in answered
        for check in ("no art", "overlapping", "cut off", "developer console", "`report_issue`"):
            assert check in CAPTURE_CHECKLIST

    asyncio.run(run())
