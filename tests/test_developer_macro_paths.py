"""`selector` 가 게임이 실제로 보여 주는 object 를 겨누는지 QA 런 `pulse` 로 보는 검사."""

import pytest

from app.agents.qa.macro.errors import MacroRejection
from app.developer_macro_paths import reject_rarely_shown_selectors

# WordVenture build 4 의 최근 QA 런 10개에서 센 값(로컬 try 194 까지).
OBSERVED = {
    "TutorialController/ChatWindow/DialogueWindow/DialoguePanel/ChatText": 2406,
    "Canvas/ChatWindow/DialogueWindow/DialoguePanel/ChatText": 3000,
    "TutorialController/InputBlocker": 400,
}

DISMISS = '''def dismiss_dialogue() -> None:
    """Advances the tutorial line."""
    if exists(selector("TutorialController/ChatWindow/ChatText")):
        press_key("Space", 0.1)
'''


def test_a_path_the_game_almost_never_showed_is_refused_with_the_one_it_did_show():
    with pytest.raises(MacroRejection) as refused:
        reject_rarely_shown_selectors(DISMISS, OBSERVED)

    assert refused.value.line == 3
    reason = refused.value.render()
    assert "`TutorialController/ChatWindow/DialogueWindow/DialoguePanel/ChatText` (2406)" in reason
    # root 가 다른 같은 이름은 대안이 아니다.
    assert "Canvas/ChatWindow" not in reason


def test_indices_and_a_leading_scene_name_are_ignored_and_a_shown_path_passes():
    source = DISMISS.replace(
        "TutorialController/ChatWindow/ChatText",
        "TurnBattleScene/TutorialController[6]/ChatWindow[1]/DialogueWindow[0]/DialoguePanel[2]/ChatText[1]",
    )

    reject_rarely_shown_selectors(source, OBSERVED)


def test_without_runs_or_without_a_far_more_common_alternative_nothing_is_refused():
    reject_rarely_shown_selectors(DISMISS, {})
    reject_rarely_shown_selectors(DISMISS, None)
    # 한 번도 안 보였어도 같은 이름의 대안이 없으면 아직 아무 런도 안 간 화면일 수 있다.
    reject_rarely_shown_selectors(DISMISS.replace("ChatText", "Portrait"), OBSERVED)
    # 대안도 몇 번 안 보였으면 근거가 아니다.
    reject_rarely_shown_selectors(DISMISS, {"TutorialController/ChatWindow/DialogueWindow/ChatText": 5})
