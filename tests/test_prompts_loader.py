"""Prompt files are chosen and checked before a run ever starts.

A prompt is now data on disk, which means the ways it can be wrong are the ways
data can be wrong: the wrong version picked, a placeholder renamed on one side
only, a file that says it is v1 while sitting in v2. Every one of those is
silent at runtime — the model simply reads something nobody intended — so they
are all made loud here and at startup.
"""

import pytest

from app import prompts
from app.prompts import loader
from app.prompts.loader import (
    PromptError,
    available_versions,
    clear_prompt_cache,
    known_agents,
    load_prompt,
    load_skill,
    load_tool_description,
    parse_prompt_file,
    placeholders_in,
    resolve_version,
    skill_descriptions,
    skill_names,
    validate_prompts,
)


class StubSettings:
    """Only the fields the loader reads."""

    def __init__(self, **versions: str | None) -> None:
        self.qa_prompt_version = versions.get("qa_prompt_version")
        self.qa_compaction_prompt_version = versions.get(
            "qa_compaction_prompt_version"
        )
        self.scenario_prompt_version = versions.get("scenario_prompt_version")
        self.game_context_prompt_version = versions.get("game_context_prompt_version")
        self.knowledge_query_prompt_version = versions.get(
            "knowledge_query_prompt_version"
        )
        self.screen_verdict_prompt_version = versions.get(
            "screen_verdict_prompt_version"
        )
        self.screen_name_prompt_version = versions.get("screen_name_prompt_version")


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
    description: str | None = None,
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
        + (f"description: {description}\n" if description is not None else "")
        + "---\n"
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


# --- tool descriptions and skills ---------------------------------------------


def test_tool_descriptions_and_skills_are_qa_run_roles(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "tool_click", "Click a target.")
    write_prompt(prompt_root, "qa_run", "v1", "skill_content_map", "Map notes.")

    assert load_tool_description("click") == load_prompt("qa_run", "tool_click")
    assert load_skill("content_map") == load_prompt("qa_run", "skill_content_map")
    assert load_tool_description("click").body == "Click a target."


def test_a_version_can_be_named_for_a_tool_description_and_a_skill(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "tool_click", "old")
    write_prompt(prompt_root, "qa_run", "v2", "tool_click", "new")
    write_prompt(prompt_root, "qa_run", "v1", "skill_a", "old skill")
    write_prompt(prompt_root, "qa_run", "v2", "skill_a", "new skill")

    assert load_tool_description("click", "v1").body == "old"
    assert load_tool_description("click").body == "new"
    assert load_skill("a", "v1").body == "old skill"


def test_a_missing_tool_description_or_skill_is_an_error(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "system", "body")

    with pytest.raises(PromptError, match="tool_nope.md"):
        load_tool_description("nope")
    with pytest.raises(PromptError, match="skill_nope.md"):
        load_skill("nope")


def test_skill_names_lists_only_skills_sorted_without_the_prefix(prompt_root) -> None:
    for role in ("skill_knowledge_base", "skill_content_map", "system", "tool_click"):
        write_prompt(prompt_root, "qa_run", "v1", role, "body")
    write_prompt(prompt_root, "qa_run", "v2", "system", "body")

    assert skill_names("v1") == ("content_map", "knowledge_base")
    assert skill_names("v2") == ()
    assert skill_names() == ()  # the latest version, v2


def test_the_prompts_package_exports_the_skill_and_tool_loaders() -> None:
    assert prompts.load_tool_description is load_tool_description
    assert prompts.load_skill is load_skill
    assert prompts.skill_names is skill_names
    assert prompts.skill_descriptions is skill_descriptions
    assert prompts.SKILL_DESCRIPTION_MAX_CHARS == 300
    assert prompts.TOOL_DESCRIPTION_MAX_CHARS == 500
    assert prompts.SYSTEM_PROMPT_MAX_CHARS == 8000


def _write_all_agents(root) -> None:
    for agent in tuple(loader.SETTINGS_VERSION_KEYS):
        write_prompt(root, agent, "v1", "system", "body")


def test_validate_prompts_rejects_a_long_tool_description_from_v19(prompt_root) -> None:
    _write_all_agents(prompt_root)
    write_prompt(prompt_root, "qa_run", "v19", "system", "body")
    write_prompt(prompt_root, "qa_run", "v19", "tool_click", "x" * 501)

    with pytest.raises(PromptError, match=r"tool_click\.md.*501 characters"):
        validate_prompts()


def test_validate_prompts_accepts_a_tool_description_at_the_limit(prompt_root) -> None:
    _write_all_agents(prompt_root)
    write_prompt(prompt_root, "qa_run", "v19", "system", "body")
    write_prompt(prompt_root, "qa_run", "v19", "tool_click", "x" * 500)

    validate_prompts()


def test_validate_prompts_exempts_versions_before_v19(prompt_root) -> None:
    # v18 is develop's phase-cycle prompt, released at 24,739 characters before
    # the caps existed; a released version is never edited.
    _write_all_agents(prompt_root)
    write_prompt(prompt_root, "qa_run", "v17", "system", "x" * 24_000)
    write_prompt(prompt_root, "qa_run", "v17", "tool_click", "x" * 5_000)
    write_prompt(prompt_root, "qa_run", "v18", "system", "x" * 24_739)

    validate_prompts()


def test_validate_prompts_rejects_a_long_system_prompt_from_v19(prompt_root) -> None:
    _write_all_agents(prompt_root)
    write_prompt(prompt_root, "qa_run", "v19", "system", "x" * 8_001)

    with pytest.raises(PromptError, match=r"system\.md.*8001 characters"):
        validate_prompts()


def test_the_size_limits_apply_to_qa_run_only(prompt_root) -> None:
    _write_all_agents(prompt_root)
    write_prompt(prompt_root, "scenario", "v19", "system", "x" * 9_000)
    write_prompt(prompt_root, "scenario", "v19", "tool_click", "x" * 9_000)

    validate_prompts()


def test_skills_are_not_held_to_the_tool_description_limit(prompt_root) -> None:
    _write_all_agents(prompt_root)
    write_prompt(prompt_root, "qa_run", "v19", "system", "body")
    write_prompt(
        prompt_root,
        "qa_run",
        "v19",
        "skill_knowledge_base",
        "x" * 6_200,
        description="What to record.",
    )

    validate_prompts()


# --- skill descriptions -------------------------------------------------------


def test_a_skill_may_carry_a_description(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "skill_a", "body", description="Holds a. Load first.")

    assert load_skill("a", "v1").description == "Holds a. Load first."
    assert load_skill("a", "v1").body == "body"


def test_a_file_without_a_description_has_none(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "system", "body")

    assert load_prompt("qa_run", "system", "v1").description is None


@pytest.mark.parametrize("role", ["system", "tool_click", "skills_directive"])
def test_a_description_outside_a_skill_is_an_error(prompt_root, role) -> None:
    write_prompt(prompt_root, "qa_run", "v1", role, "body", description="Not a skill.")

    with pytest.raises(PromptError, match=rf"{role}\.md.*'description'.*not a skill"):
        load_prompt("qa_run", role, "v1")


def test_a_description_must_be_a_scalar() -> None:
    with pytest.raises(PromptError, match="'description' must be a scalar"):
        parse_prompt_file(
            "---\nversion: v1\nnote: n\nplaceholders: []\ndescription: [a, b]\n---\nbody\n"
        )


def test_a_description_cannot_run_onto_a_second_line() -> None:
    with pytest.raises(PromptError, match="not 'key: value'"):
        parse_prompt_file(
            "---\nversion: v1\nnote: n\nplaceholders: []\n"
            "description: Holds a\n  and carries on here\n---\nbody\n"
        )


def test_validate_prompts_rejects_a_v19_skill_without_a_description(prompt_root) -> None:
    _write_all_agents(prompt_root)
    write_prompt(prompt_root, "qa_run", "v19", "system", "body")
    write_prompt(prompt_root, "qa_run", "v19", "skill_held_state", "body")

    with pytest.raises(PromptError, match=r"skill_held_state\.md.*no frontmatter 'description'"):
        validate_prompts()


def test_validate_prompts_rejects_an_empty_v19_skill_description(prompt_root) -> None:
    _write_all_agents(prompt_root)
    write_prompt(prompt_root, "qa_run", "v19", "system", "body")
    write_prompt(prompt_root, "qa_run", "v19", "skill_held_state", "body", description="")

    with pytest.raises(PromptError, match=r"skill_held_state\.md.*no frontmatter 'description'"):
        validate_prompts()


def test_validate_prompts_rejects_a_long_v19_skill_description(prompt_root) -> None:
    _write_all_agents(prompt_root)
    write_prompt(prompt_root, "qa_run", "v19", "system", "body")
    write_prompt(prompt_root, "qa_run", "v19", "skill_held_state", "body", description="x" * 301)

    with pytest.raises(PromptError, match=r"skill_held_state\.md.*301 characters.*300"):
        validate_prompts()


def test_validate_prompts_accepts_a_skill_description_at_the_limit(prompt_root) -> None:
    _write_all_agents(prompt_root)
    write_prompt(prompt_root, "qa_run", "v19", "system", "body")
    write_prompt(prompt_root, "qa_run", "v19", "skill_held_state", "body", description="x" * 300)

    validate_prompts()


def test_validate_prompts_exempts_skills_before_v19_from_the_description_rule(
    prompt_root,
) -> None:
    _write_all_agents(prompt_root)
    write_prompt(prompt_root, "qa_run", "v17", "skill_old", "body")
    write_prompt(prompt_root, "qa_run", "v18", "skill_long", "body", description="x" * 900)

    validate_prompts()


def test_skill_descriptions_are_keyed_by_name_in_name_order(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v19", "system", "body")
    write_prompt(prompt_root, "qa_run", "v19", "skill_knowledge_base", "b", description="Knowledge.")
    write_prompt(prompt_root, "qa_run", "v19", "skill_content_map", "b", description="Map.")
    write_prompt(prompt_root, "qa_run", "v19", "tool_click", "b")

    descriptions = skill_descriptions("v19")
    assert descriptions == {"content_map": "Map.", "knowledge_base": "Knowledge."}
    assert list(descriptions) == ["content_map", "knowledge_base"]


def test_skill_descriptions_refuses_a_skill_without_one(prompt_root) -> None:
    write_prompt(prompt_root, "qa_run", "v1", "skill_a", "b")

    with pytest.raises(PromptError, match=r"skill_a\.md.*no frontmatter 'description'"):
        skill_descriptions("v1")


def test_every_shipped_v19_skill_has_a_description() -> None:
    clear_prompt_cache()
    descriptions = skill_descriptions("v19")
    assert set(descriptions) == set(skill_names("v19"))
    for description in descriptions.values():
        assert 0 < len(description) <= prompts.SKILL_DESCRIPTION_MAX_CHARS


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
            # 저작 워크플로의 단계별 프롬프트 — 루프의 scenario(v9)에서 갈라져 나옴.
            "scenario_router",
            # 인사·가드레일 문구 전용. 라우터가 갈래만 내고 문구를 못 낼 때 쓴다 —
            # 결정 전용 모델(Jev)은 선택지를 고를 뿐 문장을 못 쓴다.
            "scenario_reply",
            "scenario_grouping",
            "scenario_writer",
            "scenario_modify",
            "game_context",
            "knowledge_query",
            "screen_verdict",
            "screen_name",
            "step_phrasing",
        }
        for agent in known_agents():
            assert available_versions(agent)[0] == "v1"
    finally:
        clear_prompt_cache()


# --- the body hash ------------------------------------------------------------


def test_the_hash_follows_the_body(prompt_root) -> None:
    """A version directory is a name someone chose, not a fingerprint of what is
    in it. Editing `v3` in place leaves every run before and after the edit filed
    under one name, and a comparison built on that silently averages two prompts.
    """
    write_prompt(prompt_root, "qa_run", "v1", "system", "one")
    before = load_prompt("qa_run", "system", "v1").body_sha256

    clear_prompt_cache()
    write_prompt(prompt_root, "qa_run", "v1", "system", "one, edited")

    assert load_prompt("qa_run", "system", "v1").body_sha256 != before


def test_the_hash_ignores_the_frontmatter(prompt_root) -> None:
    """`note` is documentation. Changing it does not change what the model read,
    and a hash that moved with it would split one prompt into two buckets."""
    write_prompt(prompt_root, "qa_run", "v1", "system", "one")
    before = load_prompt("qa_run", "system", "v1").body_sha256

    clear_prompt_cache()
    (prompt_root / "qa_run" / "v1" / "system.md").write_text(
        "---\nversion: v1\nnote: rewritten note\nplaceholders: []\n---\none\n",
        encoding="utf-8",
    )

    assert load_prompt("qa_run", "system", "v1").body_sha256 == before


def test_a_skill_hash_follows_its_description(prompt_root) -> None:
    """The description is read by the model as the skill's line in the Skills
    section, so an edit to it alone has to move the hash the lock and
    `prompt_hashes` record."""
    write_prompt(prompt_root, "qa_run", "v1", "skill_a", "body", description="Holds a.")
    before = load_prompt("qa_run", "skill_a", "v1").body_sha256

    clear_prompt_cache()
    write_prompt(prompt_root, "qa_run", "v1", "skill_a", "body", description="Holds a and b.")

    after = load_prompt("qa_run", "skill_a", "v1")
    assert after.body == "body"
    assert after.body_sha256 != before
    assert after.body_sha256 == loader.content_sha256("body", "Holds a and b.")


def test_a_hash_without_a_description_is_the_body_hash_it_always_was(prompt_root) -> None:
    """A non-skill role cannot carry a description, so its hash is the sha256 of
    the body alone — what the lock recorded for every released version."""
    import hashlib

    write_prompt(prompt_root, "qa_run", "v1", "system", "one")

    assert (
        load_prompt("qa_run", "system", "v1").body_sha256
        == hashlib.sha256(b"one").hexdigest()
    )
