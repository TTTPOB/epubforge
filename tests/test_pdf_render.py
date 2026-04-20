"""Smoke test for epubforge.pdf_render — local only, no network."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import fitz
import pytest
from docling_core.types.doc.base import BoundingBox, CoordOrigin

from epubforge.pdf_render import (
    render_block_crop,
    render_page,
    render_page_with_highlight,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "zxgb.pdf"


@pytest.fixture(scope="module")
def doc() -> Iterator[fitz.Document]:
    if not FIXTURE.exists():
        pytest.skip(f"fixture not found: {FIXTURE}")
    d = fitz.open(str(FIXTURE))
    yield d
    d.close()


def _decoded_size(pair: tuple[str, str]) -> int:
    b64, mime = pair
    assert mime == "image/jpeg"
    return len(base64.b64decode(b64))


def test_render_page_returns_jpeg(doc: fitz.Document) -> None:
    assert _decoded_size(render_page(doc, 1)) > 1024


def test_render_page_with_highlight_returns_jpeg(doc: fitz.Document) -> None:
    page_h = doc[0].rect.height
    bbox = BoundingBox(l=50, t=page_h - 100, r=300, b=page_h - 150, coord_origin=CoordOrigin.BOTTOMLEFT)
    pair = render_page_with_highlight(doc, 1, bbox, CoordOrigin.BOTTOMLEFT)
    assert _decoded_size(pair) > 1024


def test_render_block_crop_returns_jpeg(doc: fitz.Document) -> None:
    page_h = doc[0].rect.height
    bbox = BoundingBox(l=50, t=page_h - 100, r=300, b=page_h - 150, coord_origin=CoordOrigin.BOTTOMLEFT)
    pair = render_block_crop(doc, 1, bbox, CoordOrigin.BOTTOMLEFT)
    assert _decoded_size(pair) > 1024
