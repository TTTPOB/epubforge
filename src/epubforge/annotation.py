"""Small, dependency-light PDF page annotation renderer.

The renderer accepts already validated page-coordinate boxes.  Callers that
use another coordinate space must scale their boxes before calling it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pymupdf


ANNOTATION_COLORS: dict[str, tuple[float, float, float]] = {
    "FIGURE": (220 / 255, 38 / 255, 38 / 255),
    "CAPTION": (234 / 255, 88 / 255, 12 / 255),
    "BODY": (22 / 255, 101 / 255, 52 / 255),
    "FOOTNOTE": (126 / 255, 34 / 255, 206 / 255),
    "TABLE": (2 / 255, 132 / 255, 199 / 255),
    "FORMULA": (190 / 255, 24 / 255, 93 / 255),
}

# Keep annotation geometry explicit so visual review and tests share one contract.
ANNOTATED_BBOX_STROKE_WIDTH = 4.0
ANNOTATED_LABEL_FONT_NAME = "helv"
ANNOTATED_LABEL_FONT_SIZE = 12.0
ANNOTATED_LABEL_PADDING = 2.0
ANNOTATED_LABEL_GAP = 2.0
ANNOTATED_LABEL_FILL_OPACITY = 0.5
ANNOTATED_LABEL_BLACK = (0.0, 0.0, 0.0)
ANNOTATED_LABEL_WHITE = (1.0, 1.0, 1.0)


def label_text_color(color: Sequence[float]) -> tuple[float, float, float]:
    """Return readable black or white text for an RGB fill color."""
    luminance = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
    return ANNOTATED_LABEL_BLACK if luminance > 0.5 else ANNOTATED_LABEL_WHITE


def label_geometry(
    label: str,
    candidate_rect: pymupdf.Rect,
    *,
    page_width: float,
    page_height: float,
) -> tuple[pymupdf.Rect, tuple[float, float], float] | None:
    """Place a label above or below a box while keeping it on the page."""
    font = pymupdf.Font(fontname=ANNOTATED_LABEL_FONT_NAME)
    label_width = (
        pymupdf.get_text_length(
            label,
            fontname=ANNOTATED_LABEL_FONT_NAME,
            fontsize=ANNOTATED_LABEL_FONT_SIZE,
        )
        + 2.0 * ANNOTATED_LABEL_PADDING
    )
    label_height = (
        ANNOTATED_LABEL_FONT_SIZE * (font.ascender - font.descender)
        + 2.0 * ANNOTATED_LABEL_PADDING
    )
    if label_width > page_width or label_height > page_height:
        return None

    label_x = min(
        max(0.0, float(candidate_rect.x0)),
        max(0.0, page_width - label_width),
    )
    above_y = float(candidate_rect.y0) - ANNOTATED_LABEL_GAP - label_height
    below_y = float(candidate_rect.y1) + ANNOTATED_LABEL_GAP
    if above_y >= 0.0:
        label_y = above_y
    elif below_y + label_height <= page_height:
        label_y = below_y
    else:
        label_y = min(max(0.0, above_y), page_height - label_height)

    rect = pymupdf.Rect(label_x, label_y, label_x + label_width, label_y + label_height)
    origin = (
        label_x + ANNOTATED_LABEL_PADDING,
        label_y + ANNOTATED_LABEL_PADDING + ANNOTATED_LABEL_FONT_SIZE * font.ascender,
    )
    return rect, origin, ANNOTATED_LABEL_FONT_SIZE


def draw_annotation(
    page: pymupdf.Page,
    box: Mapping[str, Any],
    *,
    page_width: float,
    page_height: float,
) -> None:
    """Draw one stable ID/order/type annotation on *page*."""
    box_type = str(box["type"])
    color = ANNOTATION_COLORS.get(box_type, (0.1, 0.1, 0.1))
    candidate_rect = pymupdf.Rect(
        float(box["x0"]),
        float(box["y0"]),
        float(box["x1"]),
        float(box["y1"]),
    )
    page.draw_rect(
        candidate_rect,
        color=color,
        width=ANNOTATED_BBOX_STROKE_WIDTH,
    )
    label = f"{box['reading_order']} {box['id']} {box_type}"
    geometry = label_geometry(
        label,
        candidate_rect,
        page_width=page_width,
        page_height=page_height,
    )
    if geometry is None:
        return
    label_rect, text_origin, font_size = geometry
    page.draw_rect(
        label_rect,
        color=None,
        fill=color,
        fill_opacity=ANNOTATED_LABEL_FILL_OPACITY,
        width=0,
    )
    page.insert_text(
        text_origin,
        label,
        fontname=ANNOTATED_LABEL_FONT_NAME,
        fontsize=font_size,
        color=label_text_color(color),
    )


__all__ = [
    "ANNOTATED_BBOX_STROKE_WIDTH",
    "ANNOTATED_LABEL_BLACK",
    "ANNOTATED_LABEL_FILL_OPACITY",
    "ANNOTATED_LABEL_FONT_NAME",
    "ANNOTATED_LABEL_FONT_SIZE",
    "ANNOTATED_LABEL_GAP",
    "ANNOTATED_LABEL_PADDING",
    "ANNOTATED_LABEL_WHITE",
    "ANNOTATION_COLORS",
    "draw_annotation",
    "label_geometry",
    "label_text_color",
]
