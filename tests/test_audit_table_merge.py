"""Tests for the cross-page table merge audit detector."""

from __future__ import annotations

from epubforge.audit.table_merge import detect_table_merge_issues
from epubforge.ir.semantic import Book, Chapter, Provenance, Table


def _prov(page: int) -> Provenance:
    return Provenance(page=page, source="passthrough")


def _table(
    uid: str | None,
    html: str,
    page: int,
    *,
    multi_page: bool = False,
    continuation: bool = False,
) -> Table:
    return Table(
        uid=uid,
        html=html,
        multi_page=multi_page,
        continuation=continuation,
        provenance=_prov(page),
    )


def _book_with_table(table: Table) -> Book:
    chapter = Chapter(uid="ch-1", title="Test Chapter", blocks=[table])
    return Book(title="Test Book", chapters=[chapter])


# ---------------------------------------------------------------------------
# test_detect_width_drift
# ---------------------------------------------------------------------------

def test_detect_width_drift() -> None:
    # Segment 0 has 3 columns, segment 1 has 5 columns — drift = 66%, above 25% threshold.
    html = (
        "<table>"
        "<thead><tr><th>A</th><th>B</th><th>C</th></tr></thead>"
        "<tbody>"
        "<tr><td>a1</td><td>b1</td><td>c1</td></tr>"
        "<tr><td>a2</td><td>b2</td><td>c2</td></tr>"
        "</tbody>"
        "<tbody>"
        "<tr><td>x1</td><td>x2</td><td>x3</td><td>x4</td><td>x5</td></tr>"
        "</tbody>"
        "</table>"
    )
    tbl = _table("tbl-mp", html, 10, multi_page=True)
    book = _book_with_table(tbl)

    bundle = detect_table_merge_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "table.merge_width_drift" in codes


def test_no_width_drift_when_columns_match() -> None:
    # Both segments have 2 columns — no drift.
    html = (
        "<table>"
        "<tbody><tr><td>a</td><td>b</td></tr></tbody>"
        "<tbody><tr><td>c</td><td>d</td></tr></tbody>"
        "</table>"
    )
    tbl = _table("tbl-ok", html, 5, multi_page=True)
    book = _book_with_table(tbl)

    bundle = detect_table_merge_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "table.merge_width_drift" not in codes


# ---------------------------------------------------------------------------
# test_detect_header_reintroduced
# ---------------------------------------------------------------------------

def test_detect_header_reintroduced() -> None:
    # Two <thead> blocks in merged result — assembler failed to strip the continuation header.
    html = (
        "<table>"
        "<thead><tr><th>Col1</th><th>Col2</th></tr></thead>"
        "<tbody><tr><td>r1c1</td><td>r1c2</td></tr></tbody>"
        "<thead><tr><th>Col1 (repeat)</th><th>Col2 (repeat)</th></tr></thead>"
        "<tbody><tr><td>r2c1</td><td>r2c2</td></tr></tbody>"
        "</table>"
    )
    tbl = _table("tbl-hdr", html, 15, multi_page=True)
    book = _book_with_table(tbl)

    bundle = detect_table_merge_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "table.merge_header_reintroduced" in codes


def test_no_header_reintroduced_with_single_thead() -> None:
    # Only one <thead> — no issue.
    html = (
        "<table>"
        "<thead><tr><th>Col1</th><th>Col2</th></tr></thead>"
        "<tbody><tr><td>r1c1</td><td>r1c2</td></tr></tbody>"
        "<tbody><tr><td>r2c1</td><td>r2c2</td></tr></tbody>"
        "</table>"
    )
    tbl = _table("tbl-ok-hdr", html, 5, multi_page=True)
    book = _book_with_table(tbl)

    bundle = detect_table_merge_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "table.merge_header_reintroduced" not in codes


# ---------------------------------------------------------------------------
# test_detect_orphan_continuation
# ---------------------------------------------------------------------------

def test_detect_orphan_continuation() -> None:
    # continuation=True but multi_page=False: assembler found no predecessor table.
    # This is the orphan continuation case — a table block left stranded.
    html = "<table><tbody><tr><td>orphan data</td></tr></tbody></table>"
    # Crucially: multi_page=False, continuation=True.
    tbl = _table("tbl-orphan", html, 20, multi_page=False, continuation=True)
    book = _book_with_table(tbl)

    bundle = detect_table_merge_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "table.merge_orphan_continuation" in codes
    # Must NOT fire multi_page checks since multi_page=False.
    assert "table.merge_width_drift" not in codes
    assert "table.merge_header_reintroduced" not in codes
    assert "table.merge_record_incomplete" not in codes


def test_no_orphan_when_continuation_absorbed() -> None:
    # multi_page=True means assembler successfully merged the continuation;
    # no orphan issue should fire.
    html = (
        "<table>"
        "<tbody><tr><td>merged row</td></tr></tbody>"
        "</table>"
    )
    tbl = _table("tbl-merged", html, 8, multi_page=True, continuation=False)
    book = _book_with_table(tbl)

    bundle = detect_table_merge_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "table.merge_orphan_continuation" not in codes


def test_no_orphan_for_plain_non_continuation_table() -> None:
    # A regular table with neither continuation nor multi_page.
    html = "<table><tbody><tr><td>normal</td></tr></tbody></table>"
    tbl = _table("tbl-plain", html, 3, multi_page=False, continuation=False)
    book = _book_with_table(tbl)

    bundle = detect_table_merge_issues(book)
    assert len(bundle.issues) == 0


# ---------------------------------------------------------------------------
# test_detect_merge_record_incomplete
# ---------------------------------------------------------------------------

def test_detect_merge_record_incomplete() -> None:
    # multi_page=True but no <tbody> content — merge produced an empty result.
    html = "<table><thead><tr><th>Col</th></tr></thead></table>"
    tbl = _table("tbl-empty", html, 30, multi_page=True)
    book = _book_with_table(tbl)

    bundle = detect_table_merge_issues(book)
    codes = [issue.code for issue in bundle.issues]

    assert "table.merge_record_incomplete" in codes
