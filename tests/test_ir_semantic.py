"""Unit tests for Semantic IR Pydantic models."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from epubforge.ir.semantic import (
    Book,
    Chapter,
    Equation,
    Figure,
    Footnote,
    Heading,
    Paragraph,
    Provenance,
    Table,
    VLMPageOutput,
)


def _prov(page: int = 1, source: str = "passthrough") -> Provenance:
    return Provenance(page=page, source=source)


class TestParagraph:
    def test_round_trip(self) -> None:
        p = Paragraph(text="Hello world.", provenance=_prov())
        d = p.model_dump()
        assert Paragraph.model_validate(d).text == "Hello world."

    def test_kind_is_paragraph(self) -> None:
        p = Paragraph(text="x", provenance=_prov())
        assert p.kind == "paragraph"


class TestHeading:
    def test_defaults(self) -> None:
        h = Heading(text="Chapter 1", provenance=_prov())
        assert h.level == 1
        assert h.kind == "heading"

    def test_custom_level(self) -> None:
        h = Heading(text="Sec", level=3, provenance=_prov())
        assert h.level == 3


class TestFootnote:
    def test_round_trip(self) -> None:
        fn = Footnote(callout="1", text="See also...", provenance=_prov())
        assert fn.kind == "footnote"
        assert fn.callout == "1"


class TestBook:
    def test_empty_book(self) -> None:
        b = Book(title="Test Book")
        assert b.chapters == []
        assert b.authors == []
        assert b.language == "en"

    def test_chapter_with_mixed_blocks(self) -> None:
        ch = Chapter(
            title="Intro",
            blocks=[
                Paragraph(text="First paragraph.", provenance=_prov()),
                Heading(text="Background", level=2, provenance=_prov()),
                Footnote(callout="1", text="A note.", provenance=_prov()),
            ],
        )
        b = Book(title="My Book", chapters=[ch])
        assert len(b.chapters[0].blocks) == 3

    def test_json_round_trip(self) -> None:
        b = Book(
            title="Round Trip",
            chapters=[
                Chapter(
                    title="Ch1",
                    blocks=[Paragraph(text="p", provenance=_prov(source="llm"))],
                )
            ],
        )
        restored = Book.model_validate_json(b.model_dump_json())
        assert restored.chapters[0].title == "Ch1"
        assert restored.chapters[0].blocks[0].provenance.source == "llm"

    def test_block_discriminator(self) -> None:
        data = {
            "title": "Book",
            "chapters": [
                {
                    "title": "Ch",
                    "blocks": [
                        {"kind": "paragraph", "text": "x", "provenance": {"page": 1, "source": "llm"}},
                        {"kind": "figure", "caption": "Fig 1", "provenance": {"page": 2, "source": "vlm"}},
                        {"kind": "table", "html": "<table/>", "provenance": {"page": 3, "source": "vlm"}},
                        {"kind": "equation", "latex": r"E=mc^2", "provenance": {"page": 1, "source": "passthrough"}},
                    ],
                }
            ],
        }
        b = Book.model_validate(data)
        kinds = [blk.kind for blk in b.chapters[0].blocks]
        assert kinds == ["paragraph", "figure", "table", "equation"]


class TestVLMPageOutput:
    def test_parse_minimal(self) -> None:
        raw = {"page": 5, "blocks": [{"kind": "paragraph", "text": "Hello."}]}
        out = VLMPageOutput.model_validate(raw)
        assert out.page == 5
        assert out.blocks[0].kind == "paragraph"

    def test_all_kinds(self) -> None:
        raw = {
            "page": 17,
            "blocks": [
                {"kind": "paragraph", "text": "Para.", "bbox": [10.0, 20.0, 200.0, 40.0]},
                {"kind": "footnote", "callout": "1", "text": "Note.", "ref_bbox": [10.0, 700.0, 50.0, 710.0]},
                {"kind": "figure", "caption": "A fig.", "image_ref": "p17_fig1"},
                {"kind": "table", "html": "<table><tr><td>A</td></tr></table>", "caption": "Tab 1"},
                {"kind": "equation", "latex": r"\int_0^\infty"},
            ],
        }
        out = VLMPageOutput.model_validate(raw)
        assert len(out.blocks) == 5
