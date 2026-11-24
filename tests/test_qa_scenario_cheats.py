from app.qa.scenario_cheats import ALREADY_RAN, cheats_in, only_cheats, with_cheats_done


def test_호출만_있는_단계는_엔진이_끝낸다():
    text = "게임 기능 set_progress(resumeStage=1, unlockedStage=24) 로 모든 스테이지를 연 진행 상태를 저장한다"
    cheats = cheats_in(text)
    assert [(c.name, c.args) for c in cheats] == [("set_progress", {"resumeStage": 1, "unlockedStage": 24})]
    assert only_cheats(text, cheats)
    assert only_cheats("게임 기능 go_to_stage(stage=24) 로 Stage 24 를 연다", cheats_in("게임 기능 go_to_stage(stage=24) 로 Stage 24 를 연다"))


def test_호출_뒤에_조작이_이어지면_나머지는_planner_가_쓴다():
    text = "게임 기능 go_to_stage(stage=10) 으로 Stage 10 을 연 뒤, 지지를 클릭해 깜짝 놀란다 아이콘을 놓는다"
    cheats = cheats_in(text)
    assert not only_cheats(text, cheats)
    assert f"go_to_stage(stage=10){ALREADY_RAN}" in with_cheats_done(text, cheats)


def test_게임_초기화도_읽는다():
    text = "게임 초기화(reset_game)로 게임을 다시 열고, 로비가 나오면 Play 이모지를 놓는다"
    cheats = cheats_in(text)
    assert [c.name for c in cheats] == ["reset_game"]
    assert not only_cheats(text, cheats)


def test_호출이_없는_단계는_그대로다():
    assert cheats_in("리셋 버튼을 눌러 플레이 전 상태로 돌린다") == []
