"""Audit detector for Docling candidate roles (mechanical parse draft evidence)."""

from __future__ import annotations

from epubforge.audit.models import AuditBundle, AuditIssue
from epubforge.query import iter_blocks
from epubforge.ir.semantic import Book, Paragraph


def detect_candidate_issues(book: Book) -> AuditBundle:
    """Find all Paragraph blocks with ``docling_*_candidate`` roles and emit info hints.

    These are not errors — they are actionable signals that candidate blocks need
    scanner/fixer review before the book can be considered semantically complete.
    """
    issues: list[AuditIssue] = []
    for ref in iter_blocks(book):
        block = ref.block
        if not isinstance(block, Paragraph):
            continue
        if not block.role.startswith("docling_") or not block.role.endswith(
            "_candidate"
        ):
            continue
        issues.append(
            AuditIssue(
                code="candidate.needs_review",
                page=block.provenance.page,
                block_index=ref.block_idx,
                chapter_uid=ref.chapter.uid,
                block_uid=block.uid,
                message=(
                    f"block has candidate role {block.role!r} — "
                    f"requires scanner/fixer review before semantic promotion"
                ),
                note_kind="other",
            )
        )
    return AuditBundle(issues=tuple(issues))


__all__ = ["detect_candidate_issues"]
