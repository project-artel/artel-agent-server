"""게임이 낸 예외를 모아 두고, agent 가 아직 안 본 것만 세는 장부 (ARTEL-1219).

SDK 는 `PULSE` 와 `ACTION_RESULT` 의 `errors` 목록에 예외 항목을 싣는다 (ARTEL-1218, artel-sdk
PR #154). 한 항목은 `{type, message, stackHead, firstFrame, lastFrame, count, truncated}` 이고,
한 frame 에는 새로 생겼거나 `count` 가 늘어난 항목만 최대 20개 실린다. 20개를 넘는 것은 버리지
않고 다음 `pulse` 로 넘긴다. `truncated` 는 항목 하나의 `message` 가 200자에서 잘렸다는 뜻이다.
SDK 는 main thread 의 log 만 모으고, 전달에 실패한 `pulse` 에 실렸던 항목은 다시 보내지 않는다 —
그래서 이 장부는 게임이 낸 예외 전부가 아니라 이 서버에 도착한 것이다.

전부를 매 turn context 에 넣지 않는다. gyemong 은 scan 만 돌려도 게임 자체 예외가 2종 나고,
luna 계열 모델은 L1 런 170개 중 169개에 오탐이 있었다 (2026-10-22 보고서 6절, 7절). 그래서
tool 결과마다 `unseen errors: N` 한 줄만 붙이고, 내용은 `get_errors` 로 펼쳐 본다.

같은 항목은 `(type, message, stackHead)` 로 가른다. 한 번 본 항목은 그 뒤 `count` 가 늘어도
다시 세지 않는다 — 매 frame 같은 예외를 내는 게임에서 N 이 0 으로 돌아오지 않으면 그 줄은
아무것도 말하지 않게 된다. 기준선은 두지 않는다. 런 시작 전부터 나던 예외도 처음 한 번은 센다.

SDK 자신이 남긴 `[Artel]` Error log 도 같은 목록에 온다. 그것은 게임의 결함이 아니므로 N 에
넣지 않고, `get_errors` 가 따로 묶어 SDK 가 낸 것이라고 적는다.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# SDK 가 `message` 를 자르는 길이 (ARTEL-1218). agent 에게 잘렸다고 말할 때만 쓴다.
SDK_MESSAGE_LIMIT_CHARS = 200
# SDK 가 자기 log 앞에 붙이는 머리말.
SDK_LOG_PREFIX = "[Artel]"


class GameErrorItem(BaseModel):
    """SDK 가 보낸 예외 항목 하나. 필드 이름은 wire 의 camelCase 그대로다."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    type: str = ""
    message: str = ""
    stack_head: str = Field(default="", alias="stackHead")
    first_frame: int | None = Field(default=None, alias="firstFrame")
    last_frame: int | None = Field(default=None, alias="lastFrame")
    count: int = 1
    # `message` 가 200자에서 잘렸는가.
    truncated: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.type, self.message, self.stack_head)

    @property
    def from_sdk(self) -> bool:
        """ARTEL SDK 자신이 남긴 log 인가. 게임의 결함이 아니다."""
        return self.message.lstrip().startswith(SDK_LOG_PREFIX)


class _LedgerEntry(BaseModel):
    item: GameErrorItem
    seen: bool = False


def errors_in(payload: Any) -> list[GameErrorItem]:
    """frame payload 에서 예외 항목을 꺼낸다. 없으면 빈 목록.

    모양이 틀린 항목 하나는 버리고 나머지는 받는다 — 예외 하나를 못 읽었다고 판독 전체를 잃으면
    안 된다.
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("errors")
    if not isinstance(raw, list):
        return []
    items: list[GameErrorItem] = []
    for entry in raw:
        try:
            items.append(GameErrorItem.model_validate(entry))
        except ValidationError:
            continue
    return items


class GameErrorLedger:
    """런 하나가 받은 예외 항목과, 그중 agent 가 `get_errors` 로 본 것."""

    def __init__(self) -> None:
        # 도착 순서를 지킨다. `get_errors` 가 먼저 생긴 것부터 보여 준다.
        self._entries: dict[tuple[str, str, str], _LedgerEntry] = {}

    def apply(self, payload: Any) -> None:
        """`PULSE` 나 `ACTION_RESULT` 의 payload 하나를 받는다."""
        for item in errors_in(payload):
            entry = self._entries.get(item.key)
            if entry is None:
                self._entries[item.key] = _LedgerEntry(item=item)
                continue
            # 본 항목의 `count` 가 늘어도 `seen` 은 그대로다. 숫자만 최신으로 둔다.
            previous = entry.item
            entry.item = item.model_copy(
                update={
                    "first_frame": previous.first_frame
                    if previous.first_frame is not None
                    else item.first_frame,
                    "count": max(previous.count, item.count),
                }
            )

    def unseen_count(self) -> int:
        """안 본 게임 예외 수. SDK 자신의 log 는 세지 않는다."""
        return sum(
            1 for entry in self._entries.values() if not entry.seen and not entry.item.from_sdk
        )

    def seen_count(self) -> int:
        return sum(1 for entry in self._entries.values() if entry.seen)

    def take(self, include_seen: bool = False) -> list[GameErrorItem]:
        """안 본 항목(또는 전부)을 돌려주고, 돌려준 것을 본 것으로 표시한다."""
        taken: list[GameErrorItem] = []
        for entry in self._entries.values():
            if entry.seen and not include_seen:
                continue
            entry.seen = True
            taken.append(entry.item)
        return taken

    def summary_line(self) -> str | None:
        """tool 결과 끝에 붙는 한 줄. 안 본 항목이 없으면 `None` 이라 줄이 생략된다."""
        unseen = self.unseen_count()
        if unseen == 0:
            return None
        return f"unseen errors: {unseen}"


def _item_lines(item: GameErrorItem) -> list[str]:
    frames = ""
    if item.first_frame is not None:
        frames = f", first frame {item.first_frame}"
        if item.last_frame is not None and item.last_frame != item.first_frame:
            frames += f", last frame {item.last_frame}"
    cut = f" [message cut at {SDK_MESSAGE_LIMIT_CHARS} chars]" if item.truncated else ""
    lines = [f"- {item.type or 'Exception'}: {item.message}{cut} (×{item.count}{frames})"]
    if item.stack_head:
        lines.append(f"    at {item.stack_head}")
    return lines


def render_errors(items: list[GameErrorItem], ledger: GameErrorLedger, include_seen: bool) -> str:
    """`get_errors` 의 답."""
    game = [item for item in items if not item.from_sdk]
    sdk = [item for item in items if item.from_sdk]
    if not items:
        seen = ledger.seen_count()
        if seen and not include_seen:
            return (
                f"No unseen errors. {seen} error(s) you have already read are not repeated; "
                "`include_seen=true` lists them again."
            )
        return "The game has reported no errors this run."
    lines: list[str] = []
    if game:
        what = "error(s)" if include_seen else "unseen error(s), now marked seen"
        lines.append(f"{len(game)} {what}:")
        for item in game:
            lines.extend(_item_lines(item))
    if sdk:
        lines.append(
            f"{len(sdk)} log(s) from the ARTEL SDK itself, not from the game — not a game "
            "defect, and not counted in `unseen errors`:"
        )
        for item in sdk:
            lines.extend(_item_lines(item))
    lines.append(
        "An error does not decide a step: judge the step by its expected result, and file a "
        "real defect with `report_issue` — say so if it looks like the game's own known noise."
    )
    return "\n".join(lines)
