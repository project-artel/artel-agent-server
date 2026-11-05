"""Convert a ``GameContext`` into the knowledge item list `/extract` returns.

orchestration-server's ``AgentExtractClient`` reads ``game_context`` as
``List<KnowledgeIngestItem>`` — the same array shape the QA WebSocket knowledge
path already uses (``app/qa/envelope.py``'s ``KnowledgeCreatePayload``). It is
not a ``GameContext`` object. This module is the one place that bridges the two:
the agent still produces the eight-section ``GameContext`` shape
(``app/agents/game_context/schemas.py``), and this converts it into the flat
``tag``/``summary``/``description`` list at the API boundary, so the schema and
the ``game_context/v1`` prompt do not have to change.

Kept as a pure function so the conversion rules are unit-testable without an
LLM (ARTEL-745).
"""

from pydantic import BaseModel

from app.agents.game_context.schemas import (
    Entity,
    Flow,
    GameContext,
    GlossaryItem,
    Mechanic,
    MiscItem,
    Overview,
    ProgressionItem,
    Screen,
)

# 첫 줄이 너무 길면 summary 로 쓰기엔 부담스러운 길이라 자른다. 발명한 값이라
# 리뷰에서 조정될 수 있다.
_MISC_SUMMARY_MAX_LENGTH = 80


class KnowledgeIngestItem(BaseModel):
    """One design-document fact, in the shape orchestration-server ingests.

    ``tag`` is always one of ``KNOWLEDGE_TAGS`` — this module never invents a
    value outside that vocabulary.
    """

    tag: str
    summary: str
    description: str


def _join_fields(*pairs: tuple[str, str | int | None | list[str]]) -> str:
    """Render ``label: value`` lines, dropping empty fields, joined by newline.

    A string field is empty when it is ``None`` or ``""``; a list field is empty
    when it has no elements and is rendered as its values joined by ``, ``. A
    number is empty only when it is ``None`` — ``0`` is a real value (e.g.
    ``ProgressionItem.order``) and must not be dropped as falsy.
    """
    lines = []
    for label, value in pairs:
        if value is None or value == "" or value == []:
            continue
        rendered = ", ".join(value) if isinstance(value, list) else str(value)
        lines.append(f"{label}: {rendered}")
    return "\n".join(lines)


def _from_overview(overview: Overview | None) -> list[KnowledgeIngestItem]:
    if overview is None:
        return []
    description = _join_fields(
        ("genre", overview.genre),
        ("platform", overview.platform),
        ("summary", overview.summary),
        ("core_loop", overview.core_loop),
    )
    return [
        KnowledgeIngestItem(tag="OBJECTIVE", summary=overview.title or "", description=description)
    ]


def _from_screens(screens: list[Screen]) -> list[KnowledgeIngestItem]:
    return [
        KnowledgeIngestItem(
            tag="UI",
            summary=screen.name,
            description=_join_fields(
                ("purpose", screen.purpose),
                ("elements", screen.elements),
                ("transitions", screen.transitions),
            ),
        )
        for screen in screens
    ]


def _from_mechanics(mechanics: list[Mechanic]) -> list[KnowledgeIngestItem]:
    return [
        KnowledgeIngestItem(
            tag="RULE",
            summary=mechanic.name,
            description=_join_fields(
                ("description", mechanic.description),
                ("rules", mechanic.rules),
                ("preconditions", mechanic.preconditions),
            ),
        )
        for mechanic in mechanics
    ]


def _from_entities(entities: list[Entity]) -> list[KnowledgeIngestItem]:
    return [
        KnowledgeIngestItem(
            tag="MISC",
            summary=entity.name,
            description=_join_fields(
                ("type", entity.type),
                ("attributes", entity.attributes),
            ),
        )
        for entity in entities
    ]


def _from_progression(progression: list[ProgressionItem]) -> list[KnowledgeIngestItem]:
    return [
        KnowledgeIngestItem(
            tag="OBJECTIVE",
            summary=item.name,
            description=_join_fields(
                ("order", item.order),
                ("notes", item.notes),
            ),
        )
        for item in progression
    ]


def _from_flows(flows: list[Flow]) -> list[KnowledgeIngestItem]:
    # CONTROL, not RULE or UI: a flow is the sequence of steps a player walks
    # through, which reads closer to how the player interacts than to a rule the
    # game enforces or a screen's layout. The Jira issue for ARTEL-745 flags this
    # as the one debatable mapping among the five tags — flipping it later costs
    # changing this one string.
    return [
        KnowledgeIngestItem(
            tag="CONTROL",
            summary=flow.name,
            description=_join_fields(("steps", flow.steps)),
        )
        for flow in flows
    ]


def _from_glossary(glossary: list[GlossaryItem]) -> list[KnowledgeIngestItem]:
    return [
        KnowledgeIngestItem(
            tag="MISC",
            summary=item.term,
            description=_join_fields(("meaning", item.meaning)),
        )
        for item in glossary
    ]


def _from_misc(misc: list[MiscItem]) -> list[KnowledgeIngestItem]:
    items = []
    for entry in misc:
        first_line = entry.note.split("\n", 1)[0][:_MISC_SUMMARY_MAX_LENGTH]
        items.append(KnowledgeIngestItem(tag="MISC", summary=first_line, description=entry.note))
    return items


def game_context_to_knowledge_items(context: GameContext) -> list[KnowledgeIngestItem]:
    """Flatten every section of ``context`` into the knowledge item list.

    Every one of the eight ``GameContext`` sections is visited so none is
    silently dropped; only items whose ``summary`` or ``description`` end up
    empty are excluded, per ARTEL-745's acceptance criteria.
    """
    items = [
        *_from_overview(context.overview),
        *_from_screens(context.screens),
        *_from_mechanics(context.mechanics),
        *_from_entities(context.entities),
        *_from_progression(context.progression),
        *_from_flows(context.flows),
        *_from_glossary(context.glossary),
        *_from_misc(context.misc),
    ]
    return [item for item in items if item.summary and item.description]
