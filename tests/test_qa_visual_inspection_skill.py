"""선택적 시각 점검 로딩과 자동 점검 제거의 계약 검증."""

import asyncio

import pytest

from app.agents.qa.arch import QaArchSpec, VisionMode, arch_fingerprint, resolve_arch, withheld_skills
from app.agents.qa.runner import middleware_names_for
from app.agents.qa.compaction import build_compact_tool
from app.agents.qa.runner import system_prompt_with_skills, _skills_directive
from app.agents.qa.tools import QaRunState, build_tools
from app.prompts import load_skill, skill_names
from app.qa.channel import QaRunChannel
from app.qa.run_config import resolve_run_config


def make(skills="off", vision=VisionMode.on, version="v39"):
    sent = []

    async def send(frame):
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=0.5, write_timeout=0.5)
    arch = resolve_arch(QaArchSpec(skills=skills, vision=vision), "openai/gpt-6-luna")
    tools = {t.name: t for t in build_tools(channel, QaRunState(total_steps=2), arch, version)}
    return channel, arch, tools, sent


@pytest.mark.parametrize("skills", ["off", "on_demand"])
def test_visual_procedure_is_loaded_only_when_requested(skills):
    async def run():
        channel, arch, tools, sent = make(skills)
        assert "visual_inspection" in tools["load_skill"].description
        prompt = system_prompt_with_skills("system", "v39", skills, withheld_skills(arch))
        assert "# Visual inspection" not in prompt
        body = await tools["load_skill"].ainvoke({"name": "visual_inspection", "thought": "check suspected clipping"})
        assert "<<skill visual_inspection>>" in body
        assert "Do not invent an expected design" in body
        assert "report_test_case" in body and "report_issue" in body
        assert sent == []
        if skills == "off":
            other = await tools["load_skill"].ainvoke({"name": "held_state", "thought": "already inlined"})
            assert "not a skill" in other
    asyncio.run(run())


def test_visual_skill_is_unavailable_without_vision_and_old_prompts_stay_pinned():
    for skills in ("off", "on_demand"):
        _, arch, tools, _ = make(skills, VisionMode.off)
        assert "visual_inspection" in withheld_skills(arch)
        assert "visual_inspection" not in _skills_directive("v39", skills, withheld_skills(arch))
        if "load_skill" in tools:
            assert "visual_inspection" not in tools["load_skill"].description
        assert "# Visual inspection" not in system_prompt_with_skills("system", "v39", skills, withheld_skills(arch))
    assert "visual_inspection" not in skill_names("v30")
    _, _, old_tools, _ = make("off", version="v30")
    assert "load_skill" not in old_tools


def test_scene_entry_and_capture_do_not_inject_visual_checks():
    async def run():
        channel, _, tools, sent = make()
        for reading, scene in ((1, "TitleScene"), (2, "StoryScene")):
            channel.on_pulse({"type": "PULSE", "payload": {"reading": reading, "frame": reading * 10, "scene": scene, "whole": True}})
            result = await tools["observe_scene"].ainvoke({"step": 1, "thought": "continue TC"})
            assert "First view of scene" not in result

        async def answer():
            while not any(f["type"] == "ACTION" for f in sent):
                await asyncio.sleep(0)
            action = next(f for f in sent if f["type"] == "ACTION")
            channel.on_action_result({"type": "ACTION_RESULT", "correlationId": action["messageId"], "payload": {"results": [{"id": 1, "success": True, "returnValue": {"url": "http://minio/screen.png", "width": 8, "height": 8}}]}})
        result, _ = await asyncio.gather(tools["capture_screen"].ainvoke({"step": 1, "thought": "TC evidence"}), answer())
        assert "The image follows." in result
        assert "then look once more" not in result
        assert "File each one" not in result
    asyncio.run(run())


@pytest.mark.parametrize("version", ["v30", "v39"])
@pytest.mark.parametrize("skills", ["off", "on_demand"])
@pytest.mark.parametrize("vision", [VisionMode.on, VisionMode.off])
def test_recorded_tools_and_fingerprint_match_the_selected_prompt(version, skills, vision):
    channel, arch, tools, _ = make(skills, vision, version)
    config = resolve_run_config(model="openai/gpt-6-luna", arch=QaArchSpec(vision=vision, skills=skills), prompt_version=version)
    actual = list(tools.values())
    if arch.compaction:
        actual.append(build_compact_tool(QaRunState(total_steps=2)))
    assert config.tools == [tool.name for tool in actual]
    assert config.agent_fingerprint == arch_fingerprint(arch, actual, middleware_names_for(arch))
    assert ("load_skill" in config.tools) == (skills == "on_demand" or (version == "v39" and vision == VisionMode.on))
