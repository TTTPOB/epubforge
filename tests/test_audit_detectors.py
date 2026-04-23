from __future__ import annotations

from epubforge.audit import (
    detect_footnote_issues,
    detect_invariant_issues,
    detect_structure_issues,
    detect_table_issues,
)
from epubforge.editor.doctor import build_doctor_report
from epubforge.editor.memory import EditMemory, OpenQuestion
from epubforge.ir.semantic import Block, Book, Chapter, Footnote, Heading, Paragraph, Provenance, Table
from epubforge.markers import make_fn_marker


def _prov(page: int) -> Provenance:
    return Provenance(page=page, source="passthrough")


def _para(
    uid: str | None,
    text: str,
    page: int,
    *,
    role: str = "body",
    style_class: str | None = None,
) -> Paragraph:
    return Paragraph(uid=uid, text=text, role=role, style_class=style_class, provenance=_prov(page))


def _heading(
    uid: str | None,
    text: str,
    page: int,
    *,
    heading_id: str | None = None,
    style_class: str | None = None,
) -> Heading:
    return Heading(uid=uid, text=text, id=heading_id, style_class=style_class, provenance=_prov(page))


def _table(uid: str | None, html: str, page: int, *, table_title: str = "") -> Table:
    return Table(uid=uid, html=html, table_title=table_title, provenance=_prov(page))


def _footnote(
    uid: str | None,
    page: int,
    callout: str,
    *,
    paired: bool = False,
    orphan: bool = False,
) -> Footnote:
    return Footnote(uid=uid, callout=callout, text=f"note {callout}", paired=paired, orphan=orphan, provenance=_prov(page))


def _chapter(uid: str | None, title: str, blocks: list) -> Chapter:
    return Chapter(uid=uid, title=title, blocks=blocks)


def _book(chapters: list[Chapter]) -> Book:
    return Book(title="Audit Fixture", chapters=chapters)


def _memory(*, scanned: set[str] | None = None, open_question: bool = False) -> EditMemory:
    memory = EditMemory.create(
        book_id="book-1",
        updated_at="2026-04-23T08:00:00Z",
        updated_by="tester",
        chapter_uids=["ch-1", "ch-2"],
    )
    scanned = scanned or set()
    chapter_status = {
        uid: status.model_copy(update={"read_passes": 1 if uid in scanned else 0})
        for uid, status in memory.chapter_status.items()
    }
    updates: dict[str, object] = {"chapter_status": chapter_status}
    if open_question:
        updates["open_questions"] = [
            OpenQuestion(
                q_id="5300f0f5-230f-4191-abbd-b8b9fc7ee7a0",
                question="Need a reviewer decision.",
                asked_by="scanner-1",
            )
        ]
    return memory.model_copy(update=updates)


def test_table_detectors_flag_double_tbody_split_row_and_column_mismatch() -> None:
    html = (
        "<table><tbody>"
        "<tr><td>A</td><td>B</td><td>C</td></tr>"
        "</tbody><tbody>"
        "<tr><td></td><td>续行</td><td></td></tr>"
        "<tr><td>1</td><td>2</td></tr>"
        "</tbody></table>"
    )
    book = _book([_chapter("ch-1", "Tables", [_table("tbl-1", html, 12, table_title="表1")])])

    issues = detect_table_issues(book).to_audit_notes()
    codes = {issue.hint.split()[0] for issue in issues}

    assert "table.double_tbody" in codes
    assert "table.split_row_suspected" in codes
    assert "table.column_count_mismatch" in codes


def test_footnote_detectors_flag_duplicate_raw_residue_dangling_marker_and_conflicts() -> None:
    marker_1 = make_fn_marker(7, "①")
    marker_2 = make_fn_marker(7, "②")
    book = _book(
        [
            _chapter(
                "ch-1",
                "Footnotes",
                [
                    _para("p-1", f"alpha {marker_1}", 7),
                    _para("p-2", f"beta {marker_1}", 7),
                    _para("p-3", "gamma ① delta", 7),
                    _para("p-4", f"orphan marker {marker_2}", 7),
                    _footnote("fn-1", 7, "①", paired=True, orphan=True),
                    _footnote("fn-2", 7, "③", paired=True),
                ],
            )
        ]
    )

    issues = detect_footnote_issues(book).to_audit_notes()
    codes = {issue.hint.split()[0] for issue in issues}

    assert "footnote.duplicate_callout" in codes
    assert "footnote.raw_callout_residue" in codes
    assert "footnote.marker_with_no_host" in codes
    assert "footnote.paired_orphan_conflict" in codes
    assert "footnote.paired_without_marker" in codes


def test_structure_and_invariant_detectors_flag_invalid_role_unknown_style_and_uid_problems() -> None:
    book = _book(
        [
            _chapter(
                "dup-ch",
                "Chapter One",
                [
                    _para(None, "body", 1, role="mystery_role"),
                    _heading("dup-block", "Heading", 1, style_class="unknown-style"),
                ],
            ),
            _chapter(
                "dup-ch",
                "Chapter Two",
                [
                    _para("dup-block", "body", 2),
                ],
            ),
        ]
    )

    structure_codes = {issue.hint.split()[0] for issue in detect_structure_issues(book).to_audit_notes()}
    invariant_codes = {issue.hint.split()[0] for issue in detect_invariant_issues(book).to_audit_notes()}

    assert "structure.invalid_role" in structure_codes
    assert "structure.unknown_style_class" in structure_codes
    assert "invariant.duplicate_chapter_uid" in invariant_codes
    assert "invariant.missing_block_uid" in invariant_codes
    assert "invariant.duplicate_block_uid" in invariant_codes


def test_doctor_auto_runs_detectors_and_combines_issues_with_core_hints() -> None:
    chapter_one_blocks: list[Block] = [_para("p-1", "1-2 3-4 5-6", 1)]
    chapter_one_blocks.extend(_para(f"z-{page}", f"page {page}", page) for page in range(2, 31))

    chapter_two_blocks: list[Block] = [_para("p-2", "1—2 3—4 5—6", 31, role="invalid_role")]
    chapter_two_blocks.extend(_para(f"p-{page}", f"page {page}", page) for page in range(32, 60))
    chapter_two_blocks.extend(_footnote(f"fn-{index}", 60, f"*{index}") for index in range(10))

    book = _book(
        [
            _chapter("ch-1", "Chapter One", chapter_one_blocks),
            _chapter("ch-2", "Chapter Two", chapter_two_blocks),
        ]
    )
    memory = _memory(scanned={"ch-1"}, open_question=True)

    report = build_doctor_report(
        memory=memory,
        book=book,
        previous_memory=memory,
        previous_report=None,
        new_applied_op_count=0,
    )

    issue_codes = {issue.hint.split()[0] for issue in report.issues}
    hint_kinds = {hint.kind for hint in report.hints}

    assert "structure.invalid_role" in issue_codes
    assert report.readiness.chapters_unscanned == ["ch-2"]
    assert {"needs_scan", "style_inconsistency", "unusual_density", "open_question"} <= hint_kinds


def test_style_and_density_hints_are_deterministic_for_same_book() -> None:
    chapter_one_blocks: list[Block] = [_para("p-1", "10-11 12-13 14-15", 1)]
    chapter_one_blocks.extend(_para(f"a-{page}", f"page {page}", page) for page in range(2, 31))

    chapter_two_blocks: list[Block] = [_para("p-2", "10—11 12—13 14—15", 31)]
    chapter_two_blocks.extend(_para(f"b-{page}", f"page {page}", page) for page in range(32, 60))
    chapter_two_blocks.extend(_footnote(f"fn-{index}", 60, str(index), paired=False) for index in range(10))

    book = _book(
        [
            _chapter("ch-1", "Chapter One", chapter_one_blocks),
            _chapter("ch-2", "Chapter Two", chapter_two_blocks),
        ]
    )
    memory = _memory(scanned={"ch-1", "ch-2"})

    report_a = build_doctor_report(memory=memory, book=book, previous_memory=memory)
    report_b = build_doctor_report(memory=memory, book=book, previous_memory=memory)

    filtered_a = [
        (hint.kind, hint.scope, hint.chapter_uid, hint.message)
        for hint in report_a.hints
        if hint.kind in {"style_inconsistency", "unusual_density"}
    ]
    filtered_b = [
        (hint.kind, hint.scope, hint.chapter_uid, hint.message)
        for hint in report_b.hints
        if hint.kind in {"style_inconsistency", "unusual_density"}
    ]

    assert filtered_a == filtered_b
    assert any(kind == "style_inconsistency" for kind, _, _, _ in filtered_a)
    assert any(kind == "unusual_density" for kind, _, _, _ in filtered_a)
