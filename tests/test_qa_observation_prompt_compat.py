"""구버전 prompt 고정 시에도 이 단계의 진단 tool을 구성한다."""
import pytest

from app.config import get_settings
from app.prompts import load_tool_description, roles_in
from tests.test_qa_game_errors import make


@pytest.mark.parametrize("version", ["v24", "v31"])
def test_pinned_prompt_keeps_diagnostic_tools(version, monkeypatch):
    monkeypatch.setattr(get_settings(), "qa_prompt_version", version)
    _, _, tools = make()
    available = roles_in("qa_run", version)
    errors_version = version if "tool_get_errors" in available else "v31"
    assert tools["get_errors"].description == load_tool_description("get_errors", errors_version).body
