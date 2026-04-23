"""Deterministic audit detectors for the agentic editor."""

from epubforge.audit.footnotes import detect_footnote_issues
from epubforge.audit.invariants import detect_invariant_issues
from epubforge.audit.models import (
    AuditBundle,
    AuditIssue,
    DASH_CHAR_LABELS,
    DashInventoryChapter,
    PageFootnoteDensity,
    merge_bundles,
)
from epubforge.audit.punctuation import detect_dash_inventory
from epubforge.audit.structure import KNOWN_STYLE_CLASSES, detect_structure_issues
from epubforge.audit.tables import detect_table_issues
from epubforge.ir.semantic import Book


def run_all_detectors(book: Book) -> AuditBundle:
    return merge_bundles(
        detect_dash_inventory(book),
        detect_table_issues(book),
        detect_footnote_issues(book),
        detect_structure_issues(book),
        detect_invariant_issues(book),
    )


__all__ = [
    "AuditBundle",
    "AuditIssue",
    "DASH_CHAR_LABELS",
    "DashInventoryChapter",
    "KNOWN_STYLE_CLASSES",
    "PageFootnoteDensity",
    "detect_dash_inventory",
    "detect_footnote_issues",
    "detect_invariant_issues",
    "detect_structure_issues",
    "detect_table_issues",
    "run_all_detectors",
]
