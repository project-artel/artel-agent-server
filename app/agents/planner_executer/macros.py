"""The step source the planner writes, read with the macro parser.

The planner answers each step with one macro source and nothing else:

    def <action>() -> None:      # one or more: what to do, each with a docstring
        # docstring: what it does, and when it is the right move
        ...
    def done() -> bool:          # the step's goal, read from the screen
        ...

Every `-> None` def is an action Jev may pick; its docstring is the option's description. `done`
is a predicate. The text is read by `macro_definition_from_source`, the same parser the tool loop
uses when a macro is written, once per action with that action as the entry. Imports
(`from macros import ...`) are resolved against the macros registered for this build, asked for
with `MACRO_READ` the way the tool loop's `_parsed` asks. The macro package itself is not changed.

`done` is a predicate, and the runner only runs an entry that acts. To read it on its own the
source is parsed once more with a generated entry, `executer_probe_done`, that does nothing but
`if done(): flag("done")`; the executer runs that entry and looks for the flag.
"""

import ast
from dataclasses import dataclass

from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.model import MacroDefinition
from app.agents.qa.macro.parser import imported_names, macro_definition_from_source
from app.agents.qa.macro.selector_check import reject_unstable_selectors
from app.qa.channel import KnowledgeRequestFailed, QaCancelled, QaRunChannel
from app.qa.envelope import MacroReadPayload

DONE = "done"
PROBE = "executer_probe_done"
PROBE_FLAG = "done"
_PROBE_SOURCE = f'\n\ndef {PROBE}() -> None:\n    if {DONE}():\n        flag("{PROBE_FLAG}")\n'


@dataclass(frozen=True)
class StepMacros:
    """One step's macros, parsed: the actions in the order written, and the `done` probe."""

    source: str
    actions: dict[str, MacroDefinition]
    probe: MacroDefinition

    def description(self, action: str) -> str:
        return self.actions[action].entry.docstring or ""


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


async def parse_step(source: str, registered: RegisteredMacros) -> StepMacros:
    """The step's macros, or `MacroRejection` with what to write instead.

    The parser checks each action and `done`; this adds what the executer needs of the source as
    a whole — a `done` predicate, at least one action, no parameters, a docstring on each action.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise MacroRejection(f"this is not valid Python syntax — {error.msg}.", error.lineno) from None
    defs = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if PROBE in {node.name for node in defs}:
        raise MacroRejection(f"`{PROBE}` is a name the executer uses; name the def otherwise.")
    goal = next((node for node in defs if node.name == DONE), None)
    if goal is None or not _returns(goal, "bool") or goal.args.args:
        raise MacroRejection(
            "the source needs the step's goal as `def done() -> bool:` with no parameters — a "
            "predicate that returns True once the step's goal is reached on the screen."
        )
    actions = [node for node in defs if _returns(node, "None")]
    if not actions:
        raise MacroRejection(
            "the source needs at least one action: a `def <name>() -> None:` whose docstring says "
            "what it does and when it is the right move."
        )
    reject_unstable_selectors(source)
    parsed: dict[str, MacroDefinition] = {}
    for node in actions:
        if node.args.args:
            raise MacroRejection(
                f"`def {node.name}` takes parameters, and the executer calls every action with "
                "none. Find what it acts on inside it (`find(...)`, `selector(...)`).",
                node.lineno,
            )
        definition = await registered.parse(node.name, source)
        if not definition.entry.docstring:
            raise MacroRejection(
                f"`def {node.name}` has no docstring. The docstring is what the decision model "
                "reads to choose between actions: say what it does and when it is the right move.",
                node.lineno,
            )
        parsed[node.name] = definition
    probe = await registered.parse(PROBE, source.rstrip("\n") + _PROBE_SOURCE)
    return StepMacros(source, parsed, probe)


def _returns(node: ast.FunctionDef, name: str) -> bool:
    annotation = node.returns
    if name == "None":
        return isinstance(annotation, ast.Constant) and annotation.value is None
    return isinstance(annotation, ast.Name) and annotation.id == name
