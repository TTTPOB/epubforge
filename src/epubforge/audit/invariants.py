"""General editor and book invariants."""

from __future__ import annotations

from collections import Counter

from epubforge.audit.models import AuditBundle, AuditIssue
from epubforge.query import iter_blocks
from epubforge.ir.semantic import Book


_DRAFT_EXTRACTION_TITLE = "Draft extraction"


def _is_single_draft_chapter(book: Book) -> bool:
    """Return True when the book contains a single chapter titled 'Draft extraction'.

    A skip-VLM evidence draft may legitimately have a single unsplit chapter.
    This is valid — the invariant detector must not treat it as an error.
    """
    return len(book.chapters) == 1 and book.chapters[0].title == _DRAFT_EXTRACTION_TITLE


def detect_invariant_issues(book: Book) -> AuditBundle:
    issues: list[AuditIssue] = []
    chapter_uids = [chapter.uid for chapter in book.chapters if chapter.uid]
    chapter_uid_counts = Counter(chapter_uids)
    for chapter_idx, chapter in enumerate(book.chapters):
        page = chapter.blocks[0].provenance.page if chapter.blocks else chapter_idx + 1
        if chapter.uid is None or not chapter.uid.strip():
            issues.append(
                AuditIssue(
                    code="invariant.missing_chapter_uid",
                    page=page,
                    chapter_uid=chapter.uid,
                    message="chapter uid must be present",
                )
            )
        elif chapter_uid_counts[chapter.uid] > 1:
            issues.append(
                AuditIssue(
                    code="invariant.duplicate_chapter_uid",
                    page=page,
                    chapter_uid=chapter.uid,
                    message=f"chapter uid {chapter.uid!r} appears {chapter_uid_counts[chapter.uid]} times",
                )
            )

    block_uid_counts = Counter(
        ref.block.uid for ref in iter_blocks(book) if ref.block.uid
    )
    for ref in iter_blocks(book):
        if ref.block.uid is None or not ref.block.uid.strip():
            issues.append(
                AuditIssue(
                    code="invariant.missing_block_uid",
                    page=ref.block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=ref.block.uid,
                    message="block uid must be present",
                )
            )
            continue
        if block_uid_counts[ref.block.uid] > 1:
            issues.append(
                AuditIssue(
                    code="invariant.duplicate_block_uid",
                    page=ref.block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=ref.block.uid,
                    message=f"block uid {ref.block.uid!r} appears {block_uid_counts[ref.block.uid]} times",
                )
            )
        if ref.block.provenance.page < 1:
            issues.append(
                AuditIssue(
                    code="invariant.invalid_page",
                    page=ref.block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=ref.block.uid,
                    message="provenance page must be >= 1",
                )
            )
    return AuditBundle(issues=tuple(issues))


__all__ = ["detect_invariant_issues", "_is_single_draft_chapter"]
