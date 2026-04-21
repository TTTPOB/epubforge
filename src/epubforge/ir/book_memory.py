"""BookMemory — rolling per-book facts accumulated across extract units."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BookMemory(BaseModel):
    footnote_callouts: list[str] = Field(default_factory=list, max_length=30)
    """Footnote callout symbols observed, e.g. ["①","②","③","*","†"]."""

    attribution_templates: list[str] = Field(default_factory=list, max_length=6)
    """Attribution format patterns, e.g. ["——{author}：《{work}》", "——{author}"]."""

    epigraph_chapters: list[int] = Field(default_factory=list, max_length=50)
    """Page numbers where chapter-opening epigraphs were detected."""

    punctuation_quirks: list[str] = Field(default_factory=list, max_length=20)
    """Natural-language descriptions of punctuation anomalies from docling output."""

    running_headers: list[str] = Field(default_factory=list, max_length=10)
    """Page header/footer strings that should be stripped from content."""

    chapter_heading_style: str | None = None
    """Observed heading pattern, e.g. '第N章 标题'."""

    notes: list[str] = Field(default_factory=list, max_length=6)
    """Other free-form observations (kept short to control token cost)."""
