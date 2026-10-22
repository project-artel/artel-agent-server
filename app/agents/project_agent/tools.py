"""The nine tools, as schemas only.

The model proposes a call and the browser runs it, so nothing here executes. The
argument shapes are pydantic models so that the JSON Schema bound to the model
comes from one place.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

PageId = Literal[
    "home",
    "documents",
    "testCases",
    "testRuns",
    "qa",
    "qaHistory",
    "contentMap",
    "macros",
    "performance",
    "issues",
    "knowledge",
    "usage",
    "qaRun",
    "testRun",
    "testRunEdit",
    "testScenario",
]


class OpenPageArgs(BaseModel):
    page: PageId = Field(description="Page id from the page catalog.")
    params: dict[str, str] = Field(
        default_factory=dict,
        description="Path and query params the page needs, such as runId or build.",
    )


class ReadPageArgs(BaseModel):
    pass


class SetFieldArgs(BaseModel):
    field: str = Field(description="Field name from the open page's snapshot.")
    value: Any = Field(description="The new value, any JSON.")


class CallActionArgs(BaseModel):
    action: str = Field(description="Action id from the open page's snapshot.")
    args: dict[str, Any] = Field(
        default_factory=dict, description="Arguments matching the action's schema."
    )


class ChoiceOption(BaseModel):
    id: str
    label: str


class AskChoiceArgs(BaseModel):
    question: str
    options: list[ChoiceOption] = Field(min_length=2, max_length=8)
    multiple: bool = Field(default=False, description="Allow picking more than one.")


VerificationStatus = Literal["DRAFT", "VERIFIED", "FAILED", "REGRESSION", "BROKEN"]


class QueryFilters(BaseModel):
    # One flat object because a root `oneOf` on `kind` is not accepted by every provider.
    # `validate_query` checks the filters kind by kind: which are required, which allowed.

    id: str | None = Field(default=None, description="test_case: the case id (required). qa_run: same as qaRunId.")
    ids: list[str] | None = Field(default=None, description="test_cases: only these case ids.")
    search: str | None = Field(default=None, description="test_cases: text in step, scene, expected value or precondition.")
    scene: str | None = Field(default=None, description="test_cases: exact scene name.")
    folderId: str | None = Field(default=None, description="test_cases: only cases in this folder.")
    verificationStatus: VerificationStatus | None = Field(default=None, description="test_cases: verdict filter.")
    gameBuildId: str | None = Field(
        default=None, description="test_cases and test_case: read the content and verdict of this build."
    )
    offset: int | None = Field(default=None, ge=0, description="test_cases: rows to skip, for the next page.")
    testRunId: str | None = Field(default=None, description="qa_history: only runs of this test run.")
    qaRunId: str | None = Field(default=None, description="qa_run: the QA run id (required, or id).")
    status: Literal["OPEN", "RESOLVED"] | None = Field(default=None, description="issues: status filter.")
    severity: Literal["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "TRIVIAL"] | None = Field(
        default=None, description="issues: severity filter."
    )


class _Strict(BaseModel):
    model_config = {"extra": "forbid"}


class _TestCaseQuery(_Strict):
    id: str = Field(min_length=1)
    gameBuildId: str | None = None


class _TestCasesQuery(_Strict):
    ids: list[str] | None = None
    search: str | None = None
    scene: str | None = None
    folderId: str | None = None
    verificationStatus: VerificationStatus | None = None
    gameBuildId: str | None = None
    offset: int | None = Field(default=None, ge=0)


class _NoArgsQuery(_Strict):
    pass


class _QaHistoryQuery(_Strict):
    testRunId: str | None = None


class _QaRunQuery(_Strict):
    qaRunId: str | None = None
    id: str | None = None

    @model_validator(mode="after")
    def needs_an_id(self) -> "_QaRunQuery":
        if not (self.qaRunId or self.id):
            raise ValueError("qa_run needs args.qaRunId")
        return self


class _IssuesQuery(_Strict):
    status: Literal["OPEN", "RESOLVED"] | None = None
    severity: Literal["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "TRIVIAL"] | None = None


_QUERY_ARGS: dict[str, type[BaseModel]] = {
    "qa_history": _QaHistoryQuery,
    "qa_run": _QaRunQuery,
    "issues": _IssuesQuery,
    "usage": _NoArgsQuery,
    "test_cases": _TestCasesQuery,
    "test_case": _TestCaseQuery,
    "build_versions": _NoArgsQuery,
}


class QueryArgs(BaseModel):
    kind: Literal[
        "qa_history",
        "qa_run",
        "issues",
        "usage",
        "test_cases",
        "test_case",
        "build_versions",
    ]
    args: QueryFilters = Field(
        default_factory=QueryFilters,
        description=(
            "Filters for the kind; leave out what the kind does not take. "
            "test_case: id (required), gameBuildId. "
            "test_cases: ids, search, scene, folderId, verificationStatus, gameBuildId, offset. "
            "qa_history: testRunId. qa_run: qaRunId. issues: status, severity. "
            "usage and build_versions: none."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def check_args_for_kind(cls, data: Any) -> Any:
        reason = validate_query(data)
        if reason is not None:
            raise ValueError(reason)
        return data


def _summarize(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'args'}: {item['msg']}"
        for item in error.errors(include_url=False)
    )


def validate_query(data: Any) -> str | None:
    """Why a `query` call is invalid for its kind, or None."""
    if not isinstance(data, dict):
        return "the call must be an object with kind and args"
    kind = data.get("kind")
    if kind not in _QUERY_ARGS:
        return f"unknown kind {kind!r}; use one of {', '.join(_QUERY_ARGS)}"
    raw = data.get("args") or {}
    if not isinstance(raw, dict):
        return "args must be an object"
    try:
        _QUERY_ARGS[kind].model_validate(raw)
    except ValidationError as error:
        return f"{kind} args: {_summarize(error)}"
    return None


def validate_tool_call(name: str, args: dict[str, Any]) -> str | None:
    """Why a proposed call cannot run, or None. Only `query` has a contract to check."""
    return validate_query(args) if name == "query" else None


class WatchJobArgs(BaseModel):
    kind: Literal["content_map_scan", "qa_run"]
    id: str
    label: str | None = None


class AuthorScenariosArgs(BaseModel):
    run_id: int = Field(description="The test run to write scenarios into.")
    instruction: str


class ReadGuideArgs(BaseModel):
    topic: str = Field(description="Guide id from the Guides list in the system prompt.")


_TOOLS: tuple[tuple[str, str, type[BaseModel]], ...] = (
    (
        "open_page",
        "Open a page beside the chat. Returns the new page's snapshot.",
        OpenPageArgs,
    ),
    (
        "read_page",
        "Read the open page's snapshot: fields with values and options, and actions "
        "with the reason each blocked one is blocked.",
        ReadPageArgs,
    ),
    ("set_field", "Write one field of the open page.", SetFieldArgs),
    (
        "call_action",
        "Run one action of the open page. An action with risk approve waits for the "
        "user to approve it on screen.",
        CallActionArgs,
    ),
    (
        "ask_choice",
        "Show the user a choice card with 2 to 8 options. Returns the picked option ids.",
        AskChoiceArgs,
    ),
    (
        "query",
        "Read-only lookup. qa_history, qa_run, issues and usage cover QA runs and costs. "
        "test_cases lists test cases, filtered by ids, search, scene, folderId, "
        "verificationStatus, gameBuildId (only cases that have a version in that build) "
        "and offset. test_case returns everything a person sees about one case: scene, "
        "step, precondition, expected value, spec status, verification status, origin "
        "(MAP or MANUAL), folder, evidence gaps, allowed functions, last verified build, "
        "and which build versions contain it. build_versions lists game builds, newest "
        "first, with which is latest. Returns at most 20 rows.",
        QueryArgs,
    ),
    (
        "watch_job",
        "Show a progress card for a running job and return at once. The browser reports "
        "the outcome later as an event message.",
        WatchJobArgs,
    ),
    (
        "read_guide",
        "Read the body of one how-to guide by its id. Returns the guide as markdown, "
        "with its commands and links.",
        ReadGuideArgs,
    ),
    (
        "author_scenarios",
        "Ask the scenario authoring agent to write scenarios into a test run. "
        "Returns its summary and changes.",
        AuthorScenariosArgs,
    ),
)

def _portable(node: Any, defs: dict[str, Any], *, in_properties: bool = False) -> Any:
    """Inline `$ref`, collapse `X | None` to X, drop `title` keys and null defaults at every level.

    Providers differ in what they accept (`$defs`, `anyOf` with null), so the bound
    schema uses only plain types, enums, arrays and objects.
    """
    if isinstance(node, list):
        return [_portable(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if in_properties:
        # Keys here are property names, so a property called "title" is kept.
        return {key: _portable(value, defs) for key, value in node.items()}
    if node.get("default", 0) is None:
        node = {key: value for key, value in node.items() if key != "default"}
    if "$ref" in node:
        return _portable(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
    options = node.get("anyOf")
    if options:
        non_null = [option for option in options if option.get("type") != "null"]
        if len(non_null) == 1:
            rest = {k: v for k, v in node.items() if k != "anyOf"}
            return _portable({**non_null[0], **rest}, defs)
    return {
        key: _portable(value, defs, in_properties=(key == "properties"))
        for key, value in node.items()
        if key not in ("title", "$defs")
    }


def tool_schemas() -> list[dict[str, Any]]:
    """OpenAI-style function schemas, which `bind_tools` converts for every provider."""
    schemas: list[dict[str, Any]] = []
    for name, description, args in _TOOLS:
        raw = args.model_json_schema()
        parameters = _portable(raw, raw.get("$defs", {}))
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            }
        )
    return schemas
