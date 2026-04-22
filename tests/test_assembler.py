"""Tests for assembler._pair_footnotes page-scoped pairing."""

from epubforge.assembler import _is_continuation_plausible, _pair_footnotes
from epubforge.ir.semantic import Block, Footnote, Heading, Paragraph, Provenance, Table


def _para(text: str, page: int) -> Paragraph:
    return Paragraph(text=text, provenance=Provenance(page=page, source="llm"))


def _fn(callout: str, page: int) -> Footnote:
    return Footnote(callout=callout, text="note text", provenance=Provenance(page=page, source="llm"))


def _table(html: str, page: int) -> Table:
    return Table(html=html, provenance=Provenance(page=page, source="vlm"))


def _merged_table(html: str, page: int) -> Table:
    """Simulate a table merged by _merge_continued_tables (multi_page=True)."""
    return Table(html=html, multi_page=True, provenance=Provenance(page=page, source="vlm"))


def _heading(text: str, page: int, level: int = 1) -> Heading:
    return Heading(text=text, level=level, provenance=Provenance(page=page, source="vlm"))


def _cross_page_para(text: str, page: int) -> Paragraph:
    return Paragraph(text=text, cross_page=True, provenance=Provenance(page=page, source="llm"))


# --- same-page pairing ---

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


def test_pairing_table_prev_page() -> None:
    """Regular table on page N-1 pairs with footnote on page N via P0 fallback."""
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


def test_pairing_across_heading_same_page() -> None:
    """Same-page callout pairs across level-1 heading (cross-chapter, same physical page)."""
    blocks: list[Block] = [
        _table("<table><td>cell①</td></table>", page=4),
        _heading("New Section", page=4, level=1),
        _fn("①", page=4),
    ]
    result = _pair_footnotes(blocks)
    fn = result[2]
    assert isinstance(fn, Footnote)
    assert fn.paired
    tbl = result[0]
    assert isinstance(tbl, Table)
    assert "\x02fn-4-①\x03" in tbl.html


def test_no_pairing_across_heading_different_page() -> None:
    """Callout on a different page than the heading is cleared at chapter boundary."""
    blocks: list[Block] = [
        _table("<table><td>cell①</td></table>", page=3),
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


# --- cross-page pairing ---

def test_same_page_wins_over_prev_page_fallback() -> None:
    """Same-page paragraph (P3) beats prev-page paragraph (P0) for LIFO selection."""
    blocks: list[Block] = [
        _para("Some text ①", page=3),
        _para("Also text ①", page=4),
        _fn("①", page=4),
    ]
    result = _pair_footnotes(blocks)
    fn = result[2]
    assert isinstance(fn, Footnote) and fn.paired
    # p4 paragraph (P3) wins
    para_p4 = result[1]
    assert isinstance(para_p4, Paragraph)
    assert "\x02fn-4-①\x03" in para_p4.text
    # p3 paragraph (P0 candidate, not selected) keeps raw callout
    para_p3 = result[0]
    assert isinstance(para_p3, Paragraph)
    assert "\x02fn-" not in para_p3.text


def test_prev_page_fallback_when_no_same_page() -> None:
    """When no same-page candidate exists, paragraph on prev page is matched via P0 fallback."""
    blocks: list[Block] = [_para("text ①", page=14), _fn("①", page=15)]
    result = _pair_footnotes(blocks)
    fn = result[1]
    assert isinstance(fn, Footnote) and fn.paired
    para = result[0]
    assert isinstance(para, Paragraph)
    assert "\x02fn-15-①\x03" in para.text


def test_cross_page_paragraph_pairs_across_page() -> None:
    """Cross-page paragraph (P1) pairs with footnote on next page."""
    blocks: list[Block] = [_cross_page_para("text spanning pages ①", page=14), _fn("①", page=15)]
    result = _pair_footnotes(blocks)
    fn = result[1]
    assert isinstance(fn, Footnote)
    assert fn.paired
    para = result[0]
    assert isinstance(para, Paragraph)
    assert "\x02fn-15-①\x03" in para.text


def test_cross_page_paragraph_source_portion_callout() -> None:
    """Cross-page paragraph whose callout is in its SOURCE portion matches FN on source page."""
    blocks: list[Block] = [_cross_page_para("text ①", page=74), _fn("①", page=74)]
    result = _pair_footnotes(blocks)
    fn = result[1]
    assert isinstance(fn, Footnote) and fn.paired
    para = result[0]
    assert isinstance(para, Paragraph)
    assert "\x02fn-74-①\x03" in para.text


def test_cross_page_paragraph_source_portion_loses_to_p3() -> None:
    """P3 regular para wins source selection; cross-page para gets salvaged to same marker."""
    blocks: list[Block] = [
        _cross_page_para("cross-page text ①", page=5),
        _para("regular text ①", page=5),
        _fn("①", page=5),
    ]
    result = _pair_footnotes(blocks)
    fn = result[2]
    assert isinstance(fn, Footnote) and fn.paired
    # Regular para (P3) wins source selection
    assert "\x02fn-5-①\x03" in result[1].text  # type: ignore[union-attr]
    # Cross-page para (page=5, eff_pages={5,6}) also gets salvaged (duplicate callout on same page)
    assert "\x02fn-5-①\x03" in result[0].text  # type: ignore[union-attr]


def test_cross_page_paragraph_does_not_steal_same_page_fn() -> None:
    """Regular para (P3) wins source selection; cross-page para gets salvaged to same marker."""
    blocks: list[Block] = [
        _para("regular p64 ①", page=64),
        _cross_page_para("continuation p64 ①", page=64),
        _fn("①", page=64),
    ]
    result = _pair_footnotes(blocks)
    fn = result[2]
    assert isinstance(fn, Footnote) and fn.paired
    # Regular para (P3) wins source selection
    assert "\x02fn-64-①\x03" in result[0].text  # type: ignore[union-attr]
    # Cross-page para (page=64, eff_pages={64,65}) also gets salvaged
    assert "\x02fn-64-①\x03" in result[1].text  # type: ignore[union-attr]


def test_cross_page_paragraph_pairs_with_next_page_fn() -> None:
    """Cross-page paragraph (P1) pairs with next-page FN after regular para consumed same-page FN."""
    blocks: list[Block] = [
        _para("regular p64 ①", page=64),
        _cross_page_para("continuation p64 ①", page=64),
        _fn("①", page=64),
        _fn("①", page=65),
    ]
    result = _pair_footnotes(blocks)
    fn64 = result[2]
    fn65 = result[3]
    assert isinstance(fn64, Footnote) and fn64.paired
    assert isinstance(fn65, Footnote) and fn65.paired
    # Regular para → fn64 (P3)
    assert "\x02fn-64-①\x03" in result[0].text  # type: ignore[union-attr]
    # Cross-page para → fn65 (P1)
    assert "\x02fn-65-①\x03" in result[1].text  # type: ignore[union-attr]


def test_merged_table_does_not_steal_same_page_paragraph() -> None:
    """Regular para (P3) wins source selection; merged table gets salvaged to same marker."""
    blocks: list[Block] = [
        _para("regular p83 ①", page=83),
        _merged_table("<table><td>cell①</td></table>", page=83),
        _fn("①", page=83),
    ]
    result = _pair_footnotes(blocks)
    fn = result[2]
    assert isinstance(fn, Footnote) and fn.paired
    # Regular para (P3) wins source selection
    assert "\x02fn-83-①\x03" in result[0].text  # type: ignore[union-attr]
    # Merged table (same page) also gets salvaged (duplicate callout, one FN body)
    assert "\x02fn-83-①\x03" in result[1].html  # type: ignore[union-attr]


# --- LIFO and multi-callout ---

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
    merged_table = Table(
        html="<table><td>row①</td></table>",
        multi_page=True,
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
        _fn("①", page=5),  # pairs with page-5 paragraph (same-page P3)
        _fn("①", page=6),  # pairs with page-6 paragraph (same-page P3)
    ]
    result = _pair_footnotes(blocks)
    fn5 = result[2]
    fn6 = result[3]
    assert isinstance(fn5, Footnote) and fn5.paired
    assert isinstance(fn6, Footnote) and fn6.paired
    assert "\x02fn-5-①\x03" in result[0].text  # type: ignore[union-attr]
    assert "\x02fn-6-①\x03" in result[1].text  # type: ignore[union-attr]


def test_regular_table_wins_tie_with_orphan_paragraph() -> None:
    """Table wins ① via LIFO; para's raw ①s are salvaged to fn-34-①; para gets ② directly."""
    blocks: list[Block] = [
        _para("text ①①②", page=34),
        _table("<table><td>①</td></table>", page=34),
        _fn("①", page=34),
        _fn("②", page=34),
    ]
    result = _pair_footnotes(blocks)
    fn1 = result[2]
    fn2 = result[3]
    assert isinstance(fn1, Footnote) and fn1.paired
    assert isinstance(fn2, Footnote) and fn2.paired
    # Table gets fn(①) (LIFO: table pushed after para, scanned first at same P3 priority)
    tbl = result[1]
    assert isinstance(tbl, Table)
    assert "\x02fn-34-①\x03" in tbl.html
    # Para gets fn(②) directly
    para = result[0]
    assert isinstance(para, Paragraph)
    assert "\x02fn-34-②\x03" in para.text
    # Para's two raw ① are salvaged to the same fn-34-① marker (book typo: duplicate callout)
    assert para.text.count("\x02fn-34-①\x03") == 2


# --- first_footnote_continues_prev_footnote hard filter ---

def test_fn_continuation_rejected_on_callout_mismatch() -> None:
    """_is_continuation_plausible rejects when callouts differ (VLM self-contradiction)."""
    prev = Footnote(callout="⑧", text="some text.", provenance=Provenance(page=10, source="vlm"))
    cont = Footnote(callout="①", text="new fn.", provenance=Provenance(page=11, source="vlm"))
    assert not _is_continuation_plausible(prev, cont)


def test_fn_continuation_accepted_when_callout_empty() -> None:
    """_is_continuation_plausible accepts when cont callout is empty (VLM prompt contract)."""
    prev = Footnote(callout="⑧", text="text without end", provenance=Provenance(page=10, source="vlm"))
    cont = Footnote(callout="", text="continuation.", provenance=Provenance(page=11, source="vlm"))
    assert _is_continuation_plausible(prev, cont)


def test_fn_continuation_accepted_when_prev_ends_with_period() -> None:
    """_is_continuation_plausible does not reject based on trailing period (e.g. 'Dr.' abbreviation)."""
    prev = Footnote(callout="⑧", text="See Dr.", provenance=Provenance(page=10, source="vlm"))
    cont = Footnote(callout="", text="Smith's findings.", provenance=Provenance(page=11, source="vlm"))
    assert _is_continuation_plausible(prev, cont)


def test_fn_continuation_rejected_when_prev_is_none() -> None:
    """_is_continuation_plausible rejects when there is no preceding footnote."""
    cont = Footnote(callout="", text="continuation.", provenance=Provenance(page=1, source="vlm"))
    assert not _is_continuation_plausible(None, cont)


# --- Fix 1: cross-chapter same-page pairing ---

def test_cross_chapter_same_page_pairing() -> None:
    """Para before a level-1 heading pairs with FN on the same physical page (cross-chapter layout)."""
    blocks: list[Block] = [
        _para("text ①", page=10),
        _heading("Chapter 2", page=10, level=1),
        _fn("①", page=10),
    ]
    result = _pair_footnotes(blocks)
    fn = result[2]
    assert isinstance(fn, Footnote) and fn.paired
    para = result[0]
    assert isinstance(para, Paragraph)
    assert "\x02fn-10-①\x03" in para.text


# --- Fix 2: P0 distance limit ---

def test_p0_fallback_distance_limit() -> None:
    """P0 fallback does not fire when distance exceeds 1 page (distance=3 here)."""
    blocks: list[Block] = [
        _para("text ①", page=10),
        _fn("①", page=13),
    ]
    result = _pair_footnotes(blocks)
    fn = result[1]
    assert isinstance(fn, Footnote)
    assert not fn.paired
    para = result[0]
    assert isinstance(para, Paragraph)
    assert "①" in para.text
    assert "\x02" not in para.text


def test_salvage_does_not_borrow_far_page_marker() -> None:
    """Salvage has no effect when the only FN is on a distant page (P0 rejected, no paired FN on p10)."""
    blocks: list[Block] = [
        _para("text ①", page=10),
        _fn("①", page=20),
    ]
    result = _pair_footnotes(blocks)
    fn = result[1]
    assert isinstance(fn, Footnote) and not fn.paired
    para = result[0]
    assert isinstance(para, Paragraph)
    assert "①" in para.text
    assert "\x02" not in para.text


# --- Fix 3: salvage duplicate callouts ---

def test_salvage_duplicate_callouts_same_page() -> None:
    """When two paras on the same page share a callout, the second gets salvaged to the same marker."""
    blocks: list[Block] = [
        _para("first ①", page=10),
        _para("second ①", page=10),
        _fn("①", page=10),
    ]
    result = _pair_footnotes(blocks)
    fn = result[2]
    assert isinstance(fn, Footnote) and fn.paired
    para1 = result[0]
    para2 = result[1]
    assert isinstance(para1, Paragraph)
    assert isinstance(para2, Paragraph)
    # One of them got the marker via main loop, the other via salvage
    assert "\x02fn-10-①\x03" in para1.text or "\x02fn-10-①\x03" in para2.text
    assert "\x02fn-10-①\x03" in para1.text and "\x02fn-10-①\x03" in para2.text
