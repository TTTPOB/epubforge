"""Tests for skip-VLM audit detectors and doctor hints."""

from __future__ import annotations

from collections.abc import Callable


from epubforge.audit.candidates import detect_candidate_issues
from epubforge.audit.footnotes import detect_footnote_issues
from epubforge.audit.invariants import detect_invariant_issues, _is_single_draft_chapter
from epubforge.audit.table_merge import detect_table_merge_issues
from epubforge.editor.doctor import build_doctor_report
from epubforge.editor.memory import EditMemory
from epubforge.ir.semantic import (
    Book,
    Chapter,
    ExtractionMetadata,
    Footnote,
    Paragraph,
    Provenance,
    Table,
    TableMergeRecord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _para(
    prov: Callable[..., Provenance],
    uid: str,
    text: str,
    page: int,
    *,
    role: str = "body",
) -> Paragraph:
    return Paragraph(uid=uid, text=text, role=role, provenance=prov(page))


def _footnote(
    prov: Callable[..., Provenance],
    uid: str,
    page: int,
    callout: str,
    *,
    text: str = "footnote body",
) -> Footnote:
    return Footnote(uid=uid, callout=callout, text=text, provenance=prov(page))


def _table(
    prov: Callable[..., Provenance],
    uid: str,
    html: str,
    page: int,
    *,
    multi_page: bool = False,
    continuation: bool = False,
    merge_record: TableMergeRecord | None = None,
) -> Table:
    return Table(
        uid=uid,
        html=html,
        multi_page=multi_page,
        continuation=continuation,
        merge_record=merge_record,
        provenance=prov(page),
    )


def _chapter(uid: str, title: str, blocks: list) -> Chapter:
    return Chapter(uid=uid, title=title, blocks=blocks)


def _book(
    chapters: list[Chapter],
    *,
    stage3_mode: str = "unknown",
    complex_pages: list[int] | None = None,
) -> Book:
    extraction = ExtractionMetadata(
        stage3_mode=stage3_mode,  # type: ignore[arg-type]
        complex_pages=complex_pages or [],
    )
    return Book(title="Test Book", chapters=chapters, extraction=extraction)


def _memory(
    chapter_uids: list[str] | None = None, *, scanned: set[str] | None = None
) -> EditMemory:
    uids = chapter_uids or ["ch-1"]
    memory = EditMemory.create(
        book_id="book-skip-vlm",
        updated_at="2026-04-24T08:00:00Z",
        updated_by="tester",
        chapter_uids=uids,
    )
    scanned = scanned or set()
    chapter_status = {
        uid: status.model_copy(update={"read_passes": 1 if uid in scanned else 0})
        for uid, status in memory.chapter_status.items()
    }
    return memory.model_copy(update={"chapter_status": chapter_status})


# ---------------------------------------------------------------------------
# 1. detect_candidate_issues — finds candidate roles
# ---------------------------------------------------------------------------


def test_detect_candidate_issues_finds_candidate_roles(prov) -> None:
    blocks = [
        _para(
            prov, "p-cand1", "Section title draft", 1, role="docling_heading_candidate"
        ),
        _para(prov, "p-cand2", "Footnote draft", 1, role="docling_footnote_candidate"),
        _para(prov, "p-cand3", "Caption draft", 2, role="docling_caption_candidate"),
    ]
    book = _book([_chapter("ch-1", "Draft extraction", blocks)])
    bundle = detect_candidate_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert codes.count("candidate.needs_review") == 3
    block_uids = [issue.block_uid for issue in bundle.issues]
    assert "p-cand1" in block_uids
    assert "p-cand2" in block_uids
    assert "p-cand3" in block_uids


def test_detect_candidate_issues_all_candidate_role_variants(prov) -> None:
    candidate_roles = [
        "docling_title_candidate",
        "docling_heading_candidate",
        "docling_footnote_candidate",
        "docling_list_item_candidate",
        "docling_caption_candidate",
        "docling_handwritten_candidate",
        "docling_field_candidate",
        "docling_checkbox_candidate",
        "docling_unknown_candidate",
    ]
    blocks = [
        _para(prov, f"uid-{i}", "text", 1, role=role)
        for i, role in enumerate(candidate_roles)
    ]
    book = _book([_chapter("ch-1", "Chapter", blocks)])
    bundle = detect_candidate_issues(book)

    assert len(bundle.issues) == len(candidate_roles)
    assert all(issue.code == "candidate.needs_review" for issue in bundle.issues)


# ---------------------------------------------------------------------------
# 2. detect_candidate_issues — ignores non-candidate roles (body, code)
# ---------------------------------------------------------------------------


def test_detect_candidate_issues_ignores_non_candidate_roles(prov) -> None:
    blocks = [
        _para(prov, "p-body", "body text", 1, role="body"),
        _para(prov, "p-code", "x = 1", 1, role="code"),
        _para(prov, "p-epigraph", "epigraph text", 1, role="epigraph"),
    ]
    book = _book([_chapter("ch-1", "Chapter", blocks)])
    bundle = detect_candidate_issues(book)

    assert len(bundle.issues) == 0


def test_detect_candidate_issues_mixed_book_only_flags_candidates(prov) -> None:
    blocks = [
        _para(prov, "p-body", "body text", 1, role="body"),
        _para(prov, "p-cand", "heading draft", 1, role="docling_heading_candidate"),
        _para(prov, "p-code", "code block", 2, role="code"),
    ]
    book = _book([_chapter("ch-1", "Chapter", blocks)])
    bundle = detect_candidate_issues(book)

    assert len(bundle.issues) == 1
    assert bundle.issues[0].code == "candidate.needs_review"
    assert bundle.issues[0].block_uid == "p-cand"


# ---------------------------------------------------------------------------
# 3. Empty callout footnote reported as footnote.empty_callout_body
# ---------------------------------------------------------------------------


def test_footnote_empty_callout_reported(prov) -> None:
    blocks = [
        _footnote(prov, "fn-empty", 5, callout="", text="orphan note without callout"),
    ]
    book = _book([_chapter("ch-1", "Chapter", blocks)])
    bundle = detect_footnote_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "footnote.empty_callout_body" in codes
    matching = [i for i in bundle.issues if i.code == "footnote.empty_callout_body"]
    assert matching[0].block_uid == "fn-empty"
    assert matching[0].page == 5


def test_footnote_non_empty_callout_not_reported_as_empty(prov) -> None:
    blocks = [
        _footnote(prov, "fn-ok", 5, callout="①"),
    ]
    book = _book([_chapter("ch-1", "Chapter", blocks)])
    bundle = detect_footnote_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "footnote.empty_callout_body" not in codes


# ---------------------------------------------------------------------------
# 4. Skip-VLM complex page gets needs_scan hint via doctor
# ---------------------------------------------------------------------------


def test_docling_complex_page_gets_needs_scan_hint(prov) -> None:
    blocks = [_para(prov, "p-1", "text on complex page", 3, role="body")]
    book = _book(
        [_chapter("ch-1", "Chapter One", blocks)],
        stage3_mode="docling",
        complex_pages=[3],
    )
    memory = _memory(["ch-1"], scanned={"ch-1"})
    report = build_doctor_report(memory=memory, book=book, previous_memory=memory)

    docling_scan_hints = [
        h
        for h in report.hints
        if h.kind == "needs_scan"
        and h.chapter_uid == "ch-1"
        and h.suggested_subagent_type == "scanner"
    ]
    assert len(docling_scan_hints) >= 1


def test_non_docling_complex_page_no_candidate_hints(prov) -> None:
    # stage3_mode="unknown" should produce no candidate_review or table_merge_pending hints
    blocks = [_para(prov, "p-1", "body", 1, role="body")]
    book = _book([_chapter("ch-1", "Chapter", blocks)], stage3_mode="unknown")
    memory = _memory(["ch-1"], scanned={"ch-1"})
    report = build_doctor_report(memory=memory, book=book, previous_memory=memory)

    assert not any(h.kind == "candidate_review" for h in report.hints)
    assert not any(h.kind == "table_merge_pending" for h in report.hints)


# ---------------------------------------------------------------------------
# 5. Table merge detector checks only explicit metadata, not structural inference
# ---------------------------------------------------------------------------


def test_table_merge_detector_multi_page_true_merge_record_none(prov) -> None:
    """multi_page=True but merge_record=None should be flagged."""
    html = "<table><tbody><tr><td>data</td></tr></tbody></table>"
    tbl = _table(prov, "tbl-1", html, 10, multi_page=True, merge_record=None)
    book = _book([_chapter("ch-1", "Chapter", [tbl])])
    bundle = detect_table_merge_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "table.merge_record_missing" in codes


def test_table_merge_detector_merge_record_set_multi_page_false(prov) -> None:
    """merge_record set but multi_page=False should be flagged."""
    html = "<table><tbody><tr><td>data</td></tr></tbody></table>"
    record = TableMergeRecord(
        segment_html=["<tr><td>a</td></tr>", "<tr><td>b</td></tr>"],
        segment_pages=[1, 2],
        segment_order=[0, 1],
        column_widths=[1, 1],
    )
    tbl = _table(prov, "tbl-2", html, 5, multi_page=False, merge_record=record)
    book = _book([_chapter("ch-1", "Chapter", [tbl])])
    bundle = detect_table_merge_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "table.merge_record_without_multi_page" in codes


def test_table_merge_detector_multi_page_and_continuation(prov) -> None:
    """multi_page=True and continuation=True together should be flagged."""
    html = "<table><tbody><tr><td>data</td></tr></tbody></table>"
    tbl = _table(prov, "tbl-3", html, 7, multi_page=True, continuation=True)
    book = _book([_chapter("ch-1", "Chapter", [tbl])])
    bundle = detect_table_merge_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "table.merge_multi_page_and_continuation" in codes


def test_table_merge_detector_merge_record_arrays_misaligned(prov) -> None:
    """merge_record with misaligned array lengths should be flagged."""
    html = "<table><tbody><tr><td>r1</td></tr></tbody><tbody><tr><td>r2</td></tr></tbody></table>"
    record = TableMergeRecord(
        segment_html=["<tr><td>r1</td></tr>", "<tr><td>r2</td></tr>"],
        segment_pages=[1, 2],
        segment_order=[0],  # length 1 — misaligned
        column_widths=[1, 1],
    )
    tbl = _table(prov, "tbl-4", html, 12, multi_page=True, merge_record=record)
    book = _book([_chapter("ch-1", "Chapter", [tbl])])
    bundle = detect_table_merge_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "table.merge_record_arrays_misaligned" in codes


def test_table_merge_detector_merge_record_too_few_segments(prov) -> None:
    """merge_record with only 1 segment should be flagged."""
    html = "<table><tbody><tr><td>data</td></tr></tbody></table>"
    record = TableMergeRecord(
        segment_html=["<tr><td>data</td></tr>"],
        segment_pages=[5],
        segment_order=[0],
        column_widths=[1],
    )
    tbl = _table(prov, "tbl-5", html, 5, multi_page=True, merge_record=record)
    book = _book([_chapter("ch-1", "Chapter", [tbl])])
    bundle = detect_table_merge_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "table.merge_record_too_few_segments" in codes


def test_table_merge_detector_no_inference_on_plain_table(prov) -> None:
    """A plain non-continuation table with no metadata flags must not be touched."""
    html = (
        "<table><tbody>"
        "<tr><td>row 1 col 1</td></tr>"
        "<tr><td>row 2 col 1</td></tr>"
        "</tbody></table>"
    )
    tbl = _table(prov, "tbl-plain", html, 3, multi_page=False, continuation=False)
    book = _book([_chapter("ch-1", "Chapter", [tbl])])
    bundle = detect_table_merge_issues(book)
    merge_codes = [
        issue.code
        for issue in bundle.issues
        if issue.code.startswith("table.merge_record")
        or issue.code == "table.merge_multi_page_and_continuation"
    ]

    assert merge_codes == [], f"unexpected issues on plain table: {merge_codes}"


# ---------------------------------------------------------------------------
# 6. Single "Draft extraction" chapter is valid (not an error)
# ---------------------------------------------------------------------------


def test_single_draft_extraction_chapter_not_flagged(prov) -> None:
    blocks = [_para(prov, "p-1", "draft body", 1)]
    book = _book([_chapter("ch-draft", "Draft extraction", blocks)])
    bundle = detect_invariant_issues(book)
    codes = [issue.code for issue in bundle.issues]

    # uid checks still apply but there must be no "not split" structural error
    assert "invariant.missing_chapter_uid" not in codes
    # Single chapter with UID is fully valid
    assert "invariant.duplicate_chapter_uid" not in codes


def test_is_single_draft_chapter_helper(prov) -> None:
    blocks = [_para(prov, "p-1", "draft body", 1)]
    book_single = _book([_chapter("ch-1", "Draft extraction", blocks)])
    book_multi = _book(
        [
            _chapter("ch-1", "Draft extraction", blocks),
            _chapter("ch-2", "Chapter Two", blocks),
        ]
    )
    book_other_title = _book([_chapter("ch-1", "Chapter One", blocks)])

    assert _is_single_draft_chapter(book_single) is True
    assert _is_single_draft_chapter(book_multi) is False
    assert _is_single_draft_chapter(book_other_title) is False


# ---------------------------------------------------------------------------
# 7. VLM table with continuation=True but no merge gets hint via doctor
# ---------------------------------------------------------------------------


def test_docling_continuation_table_no_merge_gets_hint(prov) -> None:
    html = "<table><tbody><tr><td>continuation data</td></tr></tbody></table>"
    tbl = _table(prov, "tbl-cont", html, 8, continuation=True, multi_page=False)
    tbl = tbl.model_copy(update={"uid": "tbl-cont"})
    book = _book(
        [_chapter("ch-1", "Chapter One", [tbl])],
        stage3_mode="docling",
    )
    memory = _memory(["ch-1"], scanned={"ch-1"})
    report = build_doctor_report(memory=memory, book=book, previous_memory=memory)

    merge_hints = [h for h in report.hints if h.kind == "table_merge_pending"]
    assert len(merge_hints) >= 1
    assert merge_hints[0].block_uid == "tbl-cont"
    assert merge_hints[0].chapter_uid == "ch-1"


def test_unknown_mode_continuation_table_no_merge_hint(prov) -> None:
    """With stage3_mode='unknown', no table_merge_pending hints should be generated."""
    html = "<table><tbody><tr><td>data</td></tr></tbody></table>"
    tbl = _table(prov, "tbl-v", html, 4, continuation=True, multi_page=False)
    book = _book([_chapter("ch-1", "Chapter", [tbl])], stage3_mode="unknown")
    memory = _memory(["ch-1"], scanned={"ch-1"})
    report = build_doctor_report(memory=memory, book=book, previous_memory=memory)

    assert not any(h.kind == "table_merge_pending" for h in report.hints)


def test_docling_candidate_blocks_get_candidate_review_hints(prov) -> None:
    blocks = [
        _para(prov, "p-cand", "heading draft", 1, role="docling_heading_candidate"),
        _para(prov, "p-body", "body text", 1, role="body"),
    ]
    book = _book(
        [_chapter("ch-1", "Draft extraction", blocks)],
        stage3_mode="docling",
    )
    memory = _memory(["ch-1"], scanned={"ch-1"})
    report = build_doctor_report(memory=memory, book=book, previous_memory=memory)

    cand_hints = [h for h in report.hints if h.kind == "candidate_review"]
    assert len(cand_hints) == 1
    assert cand_hints[0].block_uid == "p-cand"
    assert cand_hints[0].chapter_uid == "ch-1"
    assert cand_hints[0].severity == "info"


def test_candidate_review_hints_are_not_errors(prov) -> None:
    """candidate_review hints must have severity='info', not 'warn' or 'error'."""
    blocks = [_para(prov, "p-cand", "title draft", 1, role="docling_title_candidate")]
    book = _book([_chapter("ch-1", "Chapter", blocks)], stage3_mode="docling")
    memory = _memory(["ch-1"], scanned={"ch-1"})
    report = build_doctor_report(memory=memory, book=book, previous_memory=memory)

    cand_hints = [h for h in report.hints if h.kind == "candidate_review"]
    assert all(h.severity == "info" for h in cand_hints)
