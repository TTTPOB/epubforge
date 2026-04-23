"""Tests for epub_builder image embedding and footnote renumbering."""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from epubforge.epub_builder import _render_chapter, build_epub
from epubforge.ir.semantic import Book, Chapter, Figure, Footnote, Paragraph, Provenance


def _figure(prov: Callable[..., Provenance], page: int, caption: str = "") -> Figure:
    return Figure(caption=caption, provenance=prov(page, source="vlm"))


# ---------------------------------------------------------------------------
# Commit 2 — image embedding
# ---------------------------------------------------------------------------


def test_render_chapter_figure_resolved(prov) -> None:
    """Figure with resolved filename renders <img src='images/...'>."""
    fig = _figure(prov, 5, "A chart")
    chapter = Chapter(title="Ch1", blocks=[fig])
    fname = "p0005_foo.png"
    body_html, _ = _render_chapter(chapter, "chap0000", {id(fig): fname})
    assert f'<img src="images/{fname}"' in body_html
    assert "figcaption" in body_html


def test_render_chapter_figure_unresolved(prov) -> None:
    """Figure with no disk match renders <figcaption> but no <img>."""
    fig = _figure(prov, 7, "A chart")
    chapter = Chapter(title="Ch1", blocks=[fig])
    body_html, _ = _render_chapter(chapter, "chap0000", {})
    assert "<img" not in body_html
    assert "figcaption" in body_html


def test_build_epub_includes_images(prov, tmp_path: Path) -> None:
    """build_epub packages image files and writes <img> references into chapter HTML."""
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "p0005_foo.png").write_bytes(b"PNG1")
    (images_dir / "p0005_bar.png").write_bytes(b"PNG2")

    fig1 = _figure(prov, 5, "Figure 1")
    fig2 = _figure(prov, 5, "Figure 2")
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


# ---------------------------------------------------------------------------
# Commit 3 — per-chapter footnote renumbering
# ---------------------------------------------------------------------------


def test_render_chapter_footnotes_numbered(prov) -> None:
    """Footnotes are numbered 1, 2, 3 per chapter regardless of original callout."""
    fn1 = Footnote(callout="①", text="Note 1", paired=True, provenance=prov(1, source="llm"))
    fn2 = Footnote(callout="*", text="Note 2", paired=True, provenance=prov(2, source="llm"))
    fn3 = Footnote(callout="②", text="Note 3", paired=True, provenance=prov(3, source="llm"))

    p1 = Paragraph(text=f"Text \x02fn-1-①\x03 more", provenance=prov(1, source="llm"))
    p2 = Paragraph(text=f"Text \x02fn-2-*\x03 more", provenance=prov(2, source="llm"))
    p3 = Paragraph(text=f"Text \x02fn-3-②\x03 more", provenance=prov(3, source="llm"))

    chapter = Chapter(title="Ch1", blocks=[p1, fn1, p2, fn2, p3, fn3])
    body_html, fn_html = _render_chapter(chapter, "ch1", {})

    # Inline noteref markers must use sequential numbers
    assert ">1<" in body_html
    assert ">2<" in body_html
    assert ">3<" in body_html
    # Original callouts must not appear as noteref labels
    assert ">①<" not in body_html
    assert ">*<" not in body_html
    assert ">②<" not in body_html

    # Footnote anchors must use per-chapter ids
    assert 'id="ch1-fn1"' in fn_html
    assert 'id="ch1-fn2"' in fn_html
    assert 'id="ch1-fn3"' in fn_html


def test_build_epub_no_images_dir(prov, tmp_path: Path) -> None:
    """build_epub without images_dir emits no <img> tags but still builds epub."""
    fig = _figure(prov, 5, "A chart")
    book = Book(title="Test", chapters=[Chapter(title="Ch1", blocks=[fig])])

    semantic = tmp_path / "semantic.json"
    semantic.write_text(book.model_dump_json(), encoding="utf-8")

    out_epub = tmp_path / "test.epub"
    build_epub(semantic, out_epub)

    with zipfile.ZipFile(out_epub) as zf:
        names = zf.namelist()
        img_entries = [n for n in names if "images/" in n and n.endswith(".png")]
        assert len(img_entries) == 0
