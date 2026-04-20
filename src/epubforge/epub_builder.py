"""Stage 6 — EPUB3 generation from Semantic IR. Implement in epubforge-cjc."""

from __future__ import annotations

from pathlib import Path


def build_epub(semantic_path: Path, out_path: Path) -> None:
    """Build an EPUB3 file from the Semantic IR at *semantic_path*.

    One XHTML file per top-level chapter.
    Footnotes use epub:type='footnote' + <aside> popups.
    Figures: <figure><img/><figcaption/></figure>, images in Images/.
    Tables: VLM HTML inlined directly.
    nav.xhtml (EPUB3) + toc.ncx (EPUB2 compat) dual TOC.
    """
    raise NotImplementedError("TODO: implement in epubforge-cjc")
