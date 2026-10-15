"""A stand-in for the game behind orchestration, for planner–executer tests without Unity.

It answers each `ACTION` frame the way the SDK does through orchestration: an `ACTION_RESULT`
correlated to the frame's `messageId` and stamped with the frame the batch finished on, then a
`PULSE` reading taken after that frame when something moved. `MACRO_READ` 에는 `macros` 에 등록해
둔 글로 답하고, 없는 이름은 orchestration 처럼 `ERROR` 로 거절한다.
"""

import asyncio
from collections.abc import Callable
from typing import Any

from app.qa.channel import QaRunChannel


class FakeGame:
    def __init__(self, scene: str, objects: dict[str, dict[str, Any]]) -> None:
        self.scene = scene
        # selector -> {"text": ..., "rect": ..., "members": {"Comp.member": value}}
        self.objects = objects
        self.frame = 100
        self.reading = 0
        self.sent: list[dict] = []
        self.actions: list[tuple[str, list]] = []
        # (method, first param) -> reaction that changes this game. ("press", selector) reacts
        # to a press aimed at that selector.
        self.reactions: dict[tuple[str, Any], Callable[["FakeGame"], None]] = {}
        self.refuse: set[tuple[str, Any]] = set()
        self._aim: Any = None
        # macro 이름 → 이 build 에 등록된 글. `MACRO_READ` 가 읽는다.
        self.macros: dict[str, str] = {}
        # 움직이지 않은 action 뒤에 `pulse` 를 기다리는 시간. 진짜 값(1.5초)이면 화면을 바꾸지 않는
        # action 마다 그만큼 기다린다.
        self.channel = QaRunChannel(
            qa_try_id=7, send=self.send, action_timeout=2.0, reading_wait=0.05
        )

    async def send(self, frame: dict) -> None:
        """What the channel calls to send a frame out."""
        self.sent.append(frame)
        if frame["type"] == "ACTION":
            # Answer on the next loop turn, as a socket would.
            asyncio.get_running_loop().call_soon(self._answer, frame)
        elif frame["type"] == "MACRO_READ":
            name = frame["payload"]["name"]
            if name in self.macros:
                asyncio.get_running_loop().call_soon(
                    self.channel.on_macro_read_result,
                    {
                        "type": "MACRO_READ_RESULT",
                        "correlationId": frame["messageId"],
                        "payload": {"type": "MACRO_READ", "name": name, "source": self.macros[name]},
                    },
                )
            else:
                asyncio.get_running_loop().call_soon(
                    self.channel.on_error,
                    {
                        "type": "ERROR",
                        "correlationId": frame["messageId"],
                        "payload": {"message": f"MACRO_READ references an unknown macro: {name}"},
                    },
                )

    def _answer(self, frame: dict) -> None:
        results = []
        moved = False
        for action in frame["payload"]["actions"]:
            method, params = action["method"], action["params"]
            first = params[0] if params else None
            self.actions.append((method, params))
            if (method, first) in self.refuse:
                results.append(
                    {"id": action["id"], "success": False, "error": f"Nothing at selector: {first}"}
                )
                continue
            if method == "move_mouse" and isinstance(first, str) and first not in self.objects:
                results.append({"id": action["id"], "success": False, "error": f"no object {first}"})
                continue
            if method == "move_mouse":
                self._aim = first
            if method == "mouse_down":
                aim = self._aim if isinstance(self._aim, str) else "point"
                reaction = self.reactions.get(("press", aim)) or self.reactions.get((method, first))
                if reaction is not None:
                    reaction(self)
                    moved = True
                results.append(
                    {
                        "id": action["id"],
                        "success": True,
                        "returnValue": {"reached": str(self._aim), "pointerHeldByPerson": False},
                    }
                )
                continue
            reaction = self.reactions.get((method, first))
            if reaction is not None:
                reaction(self)
                moved = True
            results.append({"id": action["id"], "success": True})
        self.frame += 5
        self.channel.on_action_result(
            {
                "type": "ACTION_RESULT",
                "correlationId": frame["messageId"],
                "payload": {"results": results, "frame": self.frame},
            }
        )
        if moved:
            self.push_reading()

    def push_reading(self) -> None:
        self.reading += 1
        self.frame += 1
        self.channel.on_pulse(
            {
                "type": "PULSE",
                "payload": {
                    "schema": 2,
                    "reading": self.reading,
                    "frame": self.frame,
                    "scene": self.scene,
                    "whole": True,
                    "active": [
                        {
                            "selector": selector,
                            "path": selector,
                            "text": body.get("text"),
                            "rect": body.get("rect"),
                            "members": [
                                {
                                    "on": name.split(".")[0],
                                    "member": name.split(".")[1],
                                    "value": value,
                                }
                                for name, value in body.get("members", {}).items()
                            ],
                        }
                        for selector, body in self.objects.items()
                    ],
                    "changed": [],
                },
            }
        )

    def frames(self, kind: str) -> list[dict]:
        return [frame for frame in self.sent if frame["type"] == kind]
