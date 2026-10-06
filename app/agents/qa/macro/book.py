"""한 런의 macro 초안과 등록된 정의.

수명이 둘이다.

**초안은 런의 상태에 산다.** `content_map` 이 아니다. 등록하지 않은 초안은 런이 끝나면
사라지고, 그래도 되는 이유는 등록하지 않은 초안에는 남길 값이 없었다는 뜻이기 때문이다.
등록된 macro 를 고치려고 복사한 초안도 같다.

**등록된 정의는 `content_map` 에 산다.** `MACRO_REGISTER` 가 그것을 적고
`MACRO_READ` 가 다시 읽는다(`ARTEL-921`). 이 객체가 드는 것은 그 사본이다 — 이번 런이
등록한 것과, 이번 런이 [MacroBook.adopt] 로 가져온 지난 런의 것.

**사본을 두는 이유는 쓰기가 실패해도 런이 계속 가야 하기 때문이다.** 저쪽이 거절해도,
답이 안 와도, 이 프레임을 모르는 구버전 orchestration 이라도 `run_macro` 는 이번 런
안에서 그 macro 를 부를 수 있어야 한다. 그래서 [MacroBook.register] 는 저쪽의 답을 보지
않고, 프레임을 보내는 것은 `app/agents/qa/tools/macro_tools.py` 의 `register_macro` 다.

등록된 macro 를 고칠 때 초안으로 복사하는 이유는, 고치는 중에 그 macro 를 부르는 런이
깨지지 않게 하려는 것이다. 등록된 것은 `register` 가 성공할 때까지 멀쩡하고, 성공하면
같은 이름의 등록 행을 **제자리에서** 갱신한다. 지우고 새로 넣지 않는 이유는
`ON DELETE CASCADE` 로 `screen` 관계 행이 같이 사라지기 때문이다 — 그 관계는 agent 가
공들여 단 것이라 고칠 때마다 잃으면 안 된다.
"""

from dataclasses import dataclass, field, replace

from app.agents.qa.macro.model import MacroDefinition


@dataclass(frozen=True)
class RegisteredMacro:
    """부를 수 있게 된 macro 하나.

    `screens` 는 이 macro 를 어디서 쓰는지 적은 관계다. 등록하는 순간 agent 가 서 있는
    `screen` 과 agent 가 지목한 `screen` 만 단다 — selector 는 실행 시점에 풀리므로
    등록 시점에 어느 `screen` 에서 풀릴지 계산할 수 없다. 비어 있는 것은 아직 어디서
    쓸지 모른다는 뜻이고, 서 있는 `screen` 을 모르는 경우와 맞는다.
    """

    definition: MacroDefinition
    screens: tuple[str, ...] = ()


@dataclass
class MacroBook:
    """초안과 등록된 정의, 그리고 이 런이 무엇을 읽었나.

    `QaRunState` 가 이것을 한 칸으로 든다. 사전 셋을 거기 흩뿌리지 않는 이유는 초안의
    수명주기(쓰고·읽고·고치고·등록한다)가 여기서 단위 테스트로 재어지기 때문이다 —
    tool closure 안에 두면 tool 을 통해서만 재어진다.
    """

    drafts: dict[str, str] = field(default_factory=dict)
    registrations: dict[str, RegisteredMacro] = field(default_factory=dict)
    # 이 런에서 `read_macro` 로 읽은 이름. `edit_macro` 가 요구한다 — 고치려면 먼저
    # 읽어야 하고, 안 읽고 고치는 것은 무엇을 지우는지 모르고 지우는 것이다.
    read_names: set[str] = field(default_factory=set)

    # -- 읽기 --

    def draft(self, name: str) -> str | None:
        return self.drafts.get(name)

    def registered(self, name: str) -> RegisteredMacro | None:
        return self.registrations.get(name)

    def source(self, name: str) -> str | None:
        """같은 이름의 초안이 있으면 초안, 없으면 등록된 것의 원문."""
        if name in self.drafts:
            return self.drafts[name]
        found = self.registrations.get(name)
        return None if found is None else found.definition.source

    def remember_read(self, name: str) -> None:
        self.read_names.add(name)

    def was_read(self, name: str) -> bool:
        return name in self.read_names

    # -- 쓰기 --

    def write(self, name: str, source: str) -> None:
        """초안을 만들거나 통째로 바꾼다. 등록된 행은 건드리지 않는다."""
        self.drafts[name] = source

    def register(
        self, definition: MacroDefinition, screens: tuple[str, ...]
    ) -> RegisteredMacro:
        """검사를 통과한 정의를 이 런에서 부를 수 있게 한다.

        **여기서 아무 프레임도 안 나간다.** `content_map` 에 적는 것은
        `macro_tools.register_macro` 가 `MACRO_REGISTER` 로 하고, 이 메서드는 그 결과와
        무관하게 돈다 — 쓰기가 거절당하거나 답이 안 와도 런 안에서는 계속 부를 수 있어야
        한다.

        같은 이름의 행이 이미 있으면 제자리에서 갱신하고 기존 관계를 남긴다. `screens`
        는 기존 관계에 더한다 — 빼는 길은 v1 에 없다. 저쪽의 upsert 도 같은 규칙이라
        `screen_macro` 행이 갱신에서 살아남는다.
        """
        existing = self.registrations.get(definition.name)
        if existing is None:
            self.registrations[definition.name] = RegisteredMacro(
                definition=definition, screens=_merged((), screens)
            )
        else:
            self.registrations[definition.name] = replace(
                existing,
                definition=definition,
                screens=_merged(existing.screens, screens),
            )
        # 등록이 끝나면 초안은 할 일을 다했다. 남겨 두면 `read_macro` 가 등록된 것과 같은
        # 글을 초안이라고 돌려주고, agent 는 아직 등록 안 된 것으로 읽는다.
        self.drafts.pop(definition.name, None)
        return self.registrations[definition.name]

    def adopt(
        self, definition: MacroDefinition, screens: tuple[str, ...]
    ) -> RegisteredMacro:
        """`content_map` 에 저장돼 있던 정의를 이 런의 책에 들인다.

        [register] 와 가르는 이유는 둘이 서로 반대 방향이기 때문이다. [register] 는 이
        런이 적은 것이고 뒤이어 `MACRO_REGISTER` 가 나간다. 이쪽은 `MACRO_READ` 가
        가져온 것이고 아무것도 내보내지 않는다 — 방금 읽은 것을 다시 적으면 저쪽의
        `updated_at` 이 읽기만 한 런 때문에 움직인다.

        초안을 안 지운다. 저쪽에 저장된 것을 가져오는 것이 이 런이 쓰던 초안을 버릴
        이유가 되지 않는다. 같은 이름에 초안과 등록이 함께 있을 때 `run_macro` 가 등록된
        쪽을 부르는 것은 [register] 때와 같다.

        `screens` 는 저쪽이 돌려준 `screen_ids` 그대로다. 이 런이 거기에 더하는 것은
        `register_macro` 를 다시 부를 때다.
        """
        self.registrations[definition.name] = RegisteredMacro(
            definition=definition, screens=_merged((), screens)
        )
        return self.registrations[definition.name]


def _merged(existing: tuple[str, ...], added: tuple[str, ...]) -> tuple[str, ...]:
    """기존 관계에 더한다. 순서를 지키고 중복은 접는다."""
    merged = list(existing)
    for screen in added:
        if screen and screen not in merged:
            merged.append(screen)
    return tuple(merged)
