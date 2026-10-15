"""func 의 `run` macro 를 파싱한다: tool loop 가 macro 를 저장할 때와 같은 검사로.

step module 은 func 마다 `run_<name> = '''def run_<name>() -> None: ...'''` 를 들고, `FUNCS` 가 그
이름을 `when` 옆에 둔다. 그 글은 `macro_definition_from_source` 가 tool loop 의 `write_macro` 와
같이 읽는다: `from macros import ...` 로 가져오는 이름은 이 build 의 `content_map` 에 등록된
macro(`MACRO_READ`)에서 먼저 찾아 넘기고, 새로 쓰는 글이므로 `#id`·`-`·빈 `selector` 도 거절한다
(`reject_unstable_selectors`). 거절 문장은 planner 가 다시 쓰라는 feedback 으로 읽는다.

macro package 는 고치지 않는다. 가져올 macro 를 찾는 길은 `app/agents/qa/tools/macro_tools.py`
의 `_parsed`·`_stored` 와 같은 순서이고, 이 engine 은 macro 를 등록하지 않으므로 런의
`MacroBook` 대신 읽은 것만 여기 들고 있다.
"""

from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.model import MacroDefinition
from app.agents.qa.macro.parser import imported_names, macro_definition_from_source
from app.agents.qa.macro.selector_check import reject_unstable_selectors
from app.qa.channel import KnowledgeRequestFailed, QaCancelled, QaRunChannel
from app.qa.envelope import MacroReadPayload

RUN_PREFIX = "run_"


def entry_name(func: str) -> str:
    """func `name` 의 macro 가 진입점으로 삼는 `def` 이름."""
    return f"{RUN_PREFIX}{func}"


class RegisteredMacros:
    """`from macros import` 가 가리키는 이 build 의 등록된 macro. 한 run 에서 한 번씩만 묻는다."""

    def __init__(self, channel: QaRunChannel) -> None:
        self._channel = channel
        # 이름 → 파싱된 정의, 또는 못 가져온 이유.
        self._found: dict[str, MacroDefinition | str] = {}

    async def definition(
        self, name: str, importing: tuple[str, ...] = ()
    ) -> tuple[MacroDefinition | None, str]:
        """등록된 macro 하나와, 못 찾았으면 그 이유."""
        known = self._found.get(name)
        if isinstance(known, MacroDefinition):
            return known, ""
        if isinstance(known, str):
            return None, known
        try:
            answer = await self._channel.read_macro(MacroReadPayload(name=name))
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - 조회 하나로 런이 끝나면 안 된다
            return None, f"The content map could not be asked — {error}."
        if isinstance(answer, KnowledgeRequestFailed):
            problem = f"The content map does not have it — {answer.reason}."
        elif answer is None:
            problem = "The content map did not answer."
        else:
            try:
                definition = await self.parse(answer.name or name, answer.source, importing)
            except MacroRejection as rejection:
                problem = f"It is registered but no longer parses: {rejection.render()}"
            else:
                self._found[name] = definition
                return definition, ""
        self._found[name] = problem
        return None, problem

    async def parse(
        self, name: str, source: str, importing: tuple[str, ...] = ()
    ) -> MacroDefinition:
        """`source` 를 `name` 이 진입점인 macro 로 파싱한다. 못 하면 `MacroRejection`."""
        imports: dict[str, MacroDefinition] = {}
        for imported in imported_names(source):
            if imported == name or imported in importing:
                chain = " → ".join((*importing, name, imported))
                raise MacroRejection(
                    f"`{imported}` cannot be imported here, because the imports go round in "
                    f"a circle: {chain}."
                )
            definition, problem = await self.definition(imported, (*importing, name))
            if definition is None:
                raise MacroRejection(
                    f"`{imported}` is imported but cannot be found. {problem} Import only "
                    "macros registered in this build, or write the steps yourself."
                )
            imports[imported] = definition
        return macro_definition_from_source(name, source, imports=imports)


async def parse_run(func: str, source: str, registered: RegisteredMacros) -> MacroDefinition:
    """func `func` 의 `run` macro. 못 쓰는 글이면 `MacroRejection`.

    tool loop 의 쓰는 길과 같은 검사에, 이 engine 의 것 하나를 더한다: executer 는 진입점을 인자
    없이 부르므로 parameter 를 받는 진입점은 거절한다.
    """
    name = entry_name(func)
    definition = await registered.parse(name, source)
    reject_unstable_selectors(source)
    if definition.entry.parameters:
        raise MacroRejection(
            f"`def {name}` takes parameters, and the executer calls it with none. Find what it "
            "acts on inside the macro (`find(...)`, `selector(...)`) and write `def "
            f"{name}() -> None:`."
        )
    return definition
