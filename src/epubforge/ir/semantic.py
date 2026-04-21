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
    role: str = "body"
    display_lines: list[str] | None = None
    style_class: str | None = None
    provenance: Provenance


class Heading(BaseModel):
    kind: Literal["heading"] = "heading"
    level: int = 1
    text: str
    id: str | None = None
    provenance: Provenance


class Footnote(BaseModel):
    kind: Literal["footnote"] = "footnote"
    callout: str
    text: str
    paired: bool = False  # True when callout was found and marked in a preceding paragraph
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
    id: str | None = None
    blocks: list[Block] = Field(default_factory=list)


class Book(BaseModel):
    title: str
    authors: list[str] = Field(default_factory=list)
    language: str = "en"
    source_pdf: str = ""
    chapters: list[Chapter] = Field(default_factory=list)


# --- stage 5.5 (toc refiner) schemas ---

class TocRefineItem(BaseModel):
    idx: int
    level: int
    text: str
    merge_with_prev: bool = False


class TocRefineOutput(BaseModel):
    items: list[TocRefineItem]


# --- stage 3 (cleaner) output schema ---

class CleanBlock(BaseModel):
    kind: Literal["paragraph", "heading", "footnote"]
    text: str
    level: int | None = None
    callout: str | None = None


class CleanOutput(BaseModel):
    blocks: list[CleanBlock]
    first_block_continues_prev_tail: bool = False
    first_footnote_continues_prev_footnote: bool = False


# --- VLM output schema (used as response_format) ---

class _VLMBase(BaseModel):
    bbox: list[float] | None = None


class VLMParagraph(_VLMBase):
    kind: Literal["paragraph"] = "paragraph"
    text: str


class VLMHeading(_VLMBase):
    kind: Literal["heading"] = "heading"
    text: str
    level: int = 1


class VLMFootnote(_VLMBase):
    kind: Literal["footnote"] = "footnote"
    callout: str
    text: str = ""


class VLMFigure(_VLMBase):
    kind: Literal["figure"] = "figure"
    caption: str = ""
    image_ref: str | None = None


class VLMTable(_VLMBase):
    kind: Literal["table"] = "table"
    html: str
    table_title: str = ""
    caption: str = ""
    continuation: bool = False


class VLMEquation(_VLMBase):
    kind: Literal["equation"] = "equation"
    latex: str = ""


VLMBlock = Annotated[
    Union[VLMParagraph, VLMHeading, VLMFootnote, VLMFigure, VLMTable, VLMEquation],
    Field(discriminator="kind"),
]


class VLMPageOutput(BaseModel):
    page: int
    blocks: list[VLMBlock]
    first_block_continues_prev_tail: bool = False
    first_footnote_continues_prev_footnote: bool = False


# --- stage 4 (VLM) multi-page wrapper ---

class VLMGroupOutput(BaseModel):
    pages: list[VLMPageOutput]
