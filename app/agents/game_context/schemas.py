"""Structured game_context extracted from a design document.

Fixed outer frame, flexible interior: the top-level sections never change per
game; game-specific variety is absorbed INSIDE entries (rules/attributes/steps),
never as new top-level keys. Anything that fits no section goes to ``misc``.

Fields stay strict-json-schema friendly (no open ``dict`` — flexible detail is
carried by ``list[str]``), so capable models can emit this via strict output.
``source`` (which document an entry came from) is intentionally absent here: the
per-document extraction produces source-less content, and the aggregation layer
stamps ``source`` when merging documents into a project's game_context.
"""

from pydantic import BaseModel, Field

from app.llm.models import DEFAULT_MODEL, LLMModel


class Overview(BaseModel):
    title: str | None = None
    genre: str | None = None
    platform: str | None = None
    summary: str | None = None
    core_loop: str | None = None


class Screen(BaseModel):
    name: str
    purpose: str | None = None
    elements: list[str] = Field(default_factory=list)
    transitions: list[str] = Field(default_factory=list)


class Mechanic(BaseModel):
    name: str
    description: str | None = None
    rules: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)


class Entity(BaseModel):
    name: str
    type: str | None = None
    # Free-form traits as "key: value" strings (e.g. "weakness: fire").
    attributes: list[str] = Field(default_factory=list)


class ProgressionItem(BaseModel):
    name: str
    order: int | None = None
    notes: str | None = None


class Flow(BaseModel):
    name: str
    steps: list[str] = Field(default_factory=list)


class GlossaryItem(BaseModel):
    term: str
    meaning: str | None = None


class MiscItem(BaseModel):
    note: str


class GameContext(BaseModel):
    overview: Overview | None = None
    screens: list[Screen] = Field(default_factory=list)
    mechanics: list[Mechanic] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    progression: list[ProgressionItem] = Field(default_factory=list)
    flows: list[Flow] = Field(default_factory=list)
    glossary: list[GlossaryItem] = Field(default_factory=list)
    misc: list[MiscItem] = Field(default_factory=list)


class GameContextAgentRequest(BaseModel):
    # Normalized document text produced upstream by a DocumentLoader.
    document_text: str
    model: LLMModel = DEFAULT_MODEL
