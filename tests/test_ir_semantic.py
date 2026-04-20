"""Unit tests for Semantic IR Pydantic models."""

from __future__ import annotations

import json
from typing import Literal

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
    VLMParagraph,
    VLMTable,
    VLMFootnote,
)


def _prov(page: int = 1, source: Literal["llm", "vlm", "passthrough"] = "passthrough") -> Provenance:
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

    def test_discriminated_union_correct_types(self) -> None:
        raw = {
            "page": 1,
            "blocks": [
                {"kind": "paragraph", "text": "Hello"},
                {"kind": "table", "html": "<table/>"},
            ],
        }
        out = VLMPageOutput.model_validate(raw)
        assert isinstance(out.blocks[0], VLMParagraph)
        assert isinstance(out.blocks[1], VLMTable)

    def test_invalid_kind_raises_validation_error(self) -> None:
        raw = {"page": 1, "blocks": [{"kind": "unknown_kind", "text": "x"}]}
        with pytest.raises(ValidationError):
            VLMPageOutput.model_validate(raw)

    def test_table_requires_html(self) -> None:
        raw = {"page": 1, "blocks": [{"kind": "table"}]}
        with pytest.raises(ValidationError):
            VLMPageOutput.model_validate(raw)

    def test_footnote_requires_callout(self) -> None:
        raw = {"page": 1, "blocks": [{"kind": "footnote", "text": "body"}]}
        with pytest.raises(ValidationError):
            VLMPageOutput.model_validate(raw)

    def test_extra_fields_ignored(self) -> None:
        # ref_bbox was a field in old VLMBlock; it should be silently ignored now
        raw = {
            "page": 1,
            "blocks": [{"kind": "footnote", "callout": "①", "text": "note", "ref_bbox": [1, 2, 3, 4]}],
        }
        out = VLMPageOutput.model_validate(raw)
        assert isinstance(out.blocks[0], VLMFootnote)


class TestAssemblerPagePropagation:
    def test_per_block_page_used(self) -> None:
        from epubforge.assembler import _parse_block

        block = _parse_block({"kind": "paragraph", "text": "x", "page": 42}, default_page=1, source="llm")
        assert block is not None
        assert block.provenance.page == 42

    def test_falls_back_to_default_page(self) -> None:
        from epubforge.assembler import _parse_block

        block = _parse_block({"kind": "paragraph", "text": "x"}, default_page=7, source="llm")
        assert block is not None
        assert block.provenance.page == 7
