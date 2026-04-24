"""Hard-rule footnote structure detectors."""

from __future__ import annotations

import unicodedata
from collections import defaultdict

from epubforge.audit.models import AuditBundle, AuditIssue, PageFootnoteDensity
from epubforge.fields import iter_block_text_fields
from epubforge.markers import count_raw_callout
from epubforge.query import find_footnotes, find_markers, iter_blocks
from epubforge.ir.semantic import Book, Footnote


def _is_probable_callout_symbol(callout: str) -> bool:
    if len(callout) != 1:
        return False
    category = unicodedata.category(callout)
    return category.startswith(("N", "S")) or callout in {"*", "†", "‡", "§", "¶"}


def detect_footnote_issues(book: Book) -> AuditBundle:
    issues: list[AuditIssue] = []
    block_uid_to_chapter_uid = {ref.block.uid: ref.chapter.uid for ref in iter_blocks(book) if ref.block.uid is not None}
    marker_map: dict[tuple[int, str], list] = defaultdict(list)
    for marker in find_markers(book):
        marker_map[(marker.page, marker.callout)].append(marker)

    footnote_map: dict[tuple[int, str], list] = defaultdict(list)
    footnotes = find_footnotes(book)
    for ref in footnotes:
        block = ref.block
        assert isinstance(block, Footnote)
        footnote_map[(block.provenance.page, block.callout)].append(ref)

    for (page, callout), markers in sorted(marker_map.items()):
        if len(markers) > 1:
            first = markers[0]
            block_uid = first.block.uid
            issues.append(
                AuditIssue(
                    code="footnote.duplicate_callout",
                    page=page,
                    block_index=first.block_idx,
                    chapter_uid=block_uid_to_chapter_uid.get(block_uid) if block_uid is not None else None,
                    block_uid=block_uid,
                    message=f"marker {callout!r} appears {len(markers)} times on page {page}",
                    note_kind="unknown_callout",
                )
            )
        if (page, callout) not in footnote_map:
            first = markers[0]
            block_uid = first.block.uid
            issues.append(
                AuditIssue(
                    code="footnote.marker_with_no_host",
                    page=page,
                    block_index=first.block_idx,
                    chapter_uid=block_uid_to_chapter_uid.get(block_uid) if block_uid is not None else None,
                    block_uid=block_uid,
                    message=f"marker {callout!r} has no matching footnote body on page {page}",
                    note_kind="orphan_footnote",
                )
            )

    for (page, callout), refs in sorted(footnote_map.items()):
        if len(refs) > 1:
            first = refs[0]
            issues.append(
                AuditIssue(
                    code="footnote.duplicate_body_callout",
                    page=page,
                    block_index=first.block_idx,
                    chapter_uid=first.chapter.uid,
                    block_uid=first.block.uid,
                    message=f"{len(refs)} footnote bodies share callout {callout!r} on page {page}",
                    note_kind="orphan_footnote",
                )
            )

    probable_callouts: list[str] = []
    for ref in footnotes:
        block = ref.block
        assert isinstance(block, Footnote)
        if _is_probable_callout_symbol(block.callout):
            probable_callouts.append(block.callout)
    probable_callouts = sorted(set(probable_callouts))
    for ref in iter_blocks(book):
        if ref.block.kind == "footnote":
            continue
        for field, value in iter_block_text_fields(ref.block):
            for callout in probable_callouts:
                raw_count = count_raw_callout(value, callout)
                if raw_count <= 0:
                    continue
                issues.append(
                    AuditIssue(
                        code="footnote.raw_callout_residue",
                        page=ref.block.provenance.page,
                        block_index=ref.block_idx,
                        chapter_uid=ref.chapter.uid,
                        block_uid=ref.block.uid,
                        message=f"field {field} contains raw callout {callout!r} x{raw_count}",
                        note_kind="unknown_callout",
                    )
                )

    for ref in footnotes:
        block = ref.block
        assert isinstance(block, Footnote)
        # Empty callout string means no marker symbol was extracted
        if not block.callout:
            issues.append(
                AuditIssue(
                    code="footnote.empty_callout_body",
                    page=block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=block.uid,
                    message="footnote body has an empty callout string — marker cannot be resolved",
                    note_kind="unknown_callout",
                )
            )
        key = (block.provenance.page, block.callout)
        has_marker = key in marker_map
        if block.paired and block.orphan:
            issues.append(
                AuditIssue(
                    code="footnote.paired_orphan_conflict",
                    page=block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=block.uid,
                    message="footnote cannot be paired and orphan at the same time",
                    note_kind="orphan_footnote",
                )
            )
        if block.paired and not has_marker:
            issues.append(
                AuditIssue(
                    code="footnote.paired_without_marker",
                    page=block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=block.uid,
                    message=f"paired footnote {block.callout!r} has no marker host on page {block.provenance.page}",
                    note_kind="orphan_footnote",
                )
            )
        if block.orphan and has_marker:
            issues.append(
                AuditIssue(
                    code="footnote.orphan_with_marker",
                    page=block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=block.uid,
                    message=f"orphan footnote {block.callout!r} still has a marker host on page {block.provenance.page}",
                    note_kind="orphan_footnote",
                )
            )

    page_to_chapter: dict[int, str | None] = {}
    pages: set[int] = set()
    for ref in iter_blocks(book):
        page = ref.block.provenance.page
        pages.add(page)
        page_to_chapter.setdefault(page, ref.chapter.uid)
    counts_by_page: dict[int, int] = {page: 0 for page in pages}
    for ref in footnotes:
        page = ref.block.provenance.page
        counts_by_page[page] = counts_by_page.get(page, 0) + 1
    densities = tuple(
        PageFootnoteDensity(page=page, chapter_uid=page_to_chapter.get(page), count=counts_by_page.get(page, 0))
        for page in range(min(pages, default=1), max(pages, default=0) + 1)
    )
    return AuditBundle(issues=tuple(issues), footnote_density=densities)


__all__ = ["detect_footnote_issues"]
