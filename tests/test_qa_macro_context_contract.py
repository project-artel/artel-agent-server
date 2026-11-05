"""고정된 옛 prompt로 실행해도 현재 macro 결과 계약을 읽는다."""

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec
from app.qa.run_config import resolve_run_config
from tests import test_qa_runner_context as runner_context


def test_legacy_prompt_is_followed_by_the_current_macro_result_contract(monkeypatch):
    config = resolve_run_config(arch=QaArchSpec(phase_cycle=PhaseCycleMode.off, macros="on"), prompt_version="v28")
    monkeypatch.setattr(runner_context, "resolve_run_config", lambda **_: config)
    model, _, _ = runner_context.scripted_run(monkeypatch, observations=1)
    system = "\n".join(str(message.content) for message in model.received[0] if message.type == "system")
    old_instruction = "The answer lists the text the game showed while the macro ran"
    contract = "Current macro contract: intermediate dialogue history is not collected or returned."
    assert old_instruction in system
    assert contract in system
    assert system.rfind(contract) > system.find(old_instruction)
