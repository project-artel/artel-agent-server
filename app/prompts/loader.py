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
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Formatter

from app.config import get_settings

PROMPTS_ROOT = Path(__file__).resolve().parent

# Which Settings field holds each agent's default version. Unset means "latest".
SETTINGS_VERSION_KEYS: dict[str, str] = {
    "qa_run": "qa_prompt_version",
    "scenario": "scenario_prompt_version",
    "game_context": "game_context_prompt_version",
    "knowledge_query": "knowledge_query_prompt_version",
}

_FRONTMATTER_FENCE = "---"
_FRONTMATTER_KEYS = ("version", "note", "placeholders")
_VERSION_PATTERN = re.compile(r"^v(\d+)$")


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
        if key not in _FRONTMATTER_KEYS:
            raise PromptError(
                f"{source}: unknown frontmatter key {key!r}; "
                f"expected one of {', '.join(_FRONTMATTER_KEYS)}."
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

    return PromptFile(
        agent=agent,
        version=version,
        role=role,
        note=str(meta["note"]),
        placeholders=actual,
        body=body,
    )


def load_prompt(agent: str, role: str, version: str | None = None) -> PromptFile:
    """Read one prompt. Each file is parsed once per process."""
    return _read_prompt(agent, resolve_version(agent, version), role)


def clear_prompt_cache() -> None:
    """Drop every cached read. For tests that point the loader elsewhere."""
    known_agents.cache_clear()
    available_versions.cache_clear()
    _read_prompt.cache_clear()


def validate_prompts() -> None:
    """Parse and check every prompt file, and every version named in settings.

    Called from ``create_app`` on purpose. A prompt whose placeholders have
    drifted, or a ``*_PROMPT_VERSION`` pointing at a directory nobody created,
    should stop the process at boot — not surface as a broken run an hour later.
    """
    for agent in known_agents():
        for version in available_versions(agent):
            directory = PROMPTS_ROOT / agent / version
            roles = sorted(path.stem for path in directory.glob("*.md"))
            if not roles:
                raise PromptError(f"{directory}: contains no .md prompt files.")
            for role in roles:
                _read_prompt(agent, version, role)

    for agent in SETTINGS_VERSION_KEYS:
        resolve_version(agent)
