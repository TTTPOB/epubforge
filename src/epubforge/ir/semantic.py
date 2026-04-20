"""Semantic IR — Pydantic v2 models. Implement fully in epubforge-7yd."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class Provenance(BaseModel):
    page: int
    bbox: list[float] | None = None
    source: Literal["llm", "vlm", "passthrough"] = "passthrough"
    raw_ref: str | None = None


class Paragraph(BaseModel):
    kind: Literal["paragraph"] = "paragraph"
    text: str
    provenance: Provenance


class Heading(BaseModel):
    kind: Literal["heading"] = "heading"
    level: int = 1
    text: str
    provenance: Provenance


class Footnote(BaseModel):
    kind: Literal["footnote"] = "footnote"
    callout: str
    text: str
    ref_bbox: list[float] | None = None
    provenance: Provenance


class Figure(BaseModel):
    kind: Literal["figure"] = "figure"
    caption: str = ""
    image_ref: str | None = None
    bbox: list[float] | None = None
    provenance: Provenance


class Table(BaseModel):
    kind: Literal["table"] = "table"
    html: str
    table_title: str = ""
    caption: str = ""
    continuation: bool = False
    bbox: list[float] | None = None
    provenance: Provenance


class Equation(BaseModel):
    kind: Literal["equation"] = "equation"
    latex: str = ""
    image_ref: str | None = None
    bbox: list[float] | None = None
    provenance: Provenance


Block = Annotated[
    Union[Paragraph, Heading, Footnote, Figure, Table, Equation],
    Field(discriminator="kind"),
]


class Chapter(BaseModel):
    title: str
    level: int = 1
    blocks: list[Block] = Field(default_factory=list)


class Book(BaseModel):
    title: str
    authors: list[str] = Field(default_factory=list)
    language: str = "en"
    source_pdf: str = ""
    chapters: list[Chapter] = Field(default_factory=list)


# --- VLM output schema (used as response_format) ---

class VLMBlock(BaseModel):
    kind: str
    text: str | None = None
    level: int | None = None
    callout: str | None = None
    ref_bbox: list[float] | None = None
    caption: str | None = None
    image_ref: str | None = None
    html: str | None = None
    latex: str | None = None
    bbox: list[float] | None = None
    continuation: bool = False
    table_title: str | None = None


class VLMPageOutput(BaseModel):
    page: int
    blocks: list[VLMBlock]
