"""Prompt text, kept in versioned files rather than in Python constants.

Layout is ``app/prompts/<agent>/<version>/<role>.md``. A file opens with a
frontmatter block and the rest of it is the prompt, verbatim::

    ---
    version: v1
    note: 기존 코드 상수를 그대로 옮김. 문구 변경 없음.
    placeholders: [language_directive]
    ---
    You are a QA agent ...

The frontmatter grammar is deliberately smaller than YAML — scalars and inline
lists, nothing else — because the project has no YAML dependency and this is not
worth adding one for. Anything outside that grammar is an error, not a silent
reinterpretation.

Two invariants are checked on every read, so a broken prompt is caught at
startup (see ``validate_prompts``) rather than in the middle of a run:

* ``version`` in the frontmatter matches the directory the file sits in;
* ``placeholders`` lists exactly the ``{name}`` fields the body uses — no more,
  no fewer.

Placeholder extraction follows ``str.format`` / LangChain f-string rules, so a
body that needs a literal brace doubles it (``{{`` and ``}}``); doubled braces
are literal text and are not placeholders.

A file whose role starts with ``skill_`` may also carry ``description``: one
line saying what the skill holds and when to load it. The system prompt's Skills
section is generated from these lines (``skill_descriptions``), so a skill and
the line that advertises it live in one file and cannot drift apart. Any other
role carrying ``description`` is an error. From ``qa_run/v19`` on every skill
must carry a non-empty one of at most ``SKILL_DESCRIPTION_MAX_CHARS``
characters; ``validate_prompts`` checks it.

The description is frontmatter, yet the model reads it, so it cannot be left out
of the hash the way ``note`` is. ``PromptFile.body_sha256`` of a file with a
description is the sha256 of ``description + "\n\n" + body``; a file without
one hashes the body alone. Only skills may have a description, so every other
role's hash, and every entry the lock holds for a released version, is what it
was before descriptions existed.

Neither invariant says anything about a released version staying put, which is
the rule the rest of this module is built on. ``app.prompts.lock`` enforces that
one, against a committed hash rather than against git history.
"""

import hashlib
import re
from dataclasses import dataclass
import json
from functools import lru_cache
from pathlib import Path
from string import Formatter

from app.config import get_settings

PROMPTS_ROOT = Path(__file__).resolve().parent

# Which Settings field holds each agent's default version. Unset means "latest".
SETTINGS_VERSION_KEYS: dict[str, str] = {
    "qa_run": "qa_prompt_version",
    "qa_compaction": "qa_compaction_prompt_version",
    "scenario": "scenario_prompt_version",
    "game_context": "game_context_prompt_version",
    "knowledge_query": "knowledge_query_prompt_version",
    "screen_verdict": "screen_verdict_prompt_version",
    "screen_name": "screen_name_prompt_version",
}

_FRONTMATTER_FENCE = "---"
_FRONTMATTER_KEYS = ("version", "note", "placeholders")
# Allowed, not required, and only in a file whose role starts with `skill_`.
_DESCRIPTION_KEY = "description"
_OPTIONAL_FRONTMATTER_KEYS = (_DESCRIPTION_KEY,)
_VERSION_PATTERN = re.compile(r"^v(\d+)$")

# Size caps for the QA prompt, enforced by `validate_prompts` from this version on.
# A description over the cap fails at boot instead of costing tokens on every
# turn of every run; older versions shipped before the caps and are never edited.
QA_SLIM_PROMPT_FROM_VERSION = 19
TOOL_DESCRIPTION_MAX_CHARS = 500
SYSTEM_PROMPT_MAX_CHARS = 8000
# A skill's description is one line of the Skills section, sent on every turn.
SKILL_DESCRIPTION_MAX_CHARS = 300

TOOL_ROLE_PREFIX = "tool_"
SKILL_ROLE_PREFIX = "skill_"


class PromptError(RuntimeError):
    """A prompt file is missing, unreadable, or disagrees with its frontmatter."""


@dataclass(frozen=True)
class PromptFile:
    agent: str
    version: str
    role: str
    note: str
    placeholders: tuple[str, ...]
    body: str
    # sha256 of what the model reads from this file, and the reason a version
    # directory is not enough to compare two runs by. A version is a name someone
    # chose; editing `v3` in place leaves every run before and after the edit
    # filed under the same name, and the comparison silently averages two
    # different prompts. `note` is excluded because it is documentation. A skill's
    # `description` is included, because it becomes a line of the Skills section:
    # with one, this is the sha256 of `description + "\n\n" + body`; without
    # one, of the body alone, which keeps every non-skill hash where it was.
    body_sha256: str
    # The skill's one-line description, or None. Only `skill_` roles may set it.
    description: str | None = None


# --- parsing ------------------------------------------------------------------


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_value(raw: str, key: str, source: str) -> str | list[str]:
    value = raw.strip()
    if value.startswith("["):
        if not value.endswith("]"):
            raise PromptError(f"{source}: frontmatter '{key}' has an unclosed list.")
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_unquote(item.strip()) for item in inner.split(",") if item.strip()]
    return _unquote(value)


def _parse_frontmatter(lines: list[str], source: str) -> dict[str, str | list[str]]:
    meta: dict[str, str | list[str]] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise PromptError(f"{source}: frontmatter line is not 'key: value': {line!r}")
        key = key.strip()
        if key not in _FRONTMATTER_KEYS + _OPTIONAL_FRONTMATTER_KEYS:
            raise PromptError(
                f"{source}: unknown frontmatter key {key!r}; "
                f"expected one of {', '.join(_FRONTMATTER_KEYS + _OPTIONAL_FRONTMATTER_KEYS)}."
            )
        if key in meta:
            raise PromptError(f"{source}: frontmatter key {key!r} appears twice.")
        meta[key] = _parse_value(value, key, source)

    missing = [key for key in _FRONTMATTER_KEYS if key not in meta]
    if missing:
        raise PromptError(f"{source}: frontmatter is missing {', '.join(missing)}.")
    if not isinstance(meta["placeholders"], list):
        raise PromptError(
            f"{source}: frontmatter 'placeholders' must be a list, e.g. [a, b] or []."
        )
    if isinstance(meta["version"], list):
        raise PromptError(f"{source}: frontmatter 'version' must be a scalar.")
    if isinstance(meta.get(_DESCRIPTION_KEY), list):
        raise PromptError(f"{source}: frontmatter 'description' must be a scalar.")
    return meta


def parse_prompt_file(text: str, source: str = "<string>") -> tuple[dict, str]:
    """Split a prompt file into its frontmatter and its body.

    The body is everything after the closing fence, minus the one trailing
    newline every text file ends with. Prompts are concatenated string literals
    that do not end in a newline, and a file cannot both end in one and not have
    one, so the last newline belongs to the file rather than to the prompt.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        raise PromptError(f"{source}: prompt file must open with a '---' line.")

    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_FENCE:
            closing = index
            break
    if closing is None:
        raise PromptError(f"{source}: frontmatter is never closed by a '---' line.")

    meta = _parse_frontmatter(lines[1:closing], source)
    body = "\n".join(lines[closing + 1 :])
    if body.endswith("\n"):
        body = body[:-1]
    return meta, body


def placeholders_in(body: str, source: str = "<string>") -> tuple[str, ...]:
    """The ``{name}`` fields the body substitutes, in order of first appearance.

    ``{{`` and ``}}`` are literal braces and yield no placeholder, matching what
    ``str.format`` and LangChain's f-string templates do with the same text.
    """
    names: list[str] = []
    try:
        fields = list(Formatter().parse(body))
    except ValueError as error:
        raise PromptError(f"{source}: body is not a valid format template: {error}") from error
    for _literal, field, _spec, _conversion in fields:
        if field is None:
            continue
        if not field:
            raise PromptError(
                f"{source}: body uses a positional field '{{}}'; name every placeholder."
            )
        if field not in names:
            names.append(field)
    return tuple(names)


# --- discovery and resolution -------------------------------------------------


@lru_cache(maxsize=None)
def known_agents() -> tuple[str, ...]:
    if not PROMPTS_ROOT.is_dir():  # pragma: no cover - the package ships the directory
        raise PromptError(f"Prompt root {PROMPTS_ROOT} does not exist.")
    return tuple(
        sorted(
            child.name
            for child in PROMPTS_ROOT.iterdir()
            if child.is_dir() and not child.name.startswith((".", "_"))
        )
    )


@lru_cache(maxsize=None)
def available_versions(agent: str) -> tuple[str, ...]:
    """Version directories for one agent, oldest first.

    Ordered by the number, not by the name: ``v10`` is newer than ``v2``, which
    a lexical sort gets backwards.
    """
    directory = PROMPTS_ROOT / agent
    if not directory.is_dir():
        raise PromptError(f"No prompt directory for agent {agent!r} under {PROMPTS_ROOT}.")
    numbered: list[tuple[int, str]] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        match = _VERSION_PATTERN.match(child.name)
        if match is None:
            raise PromptError(
                f"{child}: not a version directory; expected 'v<number>', e.g. 'v1'."
            )
        numbered.append((int(match.group(1)), child.name))
    if not numbered:
        raise PromptError(f"Agent {agent!r} has no prompt versions under {directory}.")
    return tuple(name for _number, name in sorted(numbered))


def roles_in(agent: str, version: str) -> tuple[str, ...]:
    """The roles one version directory defines, sorted.

    A role is the stem of a ``.md`` file, so ``system.md`` is the ``system``
    role. Shared with ``app.prompts.lock`` on purpose: if what gets validated
    and what gets locked disagreed about which files count, a prompt could ship
    outside the lock and nothing would say so.
    """
    directory = PROMPTS_ROOT / agent / version
    roles = tuple(sorted(path.stem for path in directory.glob("*.md")))
    if not roles:
        raise PromptError(f"{directory}: contains no .md prompt files.")
    return roles


def data_in(agent: str, version: str) -> tuple[str, ...]:
    """한 판본 디렉터리가 담은 **자료 파일** 이름들(`.json` 의 stem), 정렬해서.

    프롬프트가 산문만으로 안 되는 자리가 있다. 결정 전용 모델은 선택지를 **구조로** 받으므로
    그 명세가 dict 이어야 하는데(`scenario_router` 의 criteria), 그것을 코드에 두면 산문과
    따로 움직인다 — criteria 는 같은 판본의 `system.md` 를 옮겨 적은 것이라 **둘이 어긋나면
    측정이 거짓이 된다.** 같은 디렉터리에 두면 판본이 둘을 묶는다.

    `.md` 와 갈라 두는 이유는 역할(role)이 아니기 때문이다. 자료는 모델에게 렌더해 보내는
    본문이 아니라 코드가 읽는 값이고, placeholder 검사도 할 것이 없다.
    """
    directory = PROMPTS_ROOT / agent / version
    return tuple(sorted(path.stem for path in directory.glob("*.json")))


@lru_cache(maxsize=None)
def _read_data(agent: str, version: str, name: str) -> tuple[str, str]:
    """`(본문, sha256)`. 본문을 그대로 들고 다니는 것은 해시가 **파일과 같아야** 하기 때문이다."""
    import hashlib

    path = PROMPTS_ROOT / agent / version / f"{name}.json"
    source = f"{agent}/{version}/{name}.json"
    if not path.is_file():
        raise PromptError(f"{source}: no such data file under {PROMPTS_ROOT}.")
    body = path.read_text(encoding="utf-8")
    try:
        json.loads(body)
    except ValueError as error:
        raise PromptError(f"{source}: not valid JSON: {error}") from error
    return body, hashlib.sha256(body.encode("utf-8")).hexdigest()


def load_data(agent: str, name: str, version: str | None = None) -> object:
    """판본 디렉터리의 JSON 자료를 읽는다. 판본 해소는 `load_prompt` 와 같은 길을 탄다."""
    body, _ = _read_data(agent, resolve_version(agent, version), name)
    return json.loads(body)


def data_sha256(agent: str, name: str, version: str) -> str:
    """그 자료의 sha256. `lock` 이 조용한 변경을 잡는 데 쓴다."""
    return _read_data(agent, version, name)[1]


def _configured_version(agent: str) -> str | None:
    key = SETTINGS_VERSION_KEYS.get(agent)
    if key is None:
        return None
    # An empty env value reads as "not configured" rather than as version "".
    return getattr(get_settings(), key) or None


def resolve_version(agent: str, version: str | None = None) -> str:
    """Explicit argument first, then the configured default, then the latest."""
    versions = available_versions(agent)
    chosen = version if version is not None else _configured_version(agent)
    if chosen is None:
        return versions[-1]
    if chosen not in versions:
        raise PromptError(
            f"Prompt version {chosen!r} does not exist for agent {agent!r}. "
            f"Available: {', '.join(versions)}."
        )
    return chosen


# --- loading ------------------------------------------------------------------


def content_sha256(body: str, description: str | None = None) -> str:
    """The hash `PromptFile.body_sha256` holds: the body, plus the description if any."""
    text = body if description is None else f"{description}\n\n{body}"
    return hashlib.sha256(text.encode()).hexdigest()


@lru_cache(maxsize=None)
def _read_prompt(agent: str, version: str, role: str) -> PromptFile:
    path = PROMPTS_ROOT / agent / version / f"{role}.md"
    if not path.is_file():
        raise PromptError(f"No prompt file at {path}.")
    source = str(path)
    meta, body = parse_prompt_file(path.read_text(encoding="utf-8"), source)

    if meta["version"] != version:
        raise PromptError(
            f"{source}: frontmatter says version {meta['version']!r} but the file "
            f"lives in {version!r}."
        )

    declared = tuple(meta["placeholders"])
    actual = placeholders_in(body, source)
    missing = [name for name in actual if name not in declared]
    extra = [name for name in declared if name not in actual]
    if missing or extra:
        raise PromptError(
            f"{source}: declared placeholders do not match the body. "
            f"Undeclared in frontmatter: {missing or '-'}. "
            f"Declared but unused: {extra or '-'}."
        )

    description = meta.get(_DESCRIPTION_KEY)
    if description is not None and not role.startswith(SKILL_ROLE_PREFIX):
        raise PromptError(
            f"{source}: frontmatter key {_DESCRIPTION_KEY!r} is only allowed in "
            f"{SKILL_ROLE_PREFIX}* files; role {role!r} is not a skill."
        )

    return PromptFile(
        agent=agent,
        version=version,
        role=role,
        note=str(meta["note"]),
        placeholders=actual,
        body=body,
        body_sha256=content_sha256(body, description),
        description=description,
    )


def load_prompt(agent: str, role: str, version: str | None = None) -> PromptFile:
    """Read one prompt. Each file is parsed once per process."""
    return _read_prompt(agent, resolve_version(agent, version), role)


def load_tool_description(tool_name: str, version: str | None = None) -> PromptFile:
    """The description of one QA tool, from ``qa_run/<version>/tool_<tool_name>.md``."""
    return load_prompt("qa_run", f"{TOOL_ROLE_PREFIX}{tool_name}", version)


def load_skill(name: str, version: str | None = None) -> PromptFile:
    """One QA skill, from ``qa_run/<version>/skill_<name>.md``."""
    return load_prompt("qa_run", f"{SKILL_ROLE_PREFIX}{name}", version)


def skill_names(version: str | None = None) -> tuple[str, ...]:
    """Names of the skills one ``qa_run`` version defines, sorted, without the prefix."""
    resolved = resolve_version("qa_run", version)
    return tuple(
        role.removeprefix(SKILL_ROLE_PREFIX)
        for role in roles_in("qa_run", resolved)
        if role.startswith(SKILL_ROLE_PREFIX)
    )


def skill_descriptions(version: str | None = None) -> dict[str, str]:
    """Each skill's description, keyed by name without the prefix, in name order.

    The Skills section of the system prompt is built from this, so a skill file
    added with a description is listed without editing `skills_directive.md`.
    A skill without a description is an error here; from v19 on
    `validate_prompts` refuses one at boot, before anything asks.
    """
    resolved = resolve_version("qa_run", version)
    descriptions: dict[str, str] = {}
    for name in skill_names(resolved):
        skill = load_skill(name, resolved)
        if not skill.description:
            raise PromptError(
                f"{PROMPTS_ROOT / 'qa_run' / resolved / (SKILL_ROLE_PREFIX + name)}.md: "
                f"skill has no frontmatter 'description', so the Skills section "
                f"cannot list it."
            )
        descriptions[name] = skill.description
    return descriptions


def clear_prompt_cache() -> None:
    """Drop every cached read. For tests that point the loader elsewhere."""
    known_agents.cache_clear()
    available_versions.cache_clear()
    _read_prompt.cache_clear()


def _check_qa_prompt_size(prompt: PromptFile) -> None:
    """Hold ``qa_run`` tool descriptions, the system prompt and skill descriptions to their caps."""
    number = int(_VERSION_PATTERN.match(prompt.version).group(1))
    if number < QA_SLIM_PROMPT_FROM_VERSION:
        return
    path = f"{PROMPTS_ROOT / prompt.agent / prompt.version / prompt.role}.md"
    length = len(prompt.body)
    if prompt.role.startswith(TOOL_ROLE_PREFIX) and length > TOOL_DESCRIPTION_MAX_CHARS:
        raise PromptError(
            f"{path}: tool description is {length} characters; the limit is "
            f"{TOOL_DESCRIPTION_MAX_CHARS}. Move the detail into a skill."
        )
    if prompt.role == "system" and length > SYSTEM_PROMPT_MAX_CHARS:
        raise PromptError(
            f"{path}: system prompt is {length} characters; the limit is "
            f"{SYSTEM_PROMPT_MAX_CHARS}. Move the detail into a skill."
        )
    if prompt.role.startswith(SKILL_ROLE_PREFIX):
        description = (prompt.description or "").strip()
        if not description:
            raise PromptError(
                f"{path}: skill has no frontmatter 'description'; from "
                f"v{QA_SLIM_PROMPT_FROM_VERSION} every skill needs one line saying what "
                f"it holds and when to load it, at most {SKILL_DESCRIPTION_MAX_CHARS} "
                f"characters."
            )
        if len(prompt.description) > SKILL_DESCRIPTION_MAX_CHARS:
            raise PromptError(
                f"{path}: skill description is {len(prompt.description)} characters; "
                f"the limit is {SKILL_DESCRIPTION_MAX_CHARS}."
            )


def validate_prompts() -> None:
    """Parse and check every prompt file, and every version named in settings.

    Called from ``create_app`` on purpose. A prompt whose placeholders have
    drifted, or a ``*_PROMPT_VERSION`` pointing at a directory nobody created,
    should stop the process at boot — not surface as a broken run an hour later.
    """
    for agent in known_agents():
        for version in available_versions(agent):
            for role in roles_in(agent, version):
                prompt = _read_prompt(agent, version, role)
                if agent == "qa_run":
                    _check_qa_prompt_size(prompt)

    for agent in SETTINGS_VERSION_KEYS:
        resolve_version(agent)
