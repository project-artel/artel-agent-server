"""이전 QA prompt를 고정해도 진단 도구를 조립한다."""
import pytest

from app.config import get_settings
from app.prompts import load_tool_description
from tests.test_qa_game_errors import make


@pytest.mark.parametrize("version", ["v24", "v26", "v27", "v28", "v29", "v30"])
def test_pinned_prompt_keeps_diagnostic_tools(version, monkeypatch):
    monkeypatch.setattr(get_settings(), "qa_prompt_version", version)
    _, _, tools = make()
    errors_version = "v27" if int(version[1:]) < 27 else version
    performance_version = "v29" if int(version[1:]) < 29 else version
    assert tools["get_errors"].description == load_tool_description("get_errors", errors_version).body
    assert tools["get_performance"].description == load_tool_description("get_performance", performance_version).body
