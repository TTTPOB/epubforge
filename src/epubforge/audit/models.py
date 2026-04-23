"""Shared audit detector models and aggregation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from epubforge.ir.semantic import AuditNote


AuditNoteKind = Literal["orphan_footnote", "suspect_attribution", "punctuation_anomaly", "unknown_callout", "other"]

DASH_CHAR_LABELS: dict[str, str] = {
    "\u002d": "HYPHEN-MINUS",
    "\u2010": "HYPHEN",
    "\u2011": "NON-BREAKING HYPHEN",
    "\u2012": "FIGURE DASH",
    "\u2013": "EN DASH",
    "\u2014": "EM DASH",
    "\u2015": "HORIZONTAL BAR",
    "\u2e3a": "TWO-EM DASH",
    "\u2e3b": "THREE-EM DASH",
    "\uff0d": "FULLWIDTH HYPHEN-MINUS",
}


def normalized_chapter_uid(chapter_uid: str | None, index: int) -> str:
    if chapter_uid is not None and chapter_uid.strip():
        return chapter_uid
    return f"chapter-{index}"


def _truncate_hint(text: str, limit: int = 200) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class AuditIssue:
    code: str
    page: int
    message: str
    note_kind: AuditNoteKind = "other"
    block_index: int | None = None
    chapter_uid: str | None = None
    block_uid: str | None = None

    def to_audit_note(self) -> AuditNote:
        parts = [self.code]
        if self.chapter_uid:
            parts.append(f"chapter={self.chapter_uid}")
        if self.block_uid:
            parts.append(f"block={self.block_uid}")
        parts.append(self.message)
        return AuditNote(
            page=self.page,
            block_index=self.block_index,
            kind=self.note_kind,
            hint=_truncate_hint(" ".join(parts)),
        )


@dataclass(frozen=True, slots=True)
class DashInventoryChapter:
    chapter_uid: str
    counts: dict[str, int]
    total: int
    dominant_char: str | None
    dominant_count: int


@dataclass(frozen=True, slots=True)
class PageFootnoteDensity:
    page: int
    chapter_uid: str | None
    count: int


@dataclass(frozen=True, slots=True)
class AuditBundle:
    issues: tuple[AuditIssue, ...] = ()
    dash_inventory: tuple[DashInventoryChapter, ...] = ()
    footnote_density: tuple[PageFootnoteDensity, ...] = ()

    def to_audit_notes(self) -> list[AuditNote]:
        return [issue.to_audit_note() for issue in self.issues]


def merge_bundles(*bundles: AuditBundle) -> AuditBundle:
    issues: list[AuditIssue] = []
    dash_inventory: list[DashInventoryChapter] = []
    footnote_density: list[PageFootnoteDensity] = []
    for bundle in bundles:
        issues.extend(bundle.issues)
        dash_inventory.extend(bundle.dash_inventory)
        footnote_density.extend(bundle.footnote_density)
    return AuditBundle(
        issues=tuple(issues),
        dash_inventory=tuple(dash_inventory),
        footnote_density=tuple(footnote_density),
    )


__all__ = [
    "AuditBundle",
    "AuditIssue",
    "DASH_CHAR_LABELS",
    "DashInventoryChapter",
    "PageFootnoteDensity",
    "merge_bundles",
    "normalized_chapter_uid",
]
