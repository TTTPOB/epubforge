"""Unit tests for Stage 3 extract — unit grouping logic."""

from __future__ import annotations

from docling_core.types.doc import DocItemLabel
from docling_core.types.doc.base import BoundingBox

from epubforge.extract import VLMGroupUnit, _AnchorItem, _build_units, _page_trailing_element_label
from epubforge.ir.book_memory import BookMemory


def _anchor(label: DocItemLabel, t: float, b: float | None = None) -> _AnchorItem:
    return {
        "label": label,
        "text": "",
        "bbox": BoundingBox(l=50.0, t=t, r=300.0, b=b if b is not None else t - 20.0),
    }


def _pages(page_nos: list[int], kind: str = "simple") -> list[dict]:
    return [{"page": p, "kind": kind} for p in page_nos]


class TestBuildUnitsSimple:
    def test_simple_pages_batch_together(self) -> None:
        data = _pages([1, 2, 3])
        units = _build_units(data, {})
        assert len(units) == 1
        assert units[0].pages == [1, 2, 3]

    def test_simple_batch_cap(self) -> None:
        data = _pages(list(range(1, 10)))  # 9 simple pages
        units = _build_units(data, {}, max_simple_batch=8)
        assert len(units) == 2
        assert units[0].pages == list(range(1, 9))
        assert units[1].pages == [9]

    def test_simple_non_adjacent_splits(self) -> None:
        data = _pages([1, 3])  # gap at page 2
        units = _build_units(data, {})
        assert len(units) == 2

    def test_cross_kind_always_splits(self) -> None:
        data = [
            {"page": 1, "kind": "simple"},
            {"page": 2, "kind": "complex"},
            {"page": 3, "kind": "simple"},
        ]
        units = _build_units(data, {})
        assert len(units) == 3
        assert units[0].pages == [1]
        assert units[1].pages == [2]
        assert units[2].pages == [3]

    def test_all_units_are_vlm_group(self) -> None:
        data = _pages([1, 2]) + [{"page": 3, "kind": "complex"}]
        units = _build_units(data, {})
        assert all(isinstance(u, VLMGroupUnit) for u in units)


class TestBuildUnitsComplex:
    def _anchors_with_trailing_table(self, pno: int) -> dict:
        """Anchors for a page whose last meaningful element is a TABLE."""
        return {
            pno: [
                _anchor(DocItemLabel.TEXT, t=700.0),
                _anchor(DocItemLabel.TABLE, t=300.0),  # last in reading order (lowest t)
                _anchor(DocItemLabel.FOOTNOTE, t=50.0),  # bottom noise, filtered out
            ]
        }

    def _anchors_with_trailing_text(self, pno: int) -> dict:
        """Anchors for a page whose last meaningful element is a paragraph."""
        return {
            pno: [
                _anchor(DocItemLabel.TEXT, t=700.0),
                _anchor(DocItemLabel.TABLE, t=500.0),
                _anchor(DocItemLabel.TEXT, t=200.0),  # last in reading order
            ]
        }

    def test_complex_merges_when_trailing_table(self) -> None:
        data = [{"page": 10, "kind": "complex"}, {"page": 11, "kind": "complex"}]
        anchors = self._anchors_with_trailing_table(10)
        units = _build_units(data, anchors)
        assert len(units) == 1
        assert units[0].pages == [10, 11]

    def test_complex_breaks_when_trailing_text(self) -> None:
        data = [{"page": 10, "kind": "complex"}, {"page": 11, "kind": "complex"}]
        anchors = self._anchors_with_trailing_text(10)
        units = _build_units(data, anchors)
        assert len(units) == 2

    def test_complex_no_anchors_does_not_merge(self) -> None:
        data = [{"page": 10, "kind": "complex"}, {"page": 11, "kind": "complex"}]
        units = _build_units(data, {})
        assert len(units) == 2

    def test_complex_batch_cap(self) -> None:
        n = 15
        data = [{"page": p, "kind": "complex"} for p in range(1, n + 1)]
        anchors: dict = {}
        for p in range(1, n):
            anchors[p] = [_anchor(DocItemLabel.TABLE, t=300.0)]
        units = _build_units(data, anchors, max_complex_batch=12)
        # First unit: 12 pages; then remaining 3 pages in subsequent units
        assert units[0].pages == list(range(1, 13))

    def test_non_adjacent_complex_not_grouped(self) -> None:
        data = [{"page": 10, "kind": "complex"}, {"page": 12, "kind": "complex"}]
        anchors = self._anchors_with_trailing_table(10)
        units = _build_units(data, anchors)
        assert len(units) == 2


class TestPageTrailingElement:
    def test_table_at_bottom(self) -> None:
        items = [
            _anchor(DocItemLabel.TEXT, t=700.0),
            _anchor(DocItemLabel.TABLE, t=200.0),
        ]
        assert _page_trailing_element_label(items) == DocItemLabel.TABLE

    def test_text_after_table(self) -> None:
        items = [
            _anchor(DocItemLabel.TABLE, t=700.0),
            _anchor(DocItemLabel.TEXT, t=200.0),
        ]
        assert _page_trailing_element_label(items) == DocItemLabel.TEXT

    def test_footnote_in_bottom_25pct_excluded(self) -> None:
        # t spans 50..800, bottom 25% threshold = 50 + 0.25*750 = 237.5
        # footnote at t=60 is in bottom 25% → excluded
        # last meaningful = TEXT at t=300
        items = [
            _anchor(DocItemLabel.TEXT, t=800.0),
            _anchor(DocItemLabel.TEXT, t=300.0),
            _anchor(DocItemLabel.FOOTNOTE, t=60.0),
        ]
        assert _page_trailing_element_label(items) == DocItemLabel.TEXT

    def test_empty_returns_none(self) -> None:
        assert _page_trailing_element_label([]) is None

    def test_skip_labels_excluded(self) -> None:
        items = [
            _anchor(DocItemLabel.PAGE_HEADER, t=800.0),
            _anchor(DocItemLabel.PAGE_FOOTER, t=50.0),
        ]
        assert _page_trailing_element_label(items) is None


class TestBookMemory:
    def test_default_empty(self) -> None:
        m = BookMemory()
        assert m.footnote_callouts == []
        assert m.attribution_templates == []
        assert m.chapter_heading_style is None

    def test_roundtrip_json(self) -> None:
        m = BookMemory(
            footnote_callouts=["①", "②"],
            chapter_heading_style="第N章 标题",
        )
        m2 = BookMemory.model_validate_json(m.model_dump_json())
        assert m2.footnote_callouts == ["①", "②"]
        assert m2.chapter_heading_style == "第N章 标题"
