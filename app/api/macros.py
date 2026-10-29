"""developer macro 의 문법 글과 검사 (ARTEL-1087).

orchestration 이 MCP 서버의 `macro_grammar`·`publish_macros` 를 받아 이 둘을
부른다. 저장은 orchestration 의 몫이고 여기는 읽고 판정만 한다.

macro 의 잘못은 200 에 `ok: false` 로 돌려준다. 400대로 내면 orchestration 이 "요청이 틀렸다"
와 "macro 가 틀렸다" 를 status 로 가를 수 없고, 후자는 개발자에게 그대로 보여 줄 문장이다.
body 모양이 틀린 것만 FastAPI 의 422 다.
"""

from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, StringConstraints

from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.model import MacroDefinition
from app.developer_macro import (
    DEVELOPER_MACRO_RULES,
    check_developer_macro,
    developer_macro_grammar,
)

router = APIRouter(tags=["macro"])

# orchestration `MacroWriteFrames` 의 `MAX_NAME_LENGTH`·`MAX_SOURCE_LENGTH` 와 같은 값이다.
# 여기서 통과한 macro 가 저장할 때 길이로 거절되지 않게 맞춘다.
MAX_NAME_LENGTH = 200
MAX_SOURCE_LENGTH = 20_000

MacroName = Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAME_LENGTH)]
MacroSource = Annotated[str, StringConstraints(max_length=MAX_SOURCE_LENGTH)]


class MacroGrammarResponse(BaseModel):
    grammar: str
    rules: list[str]


class MacroCheckRequest(BaseModel):
    name: MacroName
    source: MacroSource
    # `from macros import ...` 가 가리킬 수 있는 같은 프로젝트의 developer macro, 이름 → source.
    imports: dict[MacroName, MacroSource] = Field(default_factory=dict)


class MacroCheckRejection(BaseModel):
    line: int | None
    # `MacroRejection.render()` 그대로다. QA 런의 tool loop 가 모델에게 보이는 문장과 같다.
    reason: str


class MacroCheckPassed(BaseModel):
    ok: Literal[True] = True
    definition: MacroDefinition
    parameters: list[str]
    docstring: str


class MacroCheckRefused(BaseModel):
    ok: Literal[False] = False
    rejection: MacroCheckRejection


@router.get("/macros/grammar", response_model=MacroGrammarResponse)
async def macro_grammar() -> MacroGrammarResponse:
    return MacroGrammarResponse(
        grammar=developer_macro_grammar(), rules=list(DEVELOPER_MACRO_RULES)
    )


@router.post("/macros/check", response_model=MacroCheckPassed | MacroCheckRefused)
async def check_macro(payload: MacroCheckRequest) -> MacroCheckPassed | MacroCheckRefused:
    try:
        definition = check_developer_macro(payload.name, payload.source, payload.imports)
    except MacroRejection as rejection:
        return MacroCheckRefused(
            rejection=MacroCheckRejection(line=rejection.line, reason=rejection.render())
        )
    # `check_developer_macro` 가 docstring 없는 entry 를 거절하므로 여기서는 비어 있지 않다.
    assert definition.entry.docstring is not None
    return MacroCheckPassed(
        definition=definition,
        parameters=[parameter.name for parameter in definition.entry.parameters],
        docstring=definition.entry.docstring,
    )
