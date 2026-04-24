"""Cross-page table merge audit detector.

Detects structural anomalies introduced by assembler._merge_continued_tables():
  - table.merge_width_drift        column count differs significantly across merged segments
  - table.merge_header_reintroduced  multiple <thead> sections survive in the merged HTML
  - table.merge_record_incomplete  multi_page table has no usable tbody content
  - table.merge_orphan_continuation  continuation=True but multi_page=False (assembler found no predecessor)
"""

from __future__ import annotations

import re

from epubforge.audit._html import COLSPAN_RE, ROW_RE, TBODY_RE
from epubforge.audit.models import AuditBundle, AuditIssue
from epubforge.ir.semantic import Book, Table
from epubforge.query import find_blocks


THEAD_RE = re.compile(r"<thead\b[^>]*>.*?</thead>", re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(r"<t[dh]\b([^>]*)>", re.IGNORECASE)

# Column width drift threshold: flag when any tbody segment's column count
# deviates from the reference (first segment) by more than this fraction.
_WIDTH_DRIFT_THRESHOLD = 0.25


def _count_row_logical_width(row_html: str) -> int:
    """Return the logical column width of a single <tr> (sum of colspan)."""
    width = 0
    for attrs_match in CELL_RE.finditer(row_html):
        attrs = attrs_match.group(1)
        colspan_match = COLSPAN_RE.search(attrs)
        width += int(colspan_match.group(1)) if colspan_match else 1
    return width


def _modal_width(tbody_html: str) -> int | None:
    """Return the most common logical row width in a tbody block."""
    rows = ROW_RE.findall(tbody_html)
    if not rows:
        return None
    widths: dict[int, int] = {}
    for row in rows:
        w = _count_row_logical_width(row)
        if w > 0:
            widths[w] = widths.get(w, 0) + 1
    if not widths:
        return None
    return max(widths, key=lambda w: widths[w])


def detect_table_merge_issues(book: Book) -> AuditBundle:
    """Detect cross-page merge structural anomalies across all Table blocks.

    Checks only *already-set* metadata fields for consistency — does NOT auto-identify
    potential continuation candidates from structural inference.
    """
    issues: list[AuditIssue] = []

    for ref in find_blocks(book, kinds={"table"}):
        block = ref.block
        assert isinstance(block, Table)

        # --- merge_record / multi_page consistency (explicit metadata only) ---
        if block.multi_page and block.merge_record is None:
            issues.append(
                AuditIssue(
                    code="table.merge_record_missing",
                    page=block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=block.uid,
                    message=(
                        f"multi_page=True but merge_record is None "
                        f"(block_uid={block.uid!r}); merge provenance was not recorded"
                    ),
                )
            )
        if block.merge_record is not None and not block.multi_page:
            issues.append(
                AuditIssue(
                    code="table.merge_record_without_multi_page",
                    page=block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=block.uid,
                    message=(
                        f"merge_record is set but multi_page=False "
                        f"(block_uid={block.uid!r}); inconsistent merge metadata"
                    ),
                )
            )
        if block.multi_page and block.continuation:
            issues.append(
                AuditIssue(
                    code="table.merge_multi_page_and_continuation",
                    page=block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=block.uid,
                    message=(
                        f"table has both multi_page=True and continuation=True "
                        f"(block_uid={block.uid!r}); a merged table must not also be a continuation"
                    ),
                )
            )
        if block.merge_record is not None:
            _check_merge_record_alignment(block, ref.block_idx, ref.chapter.uid, issues)

        # --- HTML structure checks ---
        if block.multi_page:
            _check_multi_page(block, ref.block_idx, ref.chapter.uid, issues)
        elif block.continuation:
            # continuation=True but multi_page=False: assembler found no predecessor
            issues.append(
                AuditIssue(
                    code="table.merge_orphan_continuation",
                    page=block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=block.uid,
                    message=(
                        f"table has continuation=True but multi_page=False "
                        f"(block_uid={block.uid!r}); assembler found no predecessor table"
                    ),
                )
            )

    return AuditBundle(issues=tuple(issues))


def _check_merge_record_alignment(
    block: Table,
    block_idx: int,
    chapter_uid: str | None,
    issues: list[AuditIssue],
) -> None:
    """Check that merge_record arrays are aligned and represent a valid merge."""
    record = block.merge_record
    assert record is not None

    lengths = {
        "segment_html": len(record.segment_html),
        "segment_pages": len(record.segment_pages),
        "segment_order": len(record.segment_order),
        "column_widths": len(record.column_widths),
    }
    if len(set(lengths.values())) > 1:
        mismatches = ", ".join(f"{k}={v}" for k, v in lengths.items())
        issues.append(
            AuditIssue(
                code="table.merge_record_arrays_misaligned",
                page=block.provenance.page,
                block_index=block_idx,
                chapter_uid=chapter_uid,
                block_uid=block.uid,
                message=(
                    f"merge_record arrays have inconsistent lengths: {mismatches} "
                    f"(block_uid={block.uid!r})"
                ),
            )
        )
        return  # skip length check when arrays are already misaligned

    segment_count = lengths["segment_html"]
    if segment_count < 2:
        issues.append(
            AuditIssue(
                code="table.merge_record_too_few_segments",
                page=block.provenance.page,
                block_index=block_idx,
                chapter_uid=chapter_uid,
                block_uid=block.uid,
                message=(
                    f"merge_record has {segment_count} segment(s); "
                    f"a valid merge requires at least 2 (block_uid={block.uid!r})"
                ),
            )
        )


def _check_multi_page(
    block: Table,
    block_idx: int,
    chapter_uid: str | None,
    issues: list[AuditIssue],
) -> None:
    html = block.html
    bodies = TBODY_RE.findall(html)

    # --- table.merge_record_incomplete ---
    # multi_page but no usable tbody content (assembler produced an empty merge)
    if not bodies:
        issues.append(
            AuditIssue(
                code="table.merge_record_incomplete",
                page=block.provenance.page,
                block_index=block_idx,
                chapter_uid=chapter_uid,
                block_uid=block.uid,
                message=(
                    f"multi_page table has no <tbody> sections "
                    f"(block_uid={block.uid!r}); merge may be incomplete"
                ),
            )
        )
        return  # no point running further checks on empty content

    # --- table.merge_header_reintroduced ---
    # More than one <thead> surviving in merged HTML means the assembler's
    # header-strip pass on the continuation segment missed a nested/repeated header.
    thead_matches = THEAD_RE.findall(html)
    if len(thead_matches) > 1:
        snippet = thead_matches[1][:80]  # show the offending duplicate
        issues.append(
            AuditIssue(
                code="table.merge_header_reintroduced",
                page=block.provenance.page,
                block_index=block_idx,
                chapter_uid=chapter_uid,
                block_uid=block.uid,
                message=(
                    f"multi_page table contains {len(thead_matches)} <thead> sections; "
                    f"duplicate snippet: {snippet!r}"
                ),
            )
        )

    # --- table.merge_width_drift ---
    # Compare modal column width of each tbody segment against the reference
    # (first segment).  Flag when relative deviation exceeds threshold.
    if len(bodies) >= 2:
        widths = [_modal_width(b) for b in bodies]
        reference = widths[0]
        if reference is not None and reference > 0:
            for seg_idx, w in enumerate(widths):
                if w is None:
                    continue
                drift = abs(w - reference) / reference
                if drift > _WIDTH_DRIFT_THRESHOLD:
                    issues.append(
                        AuditIssue(
                            code="table.merge_width_drift",
                            page=block.provenance.page,
                            block_index=block_idx,
                            chapter_uid=chapter_uid,
                            block_uid=block.uid,
                            message=(
                                f"tbody segment {seg_idx} has modal column width {w}, "
                                f"reference (segment 0) is {reference}; "
                                f"drift {drift:.0%} > threshold {_WIDTH_DRIFT_THRESHOLD:.0%} "
                                f"(block_uid={block.uid!r})"
                            ),
                        )
                    )
                    break  # one issue per table is enough


__all__ = ["detect_table_merge_issues"]
