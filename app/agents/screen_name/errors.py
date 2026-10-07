class ScreenNameError(RuntimeError):
    """모델이 형식을 어겨 이름을 읽을 수 없다.

    부르는 쪽은 이것을 **이름 없는 답**으로 옮긴다(`app/qa/screen_name.py`). 스키마를
    채우려고 이름을 지어내지 않는다 — 이름 없는 `screen` 은 content map 에서 그냥 이름이
    없을 뿐이지만, 지어낸 이름은 사람이 그 화면을 계속 잘못 찾게 만든다.
    """
