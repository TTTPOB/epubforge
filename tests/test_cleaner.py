"""Unit tests for Stage 3 extract — reading order, dedup, block delimiters."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from docling_core.types.doc import DocItemLabel, DoclingDocument
from docling_core.types.doc.base import BoundingBox, Size
from docling_core.types.doc.document import PageItem, ProvenanceItem

from epubforge.extract import _format_blocks_for_llm, _build_page_items, _build_units, LLMGroupUnit, VLMPageUnit
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
        page_items = _build_page_items(doc, {1})

        # Items should appear in insertion order: A, B, C
        texts = [it["text"] for it in page_items.get(1, [])]
        assert texts == ["A", "B", "C"], f"Reading order wrong: {texts}"

    def test_self_ref_dedup(self, tmp_path: Path) -> None:
        doc = DoclingDocument(name="test")
        doc.pages[1] = PageItem(page_no=1, size=Size(width=595.0, height=842.0))
        prov1 = ProvenanceItem(
            page_no=1, bbox=BoundingBox(l=50.0, t=700.0, r=300.0, b=680.0), charspan=(0, 0)
        )
        prov2 = ProvenanceItem(
            page_no=1, bbox=BoundingBox(l=50.0, t=600.0, r=300.0, b=580.0), charspan=(0, 0)
        )
        item = doc.add_text(label=DocItemLabel.TEXT, text="unique", prov=prov1)
        # Manually add a second prov to the same item
        item.prov.append(prov2)

        page_items = _build_page_items(doc, {1})
        texts = [it["text"] for it in page_items.get(1, [])]
        assert texts.count("unique") == 1, f"Item appeared {texts.count('unique')} times"


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


class TestBuildUnits:
    def test_interleaved_simple_complex(self) -> None:
        pages_data = [
            {"page": 1, "kind": "simple"},
            {"page": 2, "kind": "simple"},
            {"page": 3, "kind": "complex"},
            {"page": 4, "kind": "simple"},
        ]
        page_items = {
            1: [{"label": DocItemLabel.TEXT, "text": "a", "page": 1}],
            2: [{"label": DocItemLabel.TEXT, "text": "b", "page": 2}],
            4: [{"label": DocItemLabel.TEXT, "text": "c", "page": 4}],
        }
        units = _build_units(pages_data, page_items)
        assert len(units) == 4
        assert isinstance(units[0], LLMGroupUnit) and units[0].pages == [1]
        assert isinstance(units[1], LLMGroupUnit) and units[1].pages == [2]
        assert isinstance(units[2], VLMPageUnit) and units[2].pages == [3]
        assert isinstance(units[3], LLMGroupUnit) and units[3].pages == [4]

    def test_every_page_is_its_own_unit(self) -> None:
        pages_data = [
            {"page": 1, "kind": "simple"},
            {"page": 2, "kind": "simple"},
            {"page": 3, "kind": "simple"},
        ]
        page_items = {
            1: [{"label": DocItemLabel.TEXT, "text": "body", "page": 1}],
            2: [{"label": DocItemLabel.SECTION_HEADER, "text": "Chapter 2", "page": 2}],
            3: [{"label": DocItemLabel.TEXT, "text": "text", "page": 3}],
        }
        units = _build_units(pages_data, page_items)
        assert len(units) == 3
        assert isinstance(units[0], LLMGroupUnit) and units[0].pages == [1]
        assert isinstance(units[1], LLMGroupUnit) and units[1].pages == [2]
        assert isinstance(units[2], LLMGroupUnit) and units[2].pages == [3]

    def test_adjacent_complex_both_with_tables_grouped(self) -> None:
        pages_data = [
            {"page": 10, "kind": "complex"},
            {"page": 11, "kind": "complex"},
        ]
        units = _build_units(pages_data, {}, pages_with_tables={10, 11})
        assert len(units) == 1
        assert isinstance(units[0], VLMPageUnit) and units[0].pages == [10, 11]

    def test_adjacent_complex_only_one_with_table_not_grouped(self) -> None:
        pages_data = [
            {"page": 10, "kind": "complex"},
            {"page": 11, "kind": "complex"},
        ]
        units = _build_units(pages_data, {}, pages_with_tables={10})
        assert len(units) == 2
        assert isinstance(units[0], VLMPageUnit) and units[0].pages == [10]
        assert isinstance(units[1], VLMPageUnit) and units[1].pages == [11]

    def test_complex_pages_separated_by_simple_not_grouped(self) -> None:
        pages_data = [
            {"page": 10, "kind": "complex"},
            {"page": 11, "kind": "simple"},
            {"page": 12, "kind": "complex"},
        ]
        units = _build_units(pages_data, {}, pages_with_tables={10, 12})
        assert len(units) == 3
        assert isinstance(units[0], VLMPageUnit) and units[0].pages == [10]
        assert isinstance(units[1], LLMGroupUnit) and units[1].pages == [11]
        assert isinstance(units[2], VLMPageUnit) and units[2].pages == [12]

    def test_four_adjacent_complex_all_with_tables_grouped(self) -> None:
        pages_data = [{"page": p, "kind": "complex"} for p in range(5, 9)]
        units = _build_units(pages_data, {}, pages_with_tables={5, 6, 7, 8})
        assert len(units) == 1
        assert isinstance(units[0], VLMPageUnit) and units[0].pages == [5, 6, 7, 8]

    def test_non_adjacent_complex_pages_not_grouped(self) -> None:
        pages_data = [
            {"page": 10, "kind": "complex"},
            {"page": 12, "kind": "complex"},  # gap: page 11 missing
        ]
        units = _build_units(pages_data, {}, pages_with_tables={10, 12})
        assert len(units) == 2
        assert units[0].pages == [10]
        assert units[1].pages == [12]
