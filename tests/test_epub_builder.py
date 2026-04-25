"""Tests for epub_builder image embedding and footnote renumbering."""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from epubforge.epub_builder import _render_chapter, build_epub, resolve_build_source
from epubforge.ir.semantic import (
    Book,
    Chapter,
    ExtractionMetadata,
    Figure,
    Footnote,
    Paragraph,
    Provenance,
    Table,
)
from epubforge.stage3_artifacts import (
    Stage3ActivePointer,
    Stage3ContractError,
)


def _figure(
    prov: Callable[..., Provenance],
    page: int,
    caption: str = "",
    image_ref: str | None = None,
) -> Figure:
    return Figure(
        caption=caption, image_ref=image_ref, provenance=prov(page, source="vlm")
    )


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
    """build_epub packages image files via image_ref and writes <img> references."""
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "p0005_foo.png").write_bytes(b"PNG1")
    (images_dir / "p0005_bar.png").write_bytes(b"PNG2")

    fig1 = _figure(prov, 5, "Figure 1", image_ref="p0005_foo.png")
    fig2 = _figure(prov, 5, "Figure 2", image_ref="p0005_bar.png")
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
    fn1 = Footnote(
        callout="①", text="Note 1", paired=True, provenance=prov(1, source="llm")
    )
    fn2 = Footnote(
        callout="*", text="Note 2", paired=True, provenance=prov(2, source="llm")
    )
    fn3 = Footnote(
        callout="②", text="Note 3", paired=True, provenance=prov(3, source="llm")
    )

    p1 = Paragraph(text="Text \x02fn-1-①\x03 more", provenance=prov(1, source="llm"))
    p2 = Paragraph(text="Text \x02fn-2-*\x03 more", provenance=prov(2, source="llm"))
    p3 = Paragraph(text="Text \x02fn-3-②\x03 more", provenance=prov(3, source="llm"))

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


# ---------------------------------------------------------------------------
# resolve_build_source priority
# ---------------------------------------------------------------------------


def test_resolve_build_source_prefers_edit_state(tmp_path: Path) -> None:
    """edit_state/book.json is returned when all three candidates exist."""
    (tmp_path / "edit_state").mkdir()
    editable = tmp_path / "edit_state" / "book.json"
    editable.write_text("{}", encoding="utf-8")
    (tmp_path / "05_semantic.json").write_text("{}", encoding="utf-8")
    (tmp_path / "05_semantic_raw.json").write_text("{}", encoding="utf-8")

    assert resolve_build_source(tmp_path) == editable


def test_resolve_build_source_falls_back_to_semantic(tmp_path: Path) -> None:
    """05_semantic.json is returned when edit_state/book.json does not exist."""
    semantic = tmp_path / "05_semantic.json"
    semantic.write_text("{}", encoding="utf-8")
    (tmp_path / "05_semantic_raw.json").write_text("{}", encoding="utf-8")

    assert resolve_build_source(tmp_path) == semantic


def test_resolve_build_source_falls_back_to_raw(tmp_path: Path) -> None:
    """05_semantic_raw.json is returned when neither of the first two candidates exist."""
    raw = tmp_path / "05_semantic_raw.json"
    raw.write_text("{}", encoding="utf-8")

    assert resolve_build_source(tmp_path) == raw


def test_resolve_build_source_raises_when_none_exist(tmp_path: Path) -> None:
    """FileNotFoundError is raised when no candidate exists."""
    with pytest.raises(FileNotFoundError):
        resolve_build_source(tmp_path)


# ---------------------------------------------------------------------------
# Stale manifest check
# ---------------------------------------------------------------------------


def _write_active_pointer(work_dir: Path, manifest_sha256: str) -> None:
    """Write a minimal active_manifest.json and a stub manifest.json for the given sha.

    The manifest.json content is crafted so that its sha256 equals manifest_sha256,
    allowing load_active_stage3_manifest to succeed and return the pointer.
    """
    artifact_id = "abc123"
    extract_dir = work_dir / "03_extract"
    artifact_dir = extract_dir / "artifacts" / artifact_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Build a real Stage3Manifest whose serialised sha matches manifest_sha256.
    # We write dummy content to get a specific sha by using a padding trick:
    # just write the sha as a comment field is not possible directly, so we
    # instead create the manifest JSON and compute its real sha.
    from epubforge.stage3_artifacts import Stage3Manifest

    manifest = Stage3Manifest(
        mode="docling",
        artifact_id=artifact_id,
        artifact_dir=f"03_extract/artifacts/{artifact_id}",
        created_at="2026-01-01T00:00:00Z",
        raw_sha256="r" * 64,
        pages_sha256="p" * 64,
        source_pdf="source/source.pdf",
        source_pdf_sha256="s" * 64,
        selected_pages=[1],
        toc_pages=[],
        complex_pages=[],
        page_filter=None,
        unit_files=[],
        sidecars={},
        settings={"contract_version": 3},
    )
    manifest_text = manifest.model_dump_json(indent=2)
    actual_sha = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
    (artifact_dir / "manifest.json").write_text(manifest_text, encoding="utf-8")

    pointer = Stage3ActivePointer(
        schema_version=3,
        active_artifact_id=artifact_id,
        manifest_path=f"03_extract/artifacts/{artifact_id}/manifest.json",
        manifest_sha256=actual_sha,  # actual sha so load succeeds
        activated_at="2026-01-01T00:00:00Z",
    )
    (extract_dir / "active_manifest.json").write_text(
        pointer.model_dump_json(indent=2), encoding="utf-8"
    )
    return actual_sha  # type: ignore[return-value]


def test_build_matching_manifest_succeeds(prov, tmp_path: Path) -> None:
    """build_epub succeeds when source manifest sha matches the active manifest."""
    # _write_active_pointer returns the actual manifest sha
    actual_sha = _write_active_pointer(tmp_path, "")  # sha param unused now
    book = Book(
        title="T",
        chapters=[Chapter(title="C", blocks=[])],
        extraction=ExtractionMetadata(stage3_manifest_sha256=actual_sha),
    )
    semantic = tmp_path / "05_semantic_raw.json"
    semantic.write_text(book.model_dump_json(), encoding="utf-8")

    out_epub = tmp_path / "out.epub"
    # Should not raise — shas match
    build_epub(semantic, out_epub)
    assert out_epub.exists()


def test_build_stale_manifest_raises(prov, tmp_path: Path) -> None:
    """build_epub raises Stage3ContractError when manifest sha mismatches."""
    _write_active_pointer(tmp_path, "")  # sets up a valid active manifest
    # Book records a DIFFERENT sha than the active manifest
    book = Book(
        title="T",
        chapters=[Chapter(title="C", blocks=[])],
        extraction=ExtractionMetadata(stage3_manifest_sha256="a" * 64),
    )
    semantic = tmp_path / "05_semantic_raw.json"
    semantic.write_text(book.model_dump_json(), encoding="utf-8")

    out_epub = tmp_path / "out.epub"
    with pytest.raises(Stage3ContractError, match="stale"):
        build_epub(semantic, out_epub)


def test_build_no_active_manifest_skips_check(prov, tmp_path: Path) -> None:
    """build_epub proceeds when source has a sha but no active manifest exists."""
    book = Book(
        title="T",
        chapters=[Chapter(title="C", blocks=[])],
        extraction=ExtractionMetadata(stage3_manifest_sha256="dead" * 15 + "dead"),
    )
    semantic = tmp_path / "05_semantic_raw.json"
    semantic.write_text(book.model_dump_json(), encoding="utf-8")
    # No active_manifest.json written

    out_epub = tmp_path / "out.epub"
    build_epub(semantic, out_epub)
    assert out_epub.exists()


# ---------------------------------------------------------------------------
# Figure image_ref mapping
# ---------------------------------------------------------------------------


def test_figure_image_ref_works(prov, tmp_path: Path) -> None:
    """Figure with image_ref pointing to an existing file is mapped correctly."""
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "p0003_a.png").write_bytes(b"PNG")

    fig = _figure(prov, 3, "Caption", image_ref="p0003_a.png")
    chapter = Chapter(title="C", blocks=[fig])
    book = Book(title="T", chapters=[chapter])

    semantic = tmp_path / "semantic.json"
    semantic.write_text(book.model_dump_json(), encoding="utf-8")
    out_epub = tmp_path / "out.epub"
    build_epub(semantic, out_epub, images_dir=images_dir)

    with zipfile.ZipFile(out_epub) as zf:
        names = zf.namelist()
        assert any("p0003_a.png" in n for n in names)
        xhtml = [n for n in names if "chap" in n and n.endswith(".xhtml")]
        content = zf.read(xhtml[0]).decode()
        assert 'src="images/p0003_a.png"' in content


def test_figure_missing_image_ref_logs_warning_no_crash(
    prov, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Figure with no image_ref logs a warning but does not crash."""
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "p0001_a.png").write_bytes(b"PNG")

    # image_ref is None — should NOT register the image
    fig = _figure(prov, 1, "Some figure", image_ref=None)
    chapter = Chapter(title="C", blocks=[fig])
    book = Book(title="T", chapters=[chapter])

    semantic = tmp_path / "semantic.json"
    semantic.write_text(book.model_dump_json(), encoding="utf-8")
    out_epub = tmp_path / "out.epub"

    import logging

    with caplog.at_level(logging.WARNING, logger="epubforge.epub_builder"):
        build_epub(semantic, out_epub, images_dir=images_dir)

    assert out_epub.exists()
    assert any("no image_ref" in r.message for r in caplog.records)

    with zipfile.ZipFile(out_epub) as zf:
        img_entries = [
            n for n in zf.namelist() if "images/" in n and n.endswith(".png")
        ]
        assert len(img_entries) == 0


def test_figure_image_ref_file_missing_logs_warning(
    prov, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Figure with image_ref pointing to non-existent file logs warning, no crash."""
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    # File does NOT exist on disk

    fig = _figure(prov, 2, "A figure", image_ref="p0002_nonexistent.png")
    chapter = Chapter(title="C", blocks=[fig])
    book = Book(title="T", chapters=[chapter])

    semantic = tmp_path / "semantic.json"
    semantic.write_text(book.model_dump_json(), encoding="utf-8")
    out_epub = tmp_path / "out.epub"

    import logging

    with caplog.at_level(logging.WARNING, logger="epubforge.epub_builder"):
        build_epub(semantic, out_epub, images_dir=images_dir)

    assert out_epub.exists()
    assert any("not found on disk" in r.message for r in caplog.records)


def test_figure_no_ordinal_fallback(prov, tmp_path: Path) -> None:
    """Figure on a page that has images but no image_ref is NOT given an image."""
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "p0005_a.png").write_bytes(b"PNG")

    # Figure on page 5 but image_ref=None — ordinal fallback must NOT happen
    fig = _figure(prov, 5, "No ref", image_ref=None)
    chapter = Chapter(title="C", blocks=[fig])
    body_html, _ = _render_chapter(chapter, "c", {})
    assert "<img" not in body_html


# ---------------------------------------------------------------------------
# Table caption with footnote marker
# ---------------------------------------------------------------------------


def test_table_caption_footnote_renders(prov) -> None:
    """Table.caption with footnote marker is rendered as a linked noteref."""
    fn = Footnote(
        callout="1", text="Note text", paired=True, provenance=prov(10, source="llm")
    )
    table = Table(
        html="<table><tr><td>Data</td></tr></table>",
        table_title="Title",
        caption="See note \x02fn-10-1\x03 for details.",
        provenance=prov(10, source="llm"),
    )
    chapter = Chapter(title="C", blocks=[fn, table])
    body_html, _ = _render_chapter(chapter, "ch", {})

    # The caption should contain a noteref link, not the raw marker
    assert "\x02fn-10-1\x03" not in body_html
    assert 'epub:type="noteref"' in body_html
    assert "table-caption" in body_html


# ---------------------------------------------------------------------------
# Borrowed footnote pre-scan includes table caption/html/title
# ---------------------------------------------------------------------------


def test_borrowed_footnote_scan_table_caption(prov) -> None:
    """Footnote marker in Table.caption is found during cross-chapter pre-scan."""
    # Chapter 0 has the table (with a footnote marker in the caption)
    # Chapter 1 has the actual Footnote block
    fn = Footnote(
        callout="x", text="Cross note", paired=True, provenance=prov(20, source="llm")
    )
    table = Table(
        html="<table></table>",
        caption="See \x02fn-20-x\x03.",
        provenance=prov(1, source="docling"),
    )
    ch0 = Chapter(title="C0", blocks=[table])

    # Use build_epub indirectly via _render_chapter with manually-constructed fn_map
    # The footnote should be borrowed into ch0 and rendered there
    body_html, _ = _render_chapter(
        ch0, "c0", {}, borrowed_footnotes=[fn], borrowed_keys=None
    )

    assert "table-caption" in body_html
    assert 'epub:type="noteref"' in body_html
    assert "\x02fn-20-x\x03" not in body_html


# ---------------------------------------------------------------------------
# docling_*_candidate roles render as paragraphs
# ---------------------------------------------------------------------------


def test_candidate_role_renders_as_paragraph(prov) -> None:
    """Paragraph with docling_heading_candidate role renders as <p>, not <h2>."""
    p = Paragraph(
        text="Looks like a heading",
        role="docling_heading_candidate",
        provenance=prov(1, source="docling"),
    )
    chapter = Chapter(title="C", blocks=[p])
    body_html, _ = _render_chapter(chapter, "c", {})

    assert "<p" in body_html
    assert "<h" not in body_html.split("<h1")[1] if "<h1" in body_html else True
    assert "docling_heading_candidate" in body_html  # kept as CSS class


def test_candidate_roles_not_converted_to_semantics(prov) -> None:
    """docling_*_candidate blocks remain <p> elements for footnote, list, and table roles."""
    blocks = [
        Paragraph(
            text="fn-like",
            role="docling_footnote_candidate",
            provenance=prov(1, source="docling"),
        ),
        Paragraph(
            text="list-like",
            role="docling_list_candidate",
            provenance=prov(2, source="docling"),
        ),
        Paragraph(
            text="table-like",
            role="docling_table_candidate",
            provenance=prov(3, source="docling"),
        ),
    ]
    chapter = Chapter(title="C", blocks=blocks)
    body_html, fn_html = _render_chapter(chapter, "c", {})

    # All three rendered as <p>
    assert body_html.count("<p") == 3
    # No <aside epub:type="footnote"> generated from candidates
    assert "footnote" not in fn_html
    # No <table> generated from candidates
    assert "<table" not in body_html
