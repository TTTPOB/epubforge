"""Hard-rule structural detectors."""

from __future__ import annotations

from epubforge.audit.models import AuditBundle, AuditIssue
from epubforge.ir.style_registry import ALLOWED_ROLES, StyleRegistry, seed_defaults
from epubforge.query import iter_blocks
from epubforge.ir.semantic import Book, Heading, Paragraph


def _known_style_classes() -> set[str]:
    registry = StyleRegistry()
    seed_defaults(registry)
    classes = {style.id for style in registry.styles}
    classes.update(style.css_class for style in registry.styles)
    classes.update({"centered", "centered-section", "centered-bold"})
    return classes


KNOWN_STYLE_CLASSES = _known_style_classes()


def detect_structure_issues(book: Book) -> AuditBundle:
    issues: list[AuditIssue] = []
    for chapter_idx, chapter in enumerate(book.chapters):
        if not chapter.title.strip():
            issues.append(
                AuditIssue(
                    code="structure.blank_chapter_title",
                    page=chapter.blocks[0].provenance.page if chapter.blocks else chapter_idx + 1,
                    chapter_uid=chapter.uid,
                    message="chapter title must not be blank",
                )
            )
    for ref in iter_blocks(book):
        block = ref.block
        if isinstance(block, Paragraph) and block.role not in ALLOWED_ROLES:
            issues.append(
                AuditIssue(
                    code="structure.invalid_role",
                    page=block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=block.uid,
                    message=f"paragraph role {block.role!r} is not allowed",
                )
            )
        style_class = getattr(block, "style_class", None)
        if style_class is not None and style_class not in KNOWN_STYLE_CLASSES:
            issues.append(
                AuditIssue(
                    code="structure.unknown_style_class",
                    page=block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=block.uid,
                    message=f"style_class {style_class!r} is not in the known registry/default set",
                )
            )
        if isinstance(block, Heading):
            if not block.text.strip():
                issues.append(
                    AuditIssue(
                        code="structure.blank_heading_text",
                        page=block.provenance.page,
                        block_index=ref.block_idx,
                        chapter_uid=ref.chapter.uid,
                        block_uid=block.uid,
                        message="heading text must not be blank",
                    )
                )
            if block.id is not None and not block.id.strip():
                issues.append(
                    AuditIssue(
                        code="structure.blank_heading_id",
                        page=block.provenance.page,
                        block_index=ref.block_idx,
                        chapter_uid=ref.chapter.uid,
                        block_uid=block.uid,
                        message="heading id must be non-empty when present",
                    )
                )
    return AuditBundle(issues=tuple(issues))


__all__ = ["KNOWN_STYLE_CLASSES", "detect_structure_issues"]
