"""Unit tests for Stage 3 cleaner — reading order, dedup, block delimiters."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from docling_core.types.doc import DocItemLabel, DoclingDocument
from docling_core.types.doc.base import BoundingBox, Size
from docling_core.types.doc.document import PageItem, ProvenanceItem

from epubforge.cleaner import _format_blocks_for_llm, clean_simple_pages
from epubforge.config import Config
from epubforge.ir.semantic import CleanOutput, CleanBlock


def _make_doc(*items: tuple[str, float, int]) -> DoclingDocument:
    """Build a DoclingDocument with text items.

    Each item is (text, bbox_t, page_no). Items are added in provided order so that
    iterate_items() yields them in that order (Docling preserves insertion order).
    """
    doc = DoclingDocument(name="test")
    for pno in {p for _, _, p in items}:
        doc.pages[pno] = PageItem(page_no=pno, size=Size(width=595.0, height=842.0))
    for text, t, pno in items:
        prov = ProvenanceItem(
            page_no=pno,
            bbox=BoundingBox(l=50.0, t=t, r=300.0, b=t - 20.0),
            charspan=(0, 0),
        )
        doc.add_text(label=DocItemLabel.TEXT, text=text, prov=prov)
    return doc


def _pages_json(page_nos: list[int], kind: str = "simple") -> dict:
    return {"pages": [{"page": p, "kind": kind, "element_refs": []} for p in page_nos]}


class TestReadingOrder:
    def test_top_to_bottom_preserved(self, tmp_path: Path) -> None:
        # Items added top-to-bottom (higher t = top in BOTTOMLEFT).
        # iterate_items() should yield them in insertion order (top-to-bottom).
        # Old code sorted by bbox_t ascending = bottom-to-top (wrong).
        doc = _make_doc(("A", 700.0, 1), ("B", 500.0, 1), ("C", 300.0, 1))
        raw_path = tmp_path / "01_raw.json"
        doc.save_as_json(raw_path)

        pages_path = tmp_path / "02_pages.json"
        pages_path.write_text(json.dumps(_pages_json([1])), encoding="utf-8")
        out_dir = tmp_path / "03_simple"
        out_dir.mkdir()

        captured_text: list[str] = []
        fake_result = CleanOutput(blocks=[CleanBlock(kind="paragraph", text="A B C")])

        def fake_chat_parsed(messages, *, response_format, temperature=0.0):
            captured_text.append(messages[-1]["content"])
            return fake_result

        cfg = Config(cache_dir=tmp_path / ".cache")
        with patch("epubforge.cleaner.LLMClient") as mock_cls:
            mock_client = MagicMock()
            mock_client.chat_parsed.side_effect = fake_chat_parsed
            mock_cls.return_value = mock_client
            clean_simple_pages(raw_path, pages_path, out_dir, cfg)

        assert captured_text, "LLM was never called"
        text = captured_text[0]
        # [BLOCK p1] sections should appear in order A, B, C
        pos_a = text.index("[BLOCK p1]\nA")
        pos_b = text.index("[BLOCK p1]\nB")
        pos_c = text.index("[BLOCK p1]\nC")
        assert pos_a < pos_b < pos_c, f"Reading order wrong: A={pos_a}, B={pos_b}, C={pos_c}"

    def test_self_ref_dedup(self, tmp_path: Path) -> None:
        doc = DoclingDocument(name="test")
        doc.pages[1] = PageItem(page_no=1, size=Size(width=595.0, height=842.0))
        # Same item with two provs on the same page (defensive scenario)
        prov1 = ProvenanceItem(
            page_no=1, bbox=BoundingBox(l=50.0, t=700.0, r=300.0, b=680.0), charspan=(0, 0)
        )
        prov2 = ProvenanceItem(
            page_no=1, bbox=BoundingBox(l=50.0, t=600.0, r=300.0, b=580.0), charspan=(0, 0)
        )
        item = doc.add_text(label=DocItemLabel.TEXT, text="unique", prov=prov1)
        # Manually add a second prov to the same item
        item.prov.append(prov2)
        raw_path = tmp_path / "01_raw.json"
        doc.save_as_json(raw_path)

        pages_path = tmp_path / "02_pages.json"
        pages_path.write_text(json.dumps(_pages_json([1])), encoding="utf-8")
        out_dir = tmp_path / "03_simple"
        out_dir.mkdir()

        call_count = 0
        captured_text: list[str] = []

        def fake_chat_parsed(messages, *, response_format, temperature=0.0):
            nonlocal call_count
            call_count += 1
            captured_text.append(messages[-1]["content"])
            return CleanOutput(blocks=[CleanBlock(kind="paragraph", text="unique")])

        cfg = Config(cache_dir=tmp_path / ".cache")
        with patch("epubforge.cleaner.LLMClient") as mock_cls:
            mock_client = MagicMock()
            mock_client.chat_parsed.side_effect = fake_chat_parsed
            mock_cls.return_value = mock_client
            clean_simple_pages(raw_path, pages_path, out_dir, cfg)

        assert call_count == 1
        text = captured_text[0]
        # "unique" should appear exactly once in the LLM input
        assert text.count("unique") == 1, f"Item appeared {text.count('unique')} times"


class TestBlockDelimiters:
    def test_block_tags_emitted(self) -> None:
        items = [
            {"label": DocItemLabel.TEXT, "text": "Hello", "page": 3},
            {"label": DocItemLabel.TEXT, "text": "World", "page": 4},
        ]
        result = _format_blocks_for_llm(items)
        assert "[BLOCK p3]" in result
        assert "[/BLOCK]" in result
        assert "[BLOCK p4]" in result

    def test_skip_labels_excluded(self) -> None:
        items = [
            {"label": DocItemLabel.PAGE_HEADER, "text": "Header", "page": 1},
            {"label": DocItemLabel.TEXT, "text": "Body", "page": 1},
        ]
        result = _format_blocks_for_llm(items)
        assert "Header" not in result
        assert "Body" in result

    def test_heading_prefix_included(self) -> None:
        items = [{"label": DocItemLabel.SECTION_HEADER, "text": "Chapter 1", "page": 2}]
        result = _format_blocks_for_llm(items)
        assert "[SECTION_HEADER] Chapter 1" in result
