"""Prompt files are chosen and checked before a run ever starts.

A prompt is now data on disk, which means the ways it can be wrong are the ways
data can be wrong: the wrong version picked, a placeholder renamed on one side
only, a file that says it is v1 while sitting in v2. Every one of those is
silent at runtime — the model simply reads something nobody intended — so they
are all made loud here and at startup.
"""

import pytest

from app.prompts import loader
from app.prompts.loader import (
    PromptError,
    available_versions,
    clear_prompt_cache,
    known_agents,
    load_prompt,
    parse_prompt_file,
    placeholders_in,
    resolve_version,
    validate_prompts,
)


class StubSettings:
    """Only the fields the loader reads."""

    def __init__(self, **versions: str | None) -> None:
        self.qa_prompt_version = versions.get("qa_prompt_version")
        self.scenario_prompt_version = versions.get("scenario_prompt_version")
        self.game_context_prompt_version = versions.get("game_context_prompt_version")
        self.knowledge_query_prompt_version = versions.get(
            "knowledge_query_prompt_version"
        )


@pytest.fixture
def prompt_root(tmp_path, monkeypatch):
    """Point the loader at a throwaway tree, with no version configured."""
    monkeypatch.setattr(loader, "PROMPTS_ROOT", tmp_path)
    monkeypatch.setattr(loader, "get_settings", lambda: StubSettings())
    clear_prompt_cache()
    yield tmp_path
    clear_prompt_cache()


def write_prompt(
    root,
    agent: str,
    version: str,
    role: str,
    body: str,
    *,
    placeholders: str | None = None,
    declared_version: str | None = None,
) -> None:
    directory = root / agent / version
    directory.mkdir(parents=True, exist_ok=True)
    if placeholders is None:
        placeholders = ", ".join(placeholders_in(body))
    frontmatter = (
        "---\n"
        f"version: {declared_version or version}\n"
        "note: 테스트용\n"
        f"placeholders: [{placeholders}]\n"
        "---\n"
    )
    (directory / f"{role}.md").write_text(frontmatter + body + "\n", encoding="utf-8")


def configure(monkeypatch, **versions: str | None) -> None:
    monkeypatch.setattr(loader, "get_settings", lambda: StubSettings(**versions))
    clear_prompt_cache()


# --- version resolution -------------------------------------------------------


def test_latest_version_wins_when_nothing_is_configured(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "system", "one")
    write_prompt(prompt_root, "qa_run", "v2", "system", "two")

    assert resolve_version("qa_run") == "v2"
    assert load_prompt("qa_run", "system").body == "two"


def test_the_configured_default_beats_the_latest(prompt_root, monkeypatch) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "system", "one")
    write_prompt(prompt_root, "qa_run", "v2", "system", "two")
    configure(monkeypatch, qa_prompt_version="v1")

    assert resolve_version("qa_run") == "v1"
    assert load_prompt("qa_run", "system").body == "one"


def test_an_explicit_argument_beats_the_configured_default(
    prompt_root, monkeypatch
) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "system", "one")
    write_prompt(prompt_root, "qa_run", "v2", "system", "two")
    configure(monkeypatch, qa_prompt_version="v1")

    assert resolve_version("qa_run", "v2") == "v2"
    assert load_prompt("qa_run", "system", "v2").body == "two"


def test_an_empty_configured_value_reads_as_unset(prompt_root, monkeypatch) -> None:
    """An env var set to "" is how a deploy says "no override", not version ""."""
    write_prompt(prompt_root, "qa_run", "v1", "system", "one")
    write_prompt(prompt_root, "qa_run", "v2", "system", "two")
    configure(monkeypatch, qa_prompt_version="")

    assert resolve_version("qa_run") == "v2"


def test_versions_are_ordered_by_number_not_by_name(prompt_root) -> None:
    """Lexically 'v10' sorts before 'v2', which would silently pin the old one."""
    for version in ("v1", "v2", "v10"):
        write_prompt(prompt_root, "qa_run", version, "system", version)

    assert available_versions("qa_run") == ("v1", "v2", "v10")
    assert resolve_version("qa_run") == "v10"


def test_an_unknown_version_fails(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "system", "one")

    with pytest.raises(PromptError, match="v7"):
        resolve_version("qa_run", "v7")


def test_an_unknown_agent_fails(prompt_root) -> None:
    with pytest.raises(PromptError, match="nope"):
        resolve_version("nope")


def test_a_directory_that_is_not_a_version_fails(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "system", "one")
    (prompt_root / "qa_run" / "draft").mkdir()

    with pytest.raises(PromptError, match="v<number>"):
        available_versions("qa_run")


# --- file integrity -----------------------------------------------------------


def test_frontmatter_version_must_match_the_directory(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v2", "system", "two", declared_version="v1")

    with pytest.raises(PromptError, match="frontmatter says version"):
        load_prompt("qa_run", "system", "v2")


def test_a_placeholder_missing_from_the_frontmatter_fails(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "system", "hi {name}", placeholders="")

    with pytest.raises(PromptError, match="Undeclared in frontmatter: \\['name'\\]"):
        load_prompt("qa_run", "system", "v1")


def test_a_placeholder_declared_but_unused_fails(prompt_root) -> None:
    """The likelier direction: the body was reworded and the frontmatter was not."""
    write_prompt(prompt_root, "qa_run", "v1", "system", "hi", placeholders="name")

    with pytest.raises(PromptError, match="Declared but unused: \\['name'\\]"):
        load_prompt("qa_run", "system", "v1")


def test_doubled_braces_are_literal_text_not_placeholders(prompt_root) -> None:
    """A body carrying a JSON example escapes its braces the way str.format does."""
    body = 'Return {{"ok": true}} for {name}.'
    write_prompt(prompt_root, "qa_run", "v1", "system", body, placeholders="name")

    prompt = load_prompt("qa_run", "system", "v1")

    assert prompt.placeholders == ("name",)
    assert prompt.body.format(name="you") == 'Return {"ok": true} for you.'


def test_a_missing_file_fails(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "system", "one")

    with pytest.raises(PromptError, match="No prompt file at"):
        load_prompt("qa_run", "human", "v1")


def test_each_file_is_read_once_per_process(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "system", "one")
    load_prompt("qa_run", "system", "v1")

    (prompt_root / "qa_run" / "v1" / "system.md").unlink()

    assert load_prompt("qa_run", "system", "v1").body == "one"


# --- the frontmatter grammar --------------------------------------------------


def test_the_body_keeps_its_own_newlines_and_loses_the_files_last_one() -> None:
    meta, body = parse_prompt_file(
        "---\nversion: v1\nnote: n\nplaceholders: []\n---\nline one\n\nline two\n"
    )

    assert meta["version"] == "v1"
    assert body == "line one\n\nline two"


def test_windows_line_endings_do_not_change_the_body() -> None:
    """A CRLF checkout must not silently produce a different prompt."""
    _meta, body = parse_prompt_file(
        "---\r\nversion: v1\r\nnote: n\r\nplaceholders: []\r\n---\r\na\r\nb\r\n"
    )

    assert body == "a\nb"


def test_an_inline_list_and_an_empty_list_both_parse() -> None:
    meta, _body = parse_prompt_file(
        "---\nversion: v1\nnote: n\nplaceholders: [a, b]\n---\n{a} {b}\n"
    )
    empty, _ = parse_prompt_file(
        "---\nversion: v1\nnote: n\nplaceholders: []\n---\nx\n"
    )

    assert meta["placeholders"] == ["a", "b"]
    assert empty["placeholders"] == []


def test_a_file_without_frontmatter_fails() -> None:
    with pytest.raises(PromptError, match="must open with a '---' line"):
        parse_prompt_file("You are a QA agent.\n")


def test_unclosed_frontmatter_fails() -> None:
    with pytest.raises(PromptError, match="never closed"):
        parse_prompt_file("---\nversion: v1\nnote: n\nplaceholders: []\n")


def test_a_missing_frontmatter_key_fails() -> None:
    with pytest.raises(PromptError, match="missing placeholders"):
        parse_prompt_file("---\nversion: v1\nnote: n\n---\nbody\n")


def test_an_unknown_frontmatter_key_fails() -> None:
    with pytest.raises(PromptError, match="unknown frontmatter key"):
        parse_prompt_file(
            "---\nversion: v1\nnote: n\nplaceholders: []\nauthor: me\n---\nbody\n"
        )


def test_placeholders_must_be_a_list() -> None:
    with pytest.raises(PromptError, match="must be a list"):
        parse_prompt_file("---\nversion: v1\nnote: n\nplaceholders: name\n---\n{name}\n")


# --- startup validation -------------------------------------------------------


def test_validate_prompts_rejects_a_configured_version_that_does_not_exist(
    prompt_root, monkeypatch
) -> None:
    for agent in ("qa_run", "scenario", "game_context"):
        write_prompt(prompt_root, agent, "v1", "system", "body")
    configure(monkeypatch, qa_prompt_version="v9")

    with pytest.raises(PromptError, match="'v9' does not exist"):
        validate_prompts()


def test_validate_prompts_rejects_a_broken_file_in_a_version_nobody_uses(
    prompt_root,
) -> None:
    """A candidate version is checked too — it will be someone's default later."""
    for agent in ("qa_run", "scenario", "game_context"):
        write_prompt(prompt_root, agent, "v1", "system", "body")
    write_prompt(prompt_root, "qa_run", "v2", "system", "hi {name}", placeholders="")

    with pytest.raises(PromptError, match="Undeclared in frontmatter"):
        validate_prompts()


def test_validate_prompts_rejects_an_empty_version_directory(prompt_root) -> None:
    for agent in ("qa_run", "scenario", "game_context"):
        write_prompt(prompt_root, agent, "v1", "system", "body")
    (prompt_root / "qa_run" / "v2").mkdir()

    with pytest.raises(PromptError, match="no .md prompt files"):
        validate_prompts()


# --- the prompts this repository actually ships -------------------------------


def test_the_shipped_prompts_all_pass_validation() -> None:
    validate_prompts()


def test_every_live_agent_has_a_v1(monkeypatch) -> None:
    monkeypatch.setattr(loader, "get_settings", lambda: StubSettings())
    clear_prompt_cache()
    try:
        assert set(known_agents()) == {
            "qa_run",
            "qa_compaction",
            "scenario",
            "game_context",
            "knowledge_query",
        }
        for agent in known_agents():
            assert available_versions(agent)[0] == "v1"
    finally:
        clear_prompt_cache()
