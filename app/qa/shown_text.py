"""게임이 화면에 띄운 글자를 도착 순서대로 적어 두는 기록 (ARTEL-1222).

`PulseMemory` 는 객체마다 마지막 글자만 쥔다. 사람이 한 줄씩 읽는 동안은 그것으로 충분하지만,
macro 가 키를 스무 번 누르는 동안 지나간 대사는 마지막 한 줄 말고는 아무도 읽지 않는다.
2026-10-22 측정에서 오프닝 한 줄을 `[MISSING TEXT: STORY_003]` 로 바꾼 빌드를 3런 중 0런이
찾았다 — `advance_story` 가 Return 을 12~22회 누르고 넘어갔다.

`pulse` 의 `text` 는 바뀐 것만 실린다. 그래서 도착한 `text` 하나하나가 화면에 뜬 글자 한 번이다.
타자 효과로 같은 객체의 글자가 앞부분을 그대로 둔 채 길어지면 새 줄로 세지 않고 그 줄을 늘린다.
"""

from dataclasses import dataclass

from app.qa.pulse import PulseReading

# 오래된 것부터 버린다. macro 하나가 지나가는 대사보다 넉넉하면 된다.
MAX_SHOWN_TEXT_ENTRIES = 2_000


@dataclass
class ShownText:
    """객체 하나가 띄운 글자 한 줄."""

    source: str
    text: str
    # 이 줄을 담은 객체의 key (`scene/selector`). 타자 효과로 늘어나는 줄을 찾는 데 쓴다.
    key: str


@dataclass
class _ShownTextEntry(ShownText):
    revision: int


class ShownTextLog:
    """런 하나 동안 `pulse` 가 실어 온 글자. 위치를 표시해 두고 그 뒤의 것만 꺼낸다."""

    def __init__(self) -> None:
        self._entries: list[_ShownTextEntry] = []
        # 기존 줄의 typewriter 변경도 cursor 이후의 도착으로 기록한다.
        self._revision = 0
        # 객체마다 지금 띄운 글자.
        self._current: dict[str, str] = {}

    def mark(self) -> int:
        """지금까지의 변경 cursor. `since`에 넘긴다."""
        return self._revision

    def apply(self, reading: PulseReading) -> None:
        for arrived in reading.active:
            obj = arrived.flatten(reading.scene)
            if obj.text is None:
                continue
            key = obj.key
            before = self._current.get(key)
            self._current[key] = obj.text
            if before == obj.text:
                continue
            self._revision += 1
            if before and obj.text.startswith(before) and self._extend(key, obj.text):
                continue
            name = (obj.path or obj.selector or key).rstrip("/").rsplit("/", 1)[-1]
            self._entries.append(_ShownTextEntry(source=name, text=obj.text, key=key, revision=self._revision))
        if len(self._entries) > MAX_SHOWN_TEXT_ENTRIES:
            cut = len(self._entries) - MAX_SHOWN_TEXT_ENTRIES
            del self._entries[:cut]

    def _extend(self, key: str, text: str) -> bool:
        """같은 객체의 가장 최근 줄을 늘린다. 그 줄이 없으면(버려졌으면) `False`."""
        for entry in reversed(self._entries):
            if entry.key == key:
                entry.text = text
                entry.revision = self._revision
                return True
        return False

    def since(self, mark: int) -> list[ShownText]:
        """`mark` 뒤에 생성되거나 늘어난 줄. 같은 연속 줄은 한 번만 센다."""
        lines: list[ShownText] = []
        for entry in self._entries:
            if entry.revision <= mark:
                continue
            if lines and lines[-1].source == entry.source and lines[-1].text == entry.text:
                continue
            lines.append(ShownText(source=entry.source, text=entry.text, key=entry.key))
        return lines
