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


def render_page_catalog(pages: list[dict[str, Any]]) -> str:
    """The catalog as readable text. Inner shape is not validated, so every key is optional."""
    if not pages:
        return "(no pages)"
    blocks: list[str] = []
    for page in pages:
        heading = f"## {page.get('id', '?')}"
        if page.get("title"):
            heading += f" - {page['title']}"
        lines = [heading]
        lines.append(f"- path: /{page.get('path', '')}")
        params = page.get("params") or []
        lines.append(f"- params: {', '.join(map(str, params)) if params else '(none)'}")
        if page.get("description"):
            lines.append(f"- description: {page['description']}")
        fields = page.get("fields") or []
        if fields:
            lines.append("- fields:")
            for item in fields:
                lines.append(f"  - {item.get('name', '?')}: {item.get('description', '')}")
        actions = page.get("actions") or []
        if actions:
            lines.append("- actions:")
            for item in actions:
                lines.append(
                    f"  - {item.get('id', '?')} [risk: {item.get('risk', 'auto')}]: "
                    f"{item.get('description', '')}"
                )
                lines.append(f"    args schema: {_json(item.get('args') or {})}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_links(links: list[dict[str, Any]]) -> str:
    """Console pages outside the project, as markdown links the agent can paste."""
    if not links:
        return "(none)"
    lines: list[str] = []
    for link in links:
        title = link.get("title") or link.get("id", "?")
        line = f"- [{title}]({link.get('url', '')})"
        if link.get("description"):
            line += f": {link['description']}"
        lines.append(line)
    return "\n".join(lines)


def render_guides(guides: list[dict[str, Any]]) -> str:
    if not guides:
        return "(none)"
    lines: list[str] = []
    for guide in guides:
        line = f"- {guide.get('id', '?')}"
        if guide.get("title"):
            line += f" - {guide['title']}"
        if guide.get("summary"):
            line += f": {guide['summary']}"
        lines.append(line)
    return "\n".join(lines)


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
