import json
from typing import Any

from app.prompts import load_prompt

# Directory under app/prompts/ holding this agent's prompt versions.
PROMPT_AGENT = "project_agent"

_LANGUAGE_DIRECTIVES = {
    "ko": "Reply in Korean.",
    "en": "Reply in English.",
}


def language_directive(locale: str) -> str:
    base = locale.strip().lower().split("-")[0]
    return _LANGUAGE_DIRECTIVES.get(base, _LANGUAGE_DIRECTIVES["ko"])


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _line(value: Any) -> str:
    """One line of text: any whitespace run, newlines included, becomes one space.

    Browser text is data, so a newline in it must not start a heading or a bullet.
    """
    return " ".join(str(value).split())


def render_page_catalog(pages: list[dict[str, Any]]) -> str:
    """The catalog as readable text. Inner shape is not validated, so every key is optional."""
    if not pages:
        return "(no pages)"
    blocks: list[str] = []
    for page in pages:
        heading = f"## {_line(page.get('id', '?'))}"
        if page.get("title"):
            heading += f" - {_line(page['title'])}"
        lines = [heading]
        lines.append(f"- path: /{_line(page.get('path', ''))}")
        params = page.get("params") or []
        lines.append(f"- params: {', '.join(_line(p) for p in params) if params else '(none)'}")
        if page.get("description"):
            lines.append(f"- description: {_line(page['description'])}")
        fields = page.get("fields") or []
        if fields:
            lines.append("- fields:")
            for item in fields:
                lines.append(f"  - {_line(item.get('name', '?'))}: {_line(item.get('description', ''))}")
        actions = page.get("actions") or []
        if actions:
            lines.append("- actions:")
            for item in actions:
                lines.append(
                    f"  - {_line(item.get('id', '?'))} [risk: {_line(item.get('risk', 'auto'))}]: "
                    f"{_line(item.get('description', ''))}"
                )
                lines.append(f"    args schema: {_json(item.get('args') or {})}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_links(links: list[dict[str, Any]]) -> str:
    """Console pages outside the project, as markdown links. A link with no url is skipped."""
    lines: list[str] = []
    for link in links:
        url = _line(link.get("url") or "")
        if not url:
            continue
        title = _line(link.get("title") or link.get("id") or url)
        line = f"- [{title}]({url})"
        if link.get("description"):
            line += f": {_line(link['description'])}"
        lines.append(line)
    return "\n".join(lines) if lines else "(none)"


def render_guides(guides: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for guide in guides:
        line = f"- {_line(guide.get('id', '?'))}"
        if guide.get("title"):
            line += f" - {_line(guide['title'])}"
        if guide.get("summary"):
            line += f": {_line(guide['summary'])}"
        lines.append(line)
    return "\n".join(lines) if lines else "(none)"


def build_system_prompt(
    pages: list[dict[str, Any]],
    locale: str,
    version: str | None = None,
    links: list[dict[str, Any]] | None = None,
    guides: list[dict[str, Any]] | None = None,
) -> str:
    body = load_prompt(PROMPT_AGENT, "system", version).body
    # str.format substitutes values verbatim, so braces in the catalog JSON are safe.
    return body.format(
        language_directive=language_directive(locale),
        page_catalog=render_page_catalog(pages),
        outside_links=render_links(links or []),
        guides=render_guides(guides or []),
    )
