"""Hard-rule punctuation audit helpers."""

from __future__ import annotations

from collections import Counter

from epubforge.audit.models import AuditBundle, DASH_CHAR_LABELS, DashInventoryChapter, normalized_chapter_uid
from epubforge.fields import iter_block_text_fields
from epubforge.ir.semantic import Book


def detect_dash_inventory(book: Book) -> AuditBundle:
    inventories: list[DashInventoryChapter] = []
    for chapter_idx, chapter in enumerate(book.chapters):
        counts: Counter[str] = Counter()
        for block in chapter.blocks:
            for _, value in iter_block_text_fields(block):
                counts.update(ch for ch in value if ch in DASH_CHAR_LABELS)
        total = sum(counts.values())
        dominant_char = None
        dominant_count = 0
        if counts:
            dominant_char, dominant_count = min(
                counts.items(),
                key=lambda item: (-item[1], ord(item[0])),
            )
        inventories.append(
            DashInventoryChapter(
                chapter_uid=normalized_chapter_uid(chapter.uid, chapter_idx),
                counts=dict(sorted(counts.items(), key=lambda item: ord(item[0]))),
                total=total,
                dominant_char=dominant_char,
                dominant_count=dominant_count,
            )
        )
    return AuditBundle(dash_inventory=tuple(inventories))


__all__ = ["detect_dash_inventory"]
