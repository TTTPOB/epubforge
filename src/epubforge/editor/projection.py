"""Read-only projection renderer for Book IR — Phase 5.

Pure rendering functions that produce agent-friendly Markdown-ish projection
text from Book IR objects.  No disk I/O, no Book mutation, no workdir dependency.

Public API
----------
render_index(book, *, source, exported_at)
    Render a full-book index.md string.

render_chapter_projection(chapter)
    Render a single Chapter to its projection string.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from epubforge.ir.semantic import (
    Block,
    Book,
    Chapter,
    Equation,
    Figure,
    Footnote,
    Heading,
    Paragraph,
    Table,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provenance_meta(prov: Any) -> dict[str, Any]:
    """Extract non-null fields from a Provenance object as a flat dict."""
    meta: dict[str, Any] = {"source": prov.source}
    for field in ("bbox", "raw_ref", "raw_label", "artifact_id", "evidence_ref"):
        val = getattr(prov, field, None)
        if val is not None:
            meta[field] = val
    return meta


def _count_table_rows(html: str) -> int:
    """Return the number of <tr> elements in *html*."""
    return len(re.findall(r"<tr\b", html, re.IGNORECASE))


def _count_table_cols(html: str) -> int:
    """Estimate column count from the first <tr> in *html*."""
    m = re.search(r"<tr[^>]*>(.*?)</tr>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return 0
    cols = 0
    for cell_match in re.finditer(r"<t[dh][^>]*>", m.group(1), re.IGNORECASE):
        tag = cell_match.group(0)
        cs = re.search(r'colspan\s*=\s*["\'](\d+)["\']', tag, re.IGNORECASE)
        cols += int(cs.group(1)) if cs else 1
    return cols


# ---------------------------------------------------------------------------
# Block metadata builders
# ---------------------------------------------------------------------------


def _block_metadata(block: Block) -> dict[str, Any]:
    """Build the JSON-serialisable metadata dict for a block marker."""
    kind = block.kind
    meta: dict[str, Any] = {
        "uid": block.uid,
        "kind": kind,
        "page": block.provenance.page,
    }

    if kind == "paragraph":
        assert isinstance(block, Paragraph)
        meta["role"] = block.role
        if block.cross_page:
            meta["cross_page"] = True
        meta["provenance"] = _provenance_meta(block.provenance)

    elif kind == "heading":
        assert isinstance(block, Heading)
        meta["level"] = block.level
        if block.id is not None:
            meta["heading_id"] = block.id
        meta["provenance"] = _provenance_meta(block.provenance)

    elif kind == "footnote":
        assert isinstance(block, Footnote)
        meta["callout"] = block.callout
        meta["paired"] = block.paired
        if block.orphan:
            meta["orphan"] = True
        meta["provenance"] = _provenance_meta(block.provenance)

    elif kind == "figure":
        assert isinstance(block, Figure)
        meta["provenance"] = _provenance_meta(block.provenance)

    elif kind == "table":
        assert isinstance(block, Table)
        meta["multi_page"] = block.multi_page
        meta["num_rows"] = _count_table_rows(block.html)
        meta["num_cols"] = _count_table_cols(block.html)
        if block.multi_page and block.merge_record is not None:
            meta["num_segments"] = len(block.merge_record.segment_pages)
            meta["segment_pages"] = block.merge_record.segment_pages
        meta["provenance"] = _provenance_meta(block.provenance)

    elif kind == "equation":
        assert isinstance(block, Equation)
        meta["provenance"] = _provenance_meta(block.provenance)

    return meta


def _block_content(block: Block) -> str:
    """Return the content text for a block (may be multi-line)."""
    kind = block.kind
    if kind == "paragraph":
        assert isinstance(block, Paragraph)
        return block.text
    if kind == "heading":
        assert isinstance(block, Heading)
        return block.text
    if kind == "footnote":
        assert isinstance(block, Footnote)
        return block.text
    if kind == "figure":
        assert isinstance(block, Figure)
        if block.image_ref:
            return f"![{block.caption}]({block.image_ref})"
        return block.caption
    if kind == "table":
        assert isinstance(block, Table)
        parts: list[str] = [block.html]
        if block.table_title:
            parts.append(f"**Table title:** {block.table_title}")
        if block.caption:
            parts.append(f"**Caption:** {block.caption}")
        if block.multi_page and block.merge_record is not None:
            parts.append(
                f"**Merge record:** segments: {len(block.merge_record.segment_pages)}, "
                f"pages: {block.merge_record.segment_pages}, "
                f"order: {block.merge_record.segment_order}"
            )
        return "\n\n".join(parts)
    if kind == "equation":
        assert isinstance(block, Equation)
        return block.latex
    return ""


def _block_marker_line(block: Block) -> str:
    """Render a ``[[block <uid>]] {json}`` marker line."""
    meta = _block_metadata(block)
    uid = block.uid or ""
    return f"[[block {uid}]] {json.dumps(meta, ensure_ascii=False, separators=(",", ":"))}"


def _chapter_page_range(chapter: Chapter) -> list[int]:
    """Return [min_page, max_page] across all blocks, or empty list."""
    pages = [b.provenance.page for b in chapter.blocks if b.provenance is not None]
    if not pages:
        return []
    return [min(pages), max(pages)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_index(
    book: Book,
    *,
    source: str = "edit_state/book.json",
    exported_at: str | None = None,
) -> str:
    """Render the full-book index.md content from *book*.

    Parameters
    ----------
    book:
        The Book instance to render.
    source:
        Label for the source file (embedded in the book marker).
    exported_at:
        ISO-8601 timestamp string.  Defaults to current UTC time.

    Returns
    -------
    Markdown-ish index content as a single string.
    """
    if exported_at is None:
        exported_at = datetime.now(timezone.utc).isoformat()

    lines: list[str] = []

    # -- Book marker -------------------------------------------------------
    book_meta: dict[str, Any] = {
        "title": book.title,
        "authors": book.authors,
        "exported_at": exported_at,
        "source": source,
        "chapters": len(book.chapters),
    }
    lines.append(f"[[book]] {json.dumps(book_meta, ensure_ascii=False, separators=(",", ":"))}")
    lines.append("")
    lines.append("## Chapters")
    lines.append("")
    lines.append("| # | UID | Title | Blocks | Pages |")
    lines.append("|---|-----|-------|--------|-------|")

    for i, ch in enumerate(book.chapters, start=1):
        uid = ch.uid or ""
        page_range = _chapter_page_range(ch)
        pages_str = (
            f"{page_range[0]}-{page_range[1]}"
            if len(page_range) == 2
            else "-"
        )
        lines.append(f"| {i} | {uid} | {ch.title} | {len(ch.blocks)} | {pages_str} |")

    lines.append("")
    return "\n".join(lines)


def render_chapter_projection(chapter: Chapter) -> str:
    """Render a single *chapter* to its projection text.

    Parameters
    ----------
    chapter:
        The Chapter object to render.

    Returns
    -------
    Markdown-ish projection content as a single string.
    """
    lines: list[str] = []

    uid = chapter.uid or ""
    lines.append(f"# Chapter: {chapter.title} [{uid}]")
    lines.append("")

    ch_meta: dict[str, Any] = {
        "title": chapter.title,
        "blocks": len(chapter.blocks),
    }
    page_range = _chapter_page_range(chapter)
    if page_range:
        ch_meta["page_range"] = page_range

    lines.append(
        f"[[chapter {uid}]] {json.dumps(ch_meta, ensure_ascii=False, separators=(",", ":"))}"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    for block in chapter.blocks:
        marker = _block_marker_line(block)
        content = _block_content(block)
        lines.append(marker)
        lines.append(content)
        lines.append("")  # blank separator between blocks

    return "\n".join(lines)
