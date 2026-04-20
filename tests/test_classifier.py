"""Unit tests for the page classifier (no real PDF required)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docling_core.types.doc import (
    DocItemLabel,
    DoclingDocument,
    FormulaItem,
    PageItem,
    PictureItem,
    ProvenanceItem,
    SectionHeaderItem,
    TableItem,
    TextItem,
)
from docling_core.types.doc.base import BoundingBox, Size
from docling_core.types.doc.document import TableData

from epubforge.classifier import classify_pages, _is_multicolumn


# ── helpers ────────────────────────────────────────────────────────────────────

_DEFAULT_BBOX = BoundingBox(l=50.0, t=100.0, r=300.0, b=120.0)


def _text(label: DocItemLabel, page_no: int, ref: str = "#/texts/0",
          bbox: BoundingBox | None = None) -> TextItem | SectionHeaderItem | FormulaItem:
    """Build a text-collection item with the appropriate concrete type for *label*."""
    b = bbox or _DEFAULT_BBOX
    prov = [ProvenanceItem(page_no=page_no, bbox=b, charspan=(0, 0))]
    if label == DocItemLabel.SECTION_HEADER:
        return SectionHeaderItem(self_ref=ref, orig="", text="", prov=prov)
    if label == DocItemLabel.FORMULA:
        return FormulaItem(self_ref=ref, orig="", text="", prov=prov)
    # label is a TextItem-compatible value at this point
    return TextItem(self_ref=ref, label=label, orig="", text="", prov=prov)  # type: ignore[arg-type]


def _table(page_no: int, ref: str = "#/tables/0",
           bbox: BoundingBox | None = None) -> TableItem:
    b = bbox or _DEFAULT_BBOX
    return TableItem(
        self_ref=ref, label=DocItemLabel.TABLE,
        prov=[ProvenanceItem(page_no=page_no, bbox=b, charspan=(0, 0))],
        data=TableData(num_rows=0, num_cols=0, table_cells=[]),
    )


def _picture(page_no: int, ref: str = "#/pictures/0",
             bbox: BoundingBox | None = None) -> PictureItem:
    b = bbox or _DEFAULT_BBOX
    return PictureItem(
        self_ref=ref, label=DocItemLabel.PICTURE,
        prov=[ProvenanceItem(page_no=page_no, bbox=b, charspan=(0, 0))],
    )


_DEFAULT_PAGES = {
    1: PageItem(page_no=1, size=Size(width=595.0, height=842.0)),
    2: PageItem(page_no=2, size=Size(width=595.0, height=842.0)),
}


def _doc(texts=(), tables=(), pictures=(), pages=None) -> DoclingDocument:
    return DoclingDocument(
        name="test",
        texts=list(texts),
        tables=list(tables),
        pictures=list(pictures),
        pages=pages or _DEFAULT_PAGES,
    )


def _run(tmp_path: Path, doc: DoclingDocument) -> list[dict]:
    raw_file = tmp_path / "01_raw.json"
    out_file = tmp_path / "02_pages.json"
    doc.save_as_json(raw_file)
    classify_pages(raw_file, out_file)
    return json.loads(out_file.read_text(encoding="utf-8"))["pages"]


# ── tests ──────────────────────────────────────────────────────────────────────

class TestSimplePage:
    def test_text_only_page_is_simple(self, tmp_path: Path) -> None:
        doc = _doc(texts=[
            _text(DocItemLabel.TEXT, 1, "#/texts/0"),
            _text(DocItemLabel.TEXT, 1, "#/texts/1"),
        ])
        pages = _run(tmp_path, doc)
        assert any(p["page"] == 1 and p["kind"] == "simple" for p in pages)

    def test_section_header_is_simple(self, tmp_path: Path) -> None:
        doc = _doc(texts=[_text(DocItemLabel.SECTION_HEADER, 1)])
        pages = _run(tmp_path, doc)
        assert pages[0]["kind"] == "simple"


class TestComplexPage:
    def test_table_makes_complex(self, tmp_path: Path) -> None:
        doc = _doc(tables=[_table(1)])
        pages = _run(tmp_path, doc)
        assert pages[0]["kind"] == "complex"

    def test_picture_makes_complex(self, tmp_path: Path) -> None:
        doc = _doc(pictures=[_picture(1)])
        pages = _run(tmp_path, doc)
        assert pages[0]["kind"] == "complex"

    def test_footnote_makes_complex(self, tmp_path: Path) -> None:
        doc = _doc(texts=[_text(DocItemLabel.FOOTNOTE, 1)])
        pages = _run(tmp_path, doc)
        assert pages[0]["kind"] == "complex"

    def test_formula_makes_complex(self, tmp_path: Path) -> None:
        doc = _doc(texts=[_text(DocItemLabel.FORMULA, 1)])
        pages = _run(tmp_path, doc)
        assert pages[0]["kind"] == "complex"


class TestMultiPage:
    def test_different_pages_classified_independently(self, tmp_path: Path) -> None:
        doc = _doc(texts=[
            _text(DocItemLabel.TEXT, 1, "#/texts/0"),
            _text(DocItemLabel.FOOTNOTE, 2, "#/texts/1"),
        ])
        pages = _run(tmp_path, doc)
        by_page = {p["page"]: p["kind"] for p in pages}
        assert by_page[1] == "simple"
        assert by_page[2] == "complex"

    def test_element_refs_collected(self, tmp_path: Path) -> None:
        doc = _doc(texts=[
            _text(DocItemLabel.TEXT, 1, "#/texts/0"),
            _text(DocItemLabel.TEXT, 1, "#/texts/1"),
        ])
        pages = _run(tmp_path, doc)
        p1 = next(p for p in pages if p["page"] == 1)
        assert "#/texts/0" in p1["element_refs"]
        assert "#/texts/1" in p1["element_refs"]


class TestMultiColumn:
    def test_single_column_not_flagged(self) -> None:
        bboxes = [(50.0, 300.0)] * 6
        assert not _is_multicolumn(bboxes, 595.0)

    def test_two_columns_flagged(self) -> None:
        left = [(50.0, 250.0)] * 4   # mid=150
        right = [(320.0, 560.0)] * 4  # mid=440
        assert _is_multicolumn(left + right, 595.0)

    def test_too_few_bboxes_ignored(self) -> None:
        left = [(50.0, 250.0)] * 2
        right = [(320.0, 560.0)] * 2
        assert not _is_multicolumn(left + right, 595.0)
