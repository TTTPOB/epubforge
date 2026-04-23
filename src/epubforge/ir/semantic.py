"""Semantic IR — Pydantic v2 models."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from epubforge.ir.book_memory import BookMemory


BLOCK_INIT_NAMESPACE = "block-init"
BLOCK_RUNTIME_NAMESPACE = "block-runtime"
CHAPTER_INIT_NAMESPACE = "chapter-init"
CHAPTER_RUNTIME_NAMESPACE = "chapter-runtime"


def compute_uid(seed: str, *components: object) -> str:
    """Compute a short deterministic uid in a namespace-scoped hash domain."""
    payload = "\x1f".join([seed, *(str(component) for component in components)])
    return hashlib.sha256(payload.encode("utf-8")).digest()[:6].hex()


def compute_block_uid_init(
    seed: str,
    ch_pos: int,
    block_pos: int,
    kind: str,
    text_head: str,
    page: int | str,
) -> str:
    return compute_uid(
        seed,
        BLOCK_INIT_NAMESPACE,
        ch_pos,
        block_pos,
        kind,
        text_head[:32],
        page,
    )


def compute_chapter_uid_init(seed: str, ch_pos: int, title: str) -> str:
    return compute_uid(seed, CHAPTER_INIT_NAMESPACE, ch_pos, title[:64])


def compute_block_uid_runtime(
    seed: str,
    chapter_uid: str,
    after_uid: str | None,
    kind: str,
    text_head: str,
    op_id: str,
) -> str:
    return compute_uid(
        seed,
        BLOCK_RUNTIME_NAMESPACE,
        chapter_uid,
        after_uid or "HEAD",
        kind,
        text_head[:32],
        op_id,
    )


def compute_chapter_uid_runtime(seed: str, op_id: str, new_title: str) -> str:
    return compute_uid(seed, CHAPTER_RUNTIME_NAMESPACE, op_id, new_title[:64])


class Provenance(BaseModel):
    page: int
    bbox: list[float] | None = None
    source: Literal["llm", "vlm", "passthrough"] = "passthrough"
    raw_ref: str | None = None


class _UidMixin(BaseModel):
    uid: str | None = None


class Paragraph(_UidMixin):
    kind: Literal["paragraph"] = "paragraph"
    text: str
    role: str = "body"
    display_lines: list[str] | None = None
    style_class: str | None = None
    cross_page: bool = False  # True when paragraph spans a page break (assembled from continuation)
    provenance: Provenance


class Heading(_UidMixin):
    kind: Literal["heading"] = "heading"
    level: int = 1
    text: str
    id: str | None = None
    style_class: str | None = None
    provenance: Provenance


class Footnote(_UidMixin):
    kind: Literal["footnote"] = "footnote"
    callout: str
    text: str
    paired: bool = False  # True when callout was found and marked in a preceding paragraph
    orphan: bool = False  # True when LLM stage 7 confirms no matching callout exists in the book
    ref_bbox: list[float] | None = None
    provenance: Provenance


class Figure(_UidMixin):
    kind: Literal["figure"] = "figure"
    caption: str = ""
    image_ref: str | None = None
    bbox: list[float] | None = None
    provenance: Provenance


class TableMergeRecord(BaseModel):
    """Provenance recorded during assembler._merge_continued_tables().

    All fields reflect information available at merge time (before uid initialisation).
    constituent_block_uids is deliberately excluded: uids are not stable at this stage.
    """

    segment_html: list[str]
    """Raw HTML extracted from each tbody segment, in merge order."""

    segment_pages: list[int]
    """Source page number for each segment (same order as segment_html)."""

    segment_order: list[int]
    """0-based merge order index for each segment (same order as segment_html)."""

    column_widths: list[int]
    """Modal logical column width (sum of colspan) for each segment."""


class Table(_UidMixin):
    kind: Literal["table"] = "table"
    html: str
    table_title: str = ""
    caption: str = ""
    continuation: bool = False
    multi_page: bool = False  # True when this table was merged from cross-page continuations
    bbox: list[float] | None = None
    provenance: Provenance
    merge_record: TableMergeRecord | None = None


class Equation(_UidMixin):
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
    uid: str | None = None
    title: str
    level: int = 1
    id: str | None = None
    blocks: list[Block] = Field(default_factory=list)


class Book(BaseModel):
    version: int = 0
    initialized_at: str = ""
    uid_seed: str = ""
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


class AuditNote(BaseModel):
    page: int
    block_index: int | None = None
    kind: Literal[
        "orphan_footnote", "suspect_attribution",
        "punctuation_anomaly", "unknown_callout", "other"
    ]
    hint: str = Field(max_length=200)


class VLMPageOutput(BaseModel):
    page: int
    blocks: list[VLMBlock]
    first_block_continues_prev_tail: bool = False
    first_footnote_continues_prev_footnote: bool = False
    audit_notes: list[AuditNote] = Field(default_factory=list)


# --- stage 4 (VLM) multi-page wrapper ---

class VLMGroupOutput(BaseModel):
    pages: list[VLMPageOutput]
    updated_book_memory: BookMemory = Field(default_factory=BookMemory)
