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
    VLMFootnote,
    VLMParagraph,
    VLMTable,
    compute_block_uid_init,
    compute_block_uid_runtime,
    compute_chapter_uid_init,
    compute_chapter_uid_runtime,
    compute_uid,
)


class TestParagraph:
    def test_round_trip(self, prov) -> None:
        p = Paragraph(text="Hello world.", provenance=prov())
        d = p.model_dump()
        assert Paragraph.model_validate(d).text == "Hello world."

    def test_kind_is_paragraph(self, prov) -> None:
        p = Paragraph(text="x", provenance=prov())
        assert p.kind == "paragraph"


class TestHeading:
    def test_defaults(self, prov) -> None:
        h = Heading(text="Chapter 1", provenance=prov())
        assert h.level == 1
        assert h.kind == "heading"

    def test_custom_level(self, prov) -> None:
        h = Heading(text="Sec", level=3, provenance=prov())
        assert h.level == 3


class TestFootnote:
    def test_round_trip(self, prov) -> None:
        fn = Footnote(callout="1", text="See also...", provenance=prov())
        assert fn.kind == "footnote"
        assert fn.callout == "1"


class TestBook:
    def test_empty_book(self) -> None:
        b = Book(title="Test Book")
        assert b.chapters == []
        assert b.authors == []
        assert b.language == "en"

    def test_chapter_with_mixed_blocks(self, prov) -> None:
        ch = Chapter(
            title="Intro",
            blocks=[
                Paragraph(text="First paragraph.", provenance=prov()),
                Heading(text="Background", level=2, provenance=prov()),
                Footnote(callout="1", text="A note.", provenance=prov()),
            ],
        )
        b = Book(title="My Book", chapters=[ch])
        assert len(b.chapters[0].blocks) == 3

    def test_json_round_trip(self, prov) -> None:
        b = Book(
            title="Round Trip",
            chapters=[
                Chapter(
                    title="Ch1",
                    blocks=[Paragraph(text="p", provenance=prov(source="llm"))],
                )
            ],
        )
        restored = Book.model_validate_json(b.model_dump_json())
        assert restored.chapters[0].title == "Ch1"
        assert restored.chapters[0].blocks[0].provenance.source == "llm"
        assert restored.op_log_version == 0
        assert restored.uid_seed == ""

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


class TestUidHelpers:
    def test_compute_uid_is_deterministic(self) -> None:
        first = compute_uid("seed", "a", 1, "b")
        second = compute_uid("seed", "a", 1, "b")
        assert first == second
        assert len(first) == 12

    def test_init_and_runtime_namespaces_do_not_overlap(self) -> None:
        block_init = compute_block_uid_init("seed", 0, 1, "paragraph", "hello", 3)
        block_runtime = compute_block_uid_runtime("seed", "ch-1", "blk-1", "paragraph", "hello", "op-1")
        chapter_init = compute_chapter_uid_init("seed", 0, "Intro")
        chapter_runtime = compute_chapter_uid_runtime("seed", "op-1", "Intro")

        assert block_init != block_runtime
        assert chapter_init != chapter_runtime

    def test_models_accept_uid_fields(self, prov) -> None:
        chapter = Chapter(
            uid="ch-1",
            title="Intro",
            blocks=[Paragraph(uid="p-1", text="Hello", provenance=prov())],
        )
        book = Book(
            title="Book",
            op_log_version=2,
            initialized_at="2026-04-23T00:00:00Z",
            uid_seed="seed-1",
            chapters=[chapter],
        )
        assert book.chapters[0].uid == "ch-1"
        assert book.chapters[0].blocks[0].uid == "p-1"
        assert book.op_log_version == 2


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
