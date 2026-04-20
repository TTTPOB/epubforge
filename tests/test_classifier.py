"""Unit tests for the page classifier (no real PDF required)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from epubforge.classifier import classify_pages, _is_multicolumn


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_item(label: str, page_no: int, ref: str = "#/ref", bbox: dict | None = None) -> dict:
    prov_bbox = bbox or {"l": 50.0, "t": 100.0, "r": 300.0, "b": 120.0}
    return {
        "self_ref": ref,
        "label": label,
        "prov": [{"page_no": page_no, "bbox": prov_bbox}],
    }


def _raw_doc(texts=(), tables=(), pictures=(), pages=None) -> dict:
    pages = pages or {
        "1": {"size": {"width": 595.0, "height": 842.0}},
        "2": {"size": {"width": 595.0, "height": 842.0}},
    }
    return {
        "texts": list(texts),
        "tables": list(tables),
        "pictures": list(pictures),
        "pages": pages,
    }


def _run(tmp_path: Path, raw: dict) -> list[dict]:
    raw_file = tmp_path / "01_raw.json"
    out_file = tmp_path / "02_pages.json"
    raw_file.write_text(json.dumps(raw), encoding="utf-8")
    classify_pages(raw_file, out_file)
    result = json.loads(out_file.read_text(encoding="utf-8"))
    return result["pages"]


# ── tests ──────────────────────────────────────────────────────────────────────

class TestSimplePage:
    def test_text_only_page_is_simple(self, tmp_path: Path) -> None:
        raw = _raw_doc(
            texts=[
                _make_item("text", 1, "#/text/0"),
                _make_item("text", 1, "#/text/1"),
            ]
        )
        pages = _run(tmp_path, raw)
        assert any(p["page"] == 1 and p["kind"] == "simple" for p in pages)

    def test_section_header_is_simple(self, tmp_path: Path) -> None:
        raw = _raw_doc(texts=[_make_item("section_header", 1)])
        pages = _run(tmp_path, raw)
        assert pages[0]["kind"] == "simple"


class TestComplexPage:
    def test_table_makes_complex(self, tmp_path: Path) -> None:
        raw = _raw_doc(tables=[_make_item("table", 1)])
        pages = _run(tmp_path, raw)
        assert pages[0]["kind"] == "complex"

    def test_picture_makes_complex(self, tmp_path: Path) -> None:
        raw = _raw_doc(pictures=[_make_item("picture", 1)])
        pages = _run(tmp_path, raw)
        assert pages[0]["kind"] == "complex"

    def test_footnote_makes_complex(self, tmp_path: Path) -> None:
        raw = _raw_doc(texts=[_make_item("footnote", 1)])
        pages = _run(tmp_path, raw)
        assert pages[0]["kind"] == "complex"

    def test_formula_makes_complex(self, tmp_path: Path) -> None:
        raw = _raw_doc(texts=[_make_item("formula", 1)])
        pages = _run(tmp_path, raw)
        assert pages[0]["kind"] == "complex"


class TestMultiPage:
    def test_different_pages_classified_independently(self, tmp_path: Path) -> None:
        raw = _raw_doc(
            texts=[
                _make_item("text", 1, "#/text/0"),
                _make_item("footnote", 2, "#/text/1"),
            ]
        )
        pages = _run(tmp_path, raw)
        by_page = {p["page"]: p["kind"] for p in pages}
        assert by_page[1] == "simple"
        assert by_page[2] == "complex"

    def test_element_refs_collected(self, tmp_path: Path) -> None:
        raw = _raw_doc(
            texts=[_make_item("text", 1, "#/text/0"), _make_item("text", 1, "#/text/1")]
        )
        pages = _run(tmp_path, raw)
        p1 = next(p for p in pages if p["page"] == 1)
        assert "#/text/0" in p1["element_refs"]
        assert "#/text/1" in p1["element_refs"]


class TestMultiColumn:
    def test_single_column_not_flagged(self) -> None:
        # All bboxes in similar x range (left column only)
        bboxes = [(50.0, 300.0)] * 6
        assert not _is_multicolumn(bboxes, 595.0)

    def test_two_columns_flagged(self) -> None:
        # Left column: x_mid ≈ 150, right column: x_mid ≈ 450 (gap > 5% of 595)
        left = [(50.0, 250.0)] * 4   # mid=150
        right = [(320.0, 560.0)] * 4  # mid=440
        assert _is_multicolumn(left + right, 595.0)

    def test_too_few_bboxes_ignored(self) -> None:
        left = [(50.0, 250.0)] * 2
        right = [(320.0, 560.0)] * 2
        assert not _is_multicolumn(left + right, 595.0)
