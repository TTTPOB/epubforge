"""Tests for epub_builder image embedding and footnote renumbering."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from epubforge.epub_builder import _render_chapter, build_epub
from epubforge.ir.semantic import Book, Chapter, Figure, Footnote, Paragraph, Provenance


def _prov(page: int, source: str = "vlm") -> Provenance:
    return Provenance(page=page, source=source)  # type: ignore[arg-type]


def _figure(page: int, caption: str = "") -> Figure:
    return Figure(caption=caption, provenance=_prov(page))


# ---------------------------------------------------------------------------
# Commit 2 — image embedding
# ---------------------------------------------------------------------------


def test_render_chapter_figure_resolved() -> None:
    """Figure with resolved filename renders <img src='images/...'>."""
    fig = _figure(5, "A chart")
    chapter = Chapter(title="Ch1", blocks=[fig])
    fname = "p0005_foo.png"
    body_html, _ = _render_chapter(chapter, {id(fig): fname})
    assert f'<img src="images/{fname}"' in body_html
    assert "figcaption" in body_html


def test_render_chapter_figure_unresolved() -> None:
    """Figure with no disk match renders <figcaption> but no <img>."""
    fig = _figure(7, "A chart")
    chapter = Chapter(title="Ch1", blocks=[fig])
    body_html, _ = _render_chapter(chapter, {})
    assert "<img" not in body_html
    assert "figcaption" in body_html


def test_build_epub_includes_images(tmp_path: Path) -> None:
    """build_epub packages image files and writes <img> references into chapter HTML."""
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "p0005_foo.png").write_bytes(b"PNG1")
    (images_dir / "p0005_bar.png").write_bytes(b"PNG2")

    fig1 = _figure(5, "Figure 1")
    fig2 = _figure(5, "Figure 2")
    book = Book(title="Test", chapters=[Chapter(title="Ch1", blocks=[fig1, fig2])])

    semantic = tmp_path / "semantic.json"
    semantic.write_text(book.model_dump_json(), encoding="utf-8")

    out_epub = tmp_path / "test.epub"
    build_epub(semantic, out_epub, images_dir=images_dir)

    with zipfile.ZipFile(out_epub) as zf:
        names = zf.namelist()
        img_entries = [n for n in names if "images/" in n and n.endswith(".png")]
        assert len(img_entries) == 2

        xhtml_entries = [n for n in names if n.endswith(".xhtml") and "chap" in n]
        assert xhtml_entries
        content = zf.read(xhtml_entries[0]).decode()
        assert content.count('<img src="images/') == 2


def test_build_epub_no_images_dir(tmp_path: Path) -> None:
    """build_epub without images_dir emits no <img> tags but still builds epub."""
    fig = _figure(5, "A chart")
    book = Book(title="Test", chapters=[Chapter(title="Ch1", blocks=[fig])])

    semantic = tmp_path / "semantic.json"
    semantic.write_text(book.model_dump_json(), encoding="utf-8")

    out_epub = tmp_path / "test.epub"
    build_epub(semantic, out_epub)

    with zipfile.ZipFile(out_epub) as zf:
        names = zf.namelist()
        img_entries = [n for n in names if "images/" in n and n.endswith(".png")]
        assert len(img_entries) == 0
