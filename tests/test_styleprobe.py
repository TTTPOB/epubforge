"""Unit tests for styleprobe — no VLM, no network."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from epubforge.styleprobe import (
    _bucket,
    _compute_features,
    _estimate_line_pitch,
    _make_signature,
    _stratified_sample,
)
from docling_core.types.doc.base import CoordOrigin


def _fake_bbox(l, t, r, b, origin=CoordOrigin.BOTTOMLEFT):
    m = MagicMock()
    m.l = l
    m.t = t
    m.r = r
    m.b = b
    m.coord_origin = origin
    return m


def _fake_block(text, label, page_w, page_h, bbox):
    return {
        "self_ref": "#/texts/0",
        "label": label,
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


class TestComputeFeatures:
    PAGE_W = 595.0
    PAGE_H = 841.9

    def test_body_paragraph_bottomleft(self):
        # Simulate a body paragraph in BL coords: t > b, normal width, middle of page
        bbox = _fake_bbox(
            l=60, t=600.0, r=535.0, b=550.0,
            origin=CoordOrigin.BOTTOMLEFT,
        )
        block = _fake_block("Normal body paragraph text.", "text", self.PAGE_W, self.PAGE_H, bbox)
        pitch = 12.0
        f = _compute_features(block, pitch)
        # height in TL = t_BL - b_BL = 600 - 550 = 50; top_TL = page_h - t_BL = 841.9 - 600 = 241.9
        assert f["font_scale"] > 0, "font_scale must be positive"
        assert 0.0 < f["top"] < 1.0
        assert f["width"] > 0.5, "wide block"

    def test_chapter_title_pattern(self):
        bbox = _fake_bbox(l=200, t=750.0, r=395.0, b=720.0, origin=CoordOrigin.BOTTOMLEFT)
        block = _fake_block("第一章 绪论", "section_header", self.PAGE_W, self.PAGE_H, bbox)
        f = _compute_features(block, 12.0)
        assert f["starts_chapter"] is True

    def test_footnote_marker_pattern(self):
        bbox = _fake_bbox(l=60, t=120.0, r=300.0, b=105.0, origin=CoordOrigin.BOTTOMLEFT)
        block = _fake_block("① 这是一个脚注。", "footnote", self.PAGE_W, self.PAGE_H, bbox)
        f = _compute_features(block, 12.0)
        assert f["looks_footnote_marker"] is True

    def test_positive_height_from_bl_coords(self):
        # Key invariant: height_px must be positive for BL coords where t > b
        bbox = _fake_bbox(l=60, t=700.0, r=400.0, b=686.0, origin=CoordOrigin.BOTTOMLEFT)
        block = _fake_block("x", "text", self.PAGE_W, self.PAGE_H, bbox)
        f = _compute_features(block, 12.0)
        assert f["font_scale"] > 0


class TestMakeSignature:
    def test_signature_format(self):
        f = {
            "font_scale": 1.0,
            "width": 0.9,
            "x_center": 0.5,
            "top": 0.3,
            "left_indent": 0.1,
            "right_indent": 0.1,
            "line_count": 5,
            "is_single_line": False,
            "starts_chapter": False,
            "ends_author": False,
            "looks_footnote_marker": False,
            "char_count": 100,
            "label_bucket": "text",
        }
        sig = _make_signature(f)
        assert sig.startswith("label=text|")
        assert "fs=" in sig
        assert "w=" in sig

    def test_chapter_pattern_in_sig(self):
        f = {
            "font_scale": 1.4,
            "width": 0.3,
            "x_center": 0.5,
            "top": 0.05,
            "left_indent": 0.35,
            "right_indent": 0.35,
            "line_count": 1,
            "is_single_line": True,
            "starts_chapter": True,
            "ends_author": False,
            "looks_footnote_marker": False,
            "char_count": 8,
            "label_bucket": "heading",
        }
        sig = _make_signature(f)
        assert "pat=chapter" in sig
        assert "label=heading" in sig


class TestStratifiedSample:
    def test_fewer_than_k(self):
        blocks = [{"page_no": i} for i in range(2)]
        result = _stratified_sample([0, 1], blocks, k=3)
        assert result == [0, 1]

    def test_samples_k_items(self):
        blocks = [{"page_no": i} for i in range(10)]
        indices = list(range(10))
        result = _stratified_sample(indices, blocks, k=3)
        assert len(result) == 3

    def test_samples_spread_across_pages(self):
        blocks = [{"page_no": i} for i in range(100)]
        indices = list(range(100))
        result = _stratified_sample(indices, blocks, k=3)
        pages = [blocks[i]["page_no"] for i in result]
        assert pages[0] < pages[1] < pages[2], "should sample from different parts"


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
        # Big non-text block should be ignored
        bbox = _fake_bbox(l=0, t=500, r=400, b=0, origin=CoordOrigin.BOTTOMLEFT)
        blocks.append({"label": "section_header", "text": "title", "bbox": bbox, "coord_origin": CoordOrigin.BOTTOMLEFT})
        # Normal text blocks
        for h in [10.0, 11.0]:
            bbox2 = _fake_bbox(l=60, t=60 + h, r=400, b=60, origin=CoordOrigin.BOTTOMLEFT)
            blocks.append({"label": "text", "text": "x" * 10, "bbox": bbox2, "coord_origin": CoordOrigin.BOTTOMLEFT})
        pitch = _estimate_line_pitch(blocks)
        assert pitch < 50, "big non-text block should not pollute median"
