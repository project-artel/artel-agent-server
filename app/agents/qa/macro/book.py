"""한 런의 macro 초안과 등록된 정의.

수명이 둘이다.

**초안은 런의 상태에 산다.** `content_map` 이 아니다. 등록하지 않은 초안은 런이 끝나면
사라지고, 그래도 되는 이유는 등록하지 않은 초안에는 남길 값이 없었다는 뜻이기 때문이다.
등록된 macro 를 고치려고 복사한 초안도 같다.

**등록된 정의는 `content_map` 에 산다 — 아직은 아니다.** 그것을 적을 frame 이
`artel-orchestration-server` 에 아직 없다(`ARTEL-921`, `ARTEL-919`). 그래서 지금은
`register` 가 이 객체에 넣고, 런이 끝나면 함께 사라진다. **그 자리는 `register` 하나다.**
frame 이 생기면 이 메서드 하나만 바뀌고 tool 다섯은 그대로다.

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

    def start_draft_from_registered(self, name: str) -> str | None:
        """등록된 것을 초안으로 복사한다. 이미 초안이 있으면 그것을 그대로 쓴다."""
        if name in self.drafts:
            return self.drafts[name]
        found = self.registrations.get(name)
        if found is None:
            return None
        self.drafts[name] = found.definition.source
        return self.drafts[name]

    def register(
        self, definition: MacroDefinition, screens: tuple[str, ...]
    ) -> RegisteredMacro:
        """검사를 통과한 정의를 부를 수 있게 한다.

        **좁혀 둔 저장 자리다.** `content_map` 에 macro 행과 `screen` 관계 행을 적는
        것이 여기 들어온다(`ARTEL-919`, `ARTEL-921`). 그 frame 이 아직 없으므로 지금은
        런의 상태에만 남고, 런이 끝나면 함께 사라진다.

        같은 이름의 행이 이미 있으면 제자리에서 갱신하고 기존 관계를 남긴다. `screens`
        는 기존 관계에 더한다 — 빼는 길은 v1 에 없다.
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


def _merged(existing: tuple[str, ...], added: tuple[str, ...]) -> tuple[str, ...]:
    """기존 관계에 더한다. 순서를 지키고 중복은 접는다."""
    merged = list(existing)
    for screen in added:
        if screen and screen not in merged:
            merged.append(screen)
    return tuple(merged)
