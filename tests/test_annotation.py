from __future__ import annotations

import pymupdf
import pytest

from epubforge.annotation import (
    ANNOTATION_COLORS,
    ANNOTATED_LABEL_BLACK,
    ANNOTATED_LABEL_WHITE,
    draw_annotation,
    label_geometry,
    label_text_color,
)


def test_label_text_color_chooses_contrasting_foreground() -> None:
    assert label_text_color((0.9, 0.9, 0.9)) == ANNOTATED_LABEL_BLACK
    assert label_text_color((0.1, 0.1, 0.1)) == ANNOTATED_LABEL_WHITE


def test_label_geometry_stays_inside_page_or_declines_tiny_pages() -> None:
    geometry = label_geometry(
        "1 p0001-body BODY",
        pymupdf.Rect(150, 0, 190, 20),
        page_width=200,
        page_height=120,
    )

    assert geometry is not None
    label_rect, origin, _font_size = geometry
    assert 0 <= label_rect.x0 < label_rect.x1 <= 200
    assert 0 <= label_rect.y0 < label_rect.y1 <= 120
    assert label_rect.x0 <= origin[0] <= label_rect.x1
    assert label_rect.y0 <= origin[1] <= label_rect.y1
    assert (
        label_geometry(
            "long label", pymupdf.Rect(0, 0, 1, 1), page_width=2, page_height=2
        )
        is None
    )


def test_draw_annotation_renders_the_candidate_stroke() -> None:
    document = pymupdf.open()
    try:
        page = document.new_page(width=200, height=120)
        draw_annotation(
            page,
            {
                "type": "FIGURE",
                "x0": 20,
                "y0": 40,
                "x1": 100,
                "y1": 90,
                "reading_order": 1,
                "id": "p0001-figure-demo",
            },
            page_width=200,
            page_height=120,
        )
        pixmap = page.get_pixmap(alpha=False)
        red, green, blue = pixmap.pixel(20, 40)[:3]
    finally:
        document.close()

    expected = ANNOTATION_COLORS["FIGURE"]
    assert red > green and red > blue
    assert red / 255 == pytest.approx(expected[0], abs=0.1)
