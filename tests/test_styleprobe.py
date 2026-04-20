"""Unit tests for styleprobe — no VLM, no network."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from epubforge.styleprobe import (
    _assign_direct_role,
    _bucket,
    _compute_features,
    _estimate_line_pitch,
    _make_signature,
    _stratified_sample,
)
from epubforge.ir.semantic import StyleRole
from docling_core.types.doc.base import CoordOrigin


def _fake_bbox(l, t, r, b, origin=CoordOrigin.BOTTOMLEFT):
    m = MagicMock()
    m.l = l
    m.t = t
    m.r = r
    m.b = b
    m.coord_origin = origin
    return m


def _fake_block(text, label, page_w, page_h, bbox, content_layer="body"):
    return {
        "self_ref": "#/texts/0",
        "label": label,
        "content_layer": content_layer,
        "text": text,
        "page_no": 1,
        "page_w": page_w,
        "page_h": page_h,
        "bbox": bbox,
        "bbox_list": [bbox.l, bbox.t, bbox.r, bbox.b],
        "coord_origin": bbox.coord_origin,
    }


class TestBucket:
    def test_xs(self):
        assert _bucket(0.5, [(0.80, "xs"), (0.95, "s"), (1.15, "m"), (1.50, "l")], "xl") == "xs"

    def test_m(self):
        assert _bucket(1.0, [(0.80, "xs"), (0.95, "s"), (1.15, "m"), (1.50, "l")], "xl") == "m"

    def test_xl(self):
        assert _bucket(2.0, [(0.80, "xs"), (0.95, "s"), (1.15, "m"), (1.50, "l")], "xl") == "xl"


class TestAssignDirectRole:
    PAGE_W, PAGE_H = 595.0, 841.9

    def test_furniture_defaults_to_page_footer(self):
        bbox = _fake_bbox(60, 60, 80, 50)
        b = _fake_block("1", "page_footer", self.PAGE_W, self.PAGE_H, bbox, content_layer="furniture")
        assert _assign_direct_role(b) == StyleRole.page_footer

    def test_furniture_header(self):
        bbox = _fake_bbox(60, 830, 400, 820)
        b = _fake_block("running head", "page_header", self.PAGE_W, self.PAGE_H, bbox, content_layer="furniture")
        assert _assign_direct_role(b) == StyleRole.page_header

    def test_section_header_maps_to_section_title(self):
        bbox = _fake_bbox(60, 700, 300, 680)
        b = _fake_block("1.1 some section", "section_header", self.PAGE_W, self.PAGE_H, bbox)
        assert _assign_direct_role(b) == StyleRole.section_title

    def test_footnote_maps_directly(self):
        bbox = _fake_bbox(60, 120, 400, 100)
        b = _fake_block("① 脚注", "footnote", self.PAGE_W, self.PAGE_H, bbox)
        assert _assign_direct_role(b) == StyleRole.footnote

    def test_caption_maps_directly(self):
        bbox = _fake_bbox(60, 400, 400, 380)
        b = _fake_block("Fig 1: ...", "caption", self.PAGE_W, self.PAGE_H, bbox)
        assert _assign_direct_role(b) == StyleRole.caption

    def test_list_item_maps_directly(self):
        bbox = _fake_bbox(60, 400, 400, 380)
        b = _fake_block("1. item", "list_item", self.PAGE_W, self.PAGE_H, bbox)
        assert _assign_direct_role(b) == StyleRole.list_item

    def test_plain_text_returns_none(self):
        bbox = _fake_bbox(60, 600, 535, 550)
        b = _fake_block("body text", "text", self.PAGE_W, self.PAGE_H, bbox)
        assert _assign_direct_role(b) is None


class TestComputeFeatures:
    PAGE_W = 595.0
    PAGE_H = 841.9

    def test_body_paragraph_bottomleft(self):
        bbox = _fake_bbox(l=60, t=600.0, r=535.0, b=550.0, origin=CoordOrigin.BOTTOMLEFT)
        block = _fake_block("Normal body paragraph.", "text", self.PAGE_W, self.PAGE_H, bbox)
        f = _compute_features(block, 12.0)
        assert f["font_scale"] > 0
        assert f["width"] > 0.5
        assert f["is_vertical"] is False

    def test_positive_height_from_bl_coords(self):
        bbox = _fake_bbox(l=60, t=700.0, r=400.0, b=686.0, origin=CoordOrigin.BOTTOMLEFT)
        block = _fake_block("x", "text", self.PAGE_W, self.PAGE_H, bbox)
        f = _compute_features(block, 12.0)
        assert f["font_scale"] > 0

    def test_pure_number_pattern(self):
        bbox = _fake_bbox(l=290, t=50, r=305, b=40)
        block = _fake_block("42", "text", self.PAGE_W, self.PAGE_H, bbox)
        f = _compute_features(block, 12.0)
        assert f["pat"] == "pure_num"

    def test_dash_tail_pattern(self):
        bbox = _fake_bbox(l=100, t=500, r=400, b=488)
        block = _fake_block("某处所说 —— 张三", "text", self.PAGE_W, self.PAGE_H, bbox)
        f = _compute_features(block, 12.0)
        assert f["pat"] == "dash_tail"

    def test_vertical_text_detection(self):
        # Tall narrow block with chars
        bbox = _fake_bbox(l=100, t=700, r=120, b=300)
        block = _fake_block("垂直排文字甲乙丙丁", "text", self.PAGE_W, self.PAGE_H, bbox)
        f = _compute_features(block, 12.0)
        assert f["is_vertical"] is True

    def test_area_based_font_scale_for_large_heading(self):
        # Large single character, 36pt tall, 40pt wide — should give large font_scale.
        bbox = _fake_bbox(l=250, t=750, r=290, b=714)
        block = _fake_block("章", "text", self.PAGE_W, self.PAGE_H, bbox)
        f = _compute_features(block, 12.0)
        # sqrt(36 * 40 / 1) = sqrt(1440) ≈ 37.9 → scale ≈ 3.16
        assert f["font_scale"] > 2.0


class TestMakeSignature:
    def _base_features(self, **overrides) -> dict:
        f = {
            "font_scale": 1.0,
            "width": 0.9,
            "x_center": 0.5,
            "left_indent": 0.1,
            "right_indent": 0.1,
            "line_count": 5,
            "is_vertical": False,
            "char_count": 100,
            "pat": "none",
        }
        f.update(overrides)
        return f

    def test_signature_has_required_fields(self):
        sig = _make_signature(self._base_features())
        for field in ("fs=", "w=", "cx=", "il=", "ir=", "sl=", "orient=", "pat="):
            assert field in sig

    def test_no_label_field_in_signature(self):
        # refactor: label dropped since we only cluster TEXT blocks now
        sig = _make_signature(self._base_features())
        assert "label=" not in sig

    def test_sl_bucketing(self):
        assert "sl=single" in _make_signature(self._base_features(line_count=1))
        assert "sl=few" in _make_signature(self._base_features(line_count=3))
        assert "sl=many" in _make_signature(self._base_features(line_count=10))

    def test_vertical_orient(self):
        assert "orient=vert" in _make_signature(self._base_features(is_vertical=True))
        assert "orient=horiz" in _make_signature(self._base_features(is_vertical=False))


class TestStratifiedSample:
    def test_fewer_than_k(self):
        blocks = [{"page_no": i} for i in range(2)]
        result = _stratified_sample([0, 1], blocks, k=3)
        assert result == [0, 1]

    def test_samples_k_items(self):
        blocks = [{"page_no": i} for i in range(10)]
        result = _stratified_sample(list(range(10)), blocks, k=3)
        assert len(result) == 3

    def test_samples_spread_across_pages(self):
        blocks = [{"page_no": i} for i in range(100)]
        result = _stratified_sample(list(range(100)), blocks, k=3)
        pages = [blocks[i]["page_no"] for i in result]
        assert pages[0] < pages[1] < pages[2]


class TestEstimateLinePitch:
    def test_returns_median_of_short_text_blocks(self):
        blocks = []
        for h in [10.0, 11.0, 12.0, 10.5, 11.5]:
            bbox = _fake_bbox(l=60, t=60 + h, r=400, b=60, origin=CoordOrigin.BOTTOMLEFT)
            blocks.append({
                "label": "text",
                "text": "short",
                "bbox": bbox,
                "coord_origin": CoordOrigin.BOTTOMLEFT,
            })
        pitch = _estimate_line_pitch(blocks)
        assert 10.0 <= pitch <= 12.0

    def test_ignores_non_text_labels(self):
        blocks = []
        bbox = _fake_bbox(l=0, t=500, r=400, b=0, origin=CoordOrigin.BOTTOMLEFT)
        blocks.append({"label": "section_header", "text": "title", "bbox": bbox, "coord_origin": CoordOrigin.BOTTOMLEFT})
        for h in [10.0, 11.0]:
            bbox2 = _fake_bbox(l=60, t=60 + h, r=400, b=60, origin=CoordOrigin.BOTTOMLEFT)
            blocks.append({"label": "text", "text": "x" * 10, "bbox": bbox2, "coord_origin": CoordOrigin.BOTTOMLEFT})
        pitch = _estimate_line_pitch(blocks)
        assert pitch < 50
