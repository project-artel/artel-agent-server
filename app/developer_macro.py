"""게임 개발자가 MCP 로 쓰는 developer macro 의 문법 글과 검사 (ARTEL-1087).

QA 런의 tool loop 와 같은 parser 를 쓴다. 다른 점은 import 를 어디서 찾느냐 하나다.
tool loop 는 이 런의 책과 `content_map` 에 묻고, 여기는 orchestration 이 같은 프로젝트의
developer macro source 를 모아 요청에 실어 준 `imports` 만 본다. 이 모듈은 아무 데도
묻지 않는 순수 함수다.

`app/agents/qa/macro/` 를 고치지 않는다. QA 런이 읽는 문법과 developer macro 가 읽는
문법이 갈라지면 같은 macro 가 한쪽에서만 통과한다.
"""

import ast
from collections.abc import Mapping

from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.model import MacroDefinition
from app.agents.qa.macro.parser import imported_names, macro_definition_from_source
from app.agents.qa.macro.selector_check import reject_unstable_selectors
from app.developer_macro_grammar import DEVELOPER_GRAMMAR
from app.developer_macro_keys import reject_unknown_key_codes
from app.developer_macro_paths import reject_rarely_shown_selectors

# orchestration `developer_macro.docstring` 의 상한. 검사를 통과한 macro 가 저장할 때
# 길이로 거절되면 안 되므로 같은 값을 여기서 먼저 건다.
MAX_DOCSTRING_LENGTH = 2_000

# 문법 글(`DEVELOPER_GRAMMAR`) 뒤에 붙는 검사 규칙 요약. MCP 서버가 "developer macro 규칙" 으로 싣는다.
DEVELOPER_MACRO_RULES = (
    "The entry `def` is named exactly like the macro and declared `-> None`.",
    "The entry `def` opens with a docstring saying what the macro does and when it is "
    "the right move. A macro without one is refused.",
    "`from macros import <name>` imports another developer macro of the same project: "
    "one already published there, or one sent in the same `publish_macros` call. Send "
    "related macros together in one call; their order inside it does not matter.",
    'A `selector("...")` must not be a `#id`, `-` or an empty string: a `#id` changes '
    "between game launches, and the other two name nothing.",
    "A `key_code` written as a string must be a Unity `KeyCode` name (`LeftShift`, not "
    "`shift`).",
    "A `selector(\"...\")` that recent QA runs on the build almost never saw live, while another "
    "path with the same name under the same root showed up far more, is refused with that path: "
    "aim at the object the game actually shows, which is not always where a prefab puts it.",
    "Publishing checks the source statically — parsing, the rules above, imports, and each "
    "action's argument count, order and type. Apart from the rule above it does not check that "
    "a selector or a label exists in the build, and it does not run the macro in the game, so "
    "passing does not mean it works there; a QA run finds that out.",
)


def developer_macro_grammar() -> str:
    return DEVELOPER_GRAMMAR


def check_developer_macro(
    name: str, source: str, imports: Mapping[str, str], observed: Mapping[str, int] | None = None
) -> MacroDefinition:
    """통과하면 정의를, 못 통과하면 `MacroRejection` 을 낸다. 저장하지 않는다.

    검사 순서는 tool loop 의 `register_macro` 와 같다 — 파싱, 불안정한 `selector`, entry
    docstring. docstring 길이 상한(`MAX_DOCSTRING_LENGTH`)과 `KeyCode` 이름 검사만 여기에 더 있다.

    가져온 macro 에는 `selector` 와 docstring 검사를 하지 않는다. 이미 publish 된 것이고,
    tool loop 도 저장된 macro 를 읽을 때는 그 두 검사를 끈다.
    """
    definition = _ImportResolver(imports).definition(name, source, ())
    reject_unstable_selectors(source)
    # `observed` 는 그 build 의 최근 QA 런 `pulse` 에서 경로마다 live 로 실린 reading 수다(`developer_macro_paths`).
    reject_rarely_shown_selectors(source, observed)
    reject_unknown_key_codes(source)
    if definition.entry.docstring is None:
        raise MacroRejection(
            f"the entry `def {name}` has no docstring. Add one as the first statement of "
            "that `def`, saying what the macro does and when it is the right move — it "
            "is the summary a planner reads to choose this macro.",
            _def_line(source, name),
        )
    if len(definition.entry.docstring) > MAX_DOCSTRING_LENGTH:
        raise MacroRejection(
            f"the docstring of `def {name}` is {len(definition.entry.docstring)} "
            f"characters, over the limit of {MAX_DOCSTRING_LENGTH}. It is a summary a "
            "planner reads to choose this macro; keep it to what the macro does and when "
            "it is the right move.",
            _def_line(source, name),
        )
    return definition


class _ImportResolver:
    """`from macros import ...` 를 요청의 `imports` 로 끝까지 푼다.

    푼 정의를 이름으로 기억한다. 두 macro 가 같은 macro 를 가져오는 모양이 겹치면, 기억하지
    않을 때 같은 source 를 겹친 수만큼 다시 파싱한다.
    """

    def __init__(self, sources: Mapping[str, str]) -> None:
        self._sources = sources
        self._definitions: dict[str, MacroDefinition] = {}

    def definition(
        self, name: str, source: str, importing: tuple[str, ...]
    ) -> MacroDefinition:
        resolved: dict[str, MacroDefinition] = {}
        for imported in imported_names(source):
            line = _import_line(source, imported)
            if imported == name or imported in importing:
                chain = " → ".join((*importing, name, imported))
                raise MacroRejection(
                    f"`{imported}` cannot be imported here, because the imports go round "
                    f"in a circle: {chain}. Call it directly instead of importing it.",
                    line,
                )
            if imported in self._definitions:
                resolved[imported] = self._definitions[imported]
                continue
            imported_source = self._sources.get(imported)
            if imported_source is None:
                raise MacroRejection(
                    f"`{imported}` is imported but this project has no developer macro "
                    "by that name. Publish it first — sending both in the same "
                    "`publish_macros` call is enough — or remove it from the import.",
                    line,
                )
            try:
                found = self.definition(imported, imported_source, (*importing, name))
            except MacroRejection as rejection:
                # 안쪽 줄 번호는 가져온 macro 의 source 기준이라 바깥 `line` 으로 쓸 수
                # 없다. 문장 안에 그대로 남기고, 바깥 줄은 import 한 자리를 가리킨다.
                raise MacroRejection(
                    f"`{imported}` is imported but is itself refused: {rejection.render()}",
                    line,
                ) from rejection
            self._definitions[imported] = found
            resolved[imported] = found
        return macro_definition_from_source(name, source, imports=resolved)


def _import_line(source: str, imported: str) -> int | None:
    # `imported_names` 가 이름을 냈다면 source 는 이미 파싱된다.
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == imported for alias in node.names
        ):
            return node.lineno
    return None


def _def_line(source: str, name: str) -> int | None:
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node.lineno
    return None
