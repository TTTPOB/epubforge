"""Tests for assembler._pair_footnotes page-scoped pairing."""

from epubforge.assembler import _pair_footnotes
from epubforge.ir.semantic import Block, Footnote, Heading, Paragraph, Provenance, Table


def _para(text: str, page: int) -> Paragraph:
    return Paragraph(text=text, provenance=Provenance(page=page, source="llm"))


def _fn(callout: str, page: int) -> Footnote:
    return Footnote(callout=callout, text="note text", provenance=Provenance(page=page, source="llm"))


def test_no_pairing_across_pages() -> None:
    """Footnote on page N must not pair with paragraph on page N-1."""
    blocks: list[Block] = [_para("Some text ①", page=3), _fn("①", page=4)]
    result = _pair_footnotes(blocks)
    fn = result[1]
    assert isinstance(fn, Footnote)
    assert not fn.paired
    para = result[0]
    assert isinstance(para, Paragraph)
    assert "\x02fn-" not in para.text


def _table(html: str, page: int) -> Table:
    return Table(html=html, provenance=Provenance(page=page, source="vlm"))


def _heading(text: str, page: int, level: int = 1) -> Heading:
    return Heading(text=text, level=level, provenance=Provenance(page=page, source="vlm"))


def test_pairing_table_prev_page() -> None:
    """Footnote on page N pairs with a table on page N-1 (cross-page table spans)."""
    blocks: list[Block] = [
        _table("<table><td>cell①</td></table>", page=3),
        _fn("①", page=4),
    ]
    result = _pair_footnotes(blocks)
    fn = result[1]
    assert isinstance(fn, Footnote)
    assert fn.paired
    tbl = result[0]
    assert isinstance(tbl, Table)
    assert "\x02fn-4-①\x03" in tbl.html


def test_no_pairing_paragraph_prev_page() -> None:
    """Footnote on page N must not pair with a paragraph on page N-1."""
    blocks: list[Block] = [_para("Some text ①", page=3), _fn("①", page=4)]
    result = _pair_footnotes(blocks)
    fn = result[1]
    assert isinstance(fn, Footnote)
    assert not fn.paired


def test_no_pairing_across_heading_boundary() -> None:
    """Footnote must not pair past a level-1 chapter heading boundary."""
    blocks: list[Block] = [
        _table("<table><td>cell①</td></table>", page=4),
        _heading("New Section", page=4, level=1),
        _fn("①", page=4),
    ]
    result = _pair_footnotes(blocks)
    fn = result[2]
    assert isinstance(fn, Footnote)
    assert not fn.paired


def test_pairing_past_subsection_heading() -> None:
    """Callout in chapter-intro paragraph pairs with footnote even when subsection headings intervene."""
    blocks: list[Block] = [
        _para("chapter intro with callout ①", page=5),
        _heading("第一节", page=5, level=2),
        _heading("一、子节", page=5, level=3),
        _fn("①", page=5),
    ]
    result = _pair_footnotes(blocks)
    fn = result[3]
    assert isinstance(fn, Footnote)
    assert fn.paired
    para = result[0]
    assert isinstance(para, Paragraph)
    assert "\x02fn-5-①\x03" in para.text


def test_pairing_same_page() -> None:
    """Footnote on page N pairs with paragraph on page N."""
    blocks: list[Block] = [_para("Some text ①", page=4), _fn("①", page=4)]
    result = _pair_footnotes(blocks)
    fn = result[1]
    assert isinstance(fn, Footnote)
    assert fn.paired
    para = result[0]
    assert isinstance(para, Paragraph)
    assert "\x02fn-4-①\x03" in para.text


def test_lifo_two_tables_same_callout() -> None:
    """LIFO: two tables both containing ① — footnote pairs with the most recent one."""
    blocks: list[Block] = [
        _table("<table><td>cell①</td></table>", page=3),
        _table("<table><td>also①</td></table>", page=4),
        _fn("①", page=4),
    ]
    result = _pair_footnotes(blocks)
    fn = result[2]
    assert isinstance(fn, Footnote)
    assert fn.paired
    # Most recent table (index 1) gets the marker
    tbl_recent = result[1]
    assert isinstance(tbl_recent, Table)
    assert "\x02fn-4-①\x03" in tbl_recent.html
    # Earlier table is untouched
    tbl_old = result[0]
    assert isinstance(tbl_old, Table)
    assert "\x02fn-" not in tbl_old.html


def test_three_page_spanning_table() -> None:
    """Table merged across pages 3-5 pairs with footnote on page 5 (large page gap)."""
    # After _merge_continued_tables the merged table has provenance.page = 3
    # but the callout ① was in the continuation rows (page 5 data).
    # The LIFO stack must ignore page distance for tables.
    from epubforge.ir.semantic import Provenance
    merged_table = Table(
        html="<table><td>row①</td></table>",
        provenance=Provenance(page=3, source="vlm"),
    )
    blocks: list[Block] = [merged_table, _fn("①", page=5)]
    result = _pair_footnotes(blocks)
    fn = result[1]
    assert isinstance(fn, Footnote)
    assert fn.paired
    tbl = result[0]
    assert isinstance(tbl, Table)
    assert "\x02fn-5-①\x03" in tbl.html


def test_lifo_multiple_footnote_bodies_same_callout() -> None:
    """Two same-callout footnote bodies each consume one stack entry (LIFO)."""
    blocks: list[Block] = [
        _para("text ① more", page=5),
        _para("also ① here", page=6),
        _fn("①", page=5),  # pairs with page-5 paragraph (same-page)
        _fn("①", page=6),  # pairs with page-6 paragraph (same-page)
    ]
    result = _pair_footnotes(blocks)
    fn5 = result[2]
    fn6 = result[3]
    assert isinstance(fn5, Footnote) and fn5.paired
    assert isinstance(fn6, Footnote) and fn6.paired
    # Each paragraph got its own marker
    assert "\x02fn-5-①\x03" in result[0].text  # type: ignore[union-attr]
    assert "\x02fn-6-①\x03" in result[1].text  # type: ignore[union-attr]
