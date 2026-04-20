"""Shared PDF page / block rendering helpers.

Centralises the BOTTOMLEFT → TOPLEFT coordinate flip and JPEG-base64 encoding
used by both the VLM reader (stage 4) and the style probe. Callers pass the
raw :class:`BoundingBox` together with its :class:`CoordOrigin`; no external
pre-flipping is needed (or tolerated).
"""

from __future__ import annotations

import base64
import io

import fitz  # PyMuPDF
from docling_core.types.doc.base import BoundingBox, CoordOrigin
from PIL import Image, ImageDraw


def render_page(doc: fitz.Document, page_no: int, *, dpi: int = 150) -> tuple[str, str]:
    """Render a 1-indexed PDF page → (base64_jpg, ``"image/jpeg"``)."""
    page = doc[page_no - 1]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    buf = io.BytesIO(pix.tobytes("jpg", jpg_quality=75))
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


def render_page_with_highlight(
    doc: fitz.Document,
    page_no: int,
    bbox: BoundingBox,
    coord_origin: CoordOrigin,
    *,
    dpi: int = 120,
    stroke_rgb: tuple[int, int, int] = (255, 0, 0),
    stroke_width: int = 4,
) -> tuple[str, str]:
    """Render a page and outline ``bbox`` with a red rectangle.

    Lower default DPI than :func:`render_block_crop` — we just need enough
    resolution to see overall layout and the highlight, not glyph detail.
    """
    page = doc[page_no - 1]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    im = Image.open(io.BytesIO(pix.tobytes("ppm"))).convert("RGB")

    page_h = page.rect.height
    if coord_origin == CoordOrigin.BOTTOMLEFT:
        top_pt = page_h - bbox.t
        bot_pt = page_h - bbox.b
    else:
        top_pt = bbox.t
        bot_pt = bbox.b

    scale = dpi / 72
    lx = bbox.l * scale
    rx = bbox.r * scale
    ly = top_pt * scale
    ry = bot_pt * scale

    draw = ImageDraw.Draw(im)
    draw.rectangle([(lx, ly), (rx, ry)], outline=stroke_rgb, width=stroke_width)

    out = io.BytesIO()
    im.save(out, format="JPEG", quality=75)
    return base64.b64encode(out.getvalue()).decode(), "image/jpeg"


def render_block_crop(
    doc: fitz.Document,
    page_no: int,
    bbox: BoundingBox,
    coord_origin: CoordOrigin,
    *,
    margin_pt: float = 10.0,
    dpi: int = 150,
) -> tuple[str, str]:
    """Render a zoomed crop around ``bbox`` padded by ``margin_pt`` on each side."""
    page = doc[page_no - 1]
    page_h = page.rect.height

    if coord_origin == CoordOrigin.BOTTOMLEFT:
        t = page_h - bbox.t
        b_coord = page_h - bbox.b
    else:
        t = bbox.t
        b_coord = bbox.b

    l = bbox.l - margin_pt
    r = bbox.r + margin_pt
    t = t - margin_pt
    b_coord = b_coord + margin_pt

    clip = fitz.Rect(
        max(0, l), max(0, t),
        min(page.rect.width, r), min(page_h, b_coord),
    )
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, clip=clip)
    buf = io.BytesIO(pix.tobytes("jpg", jpg_quality=80))
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
