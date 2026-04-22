"""Tests for footnote_verifier stage 7."""

from unittest.mock import MagicMock

import pytest

from epubforge.config import Config
from epubforge.footnote_verifier import (
    FootnoteEditOp,
    FootnoteVerifyOutput,
    _apply_fn_ops,
    _collect_chapter_descriptors,
    _estimate_tokens,
    _validate_paired_invariants,
)
from epubforge.ir.semantic import Book, Chapter, Footnote, Paragraph, Provenance, Table


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _prov(page: int) -> Provenance:
    return Provenance(page=page, source="llm")


def _para(text: str, page: int, cross_page: bool = False) -> Paragraph:
    return Paragraph(text=text, provenance=_prov(page), cross_page=cross_page)


def _fn(callout: str, page: int, paired: bool = False, text: str = "note body") -> Footnote:
    return Footnote(callout=callout, text=text, paired=paired, provenance=_prov(page))


def _table(html: str, page: int) -> Table:
    return Table(html=html, provenance=_prov(page))


def _book(*chapters: Chapter) -> Book:
    return Book(title="Test Book", chapters=list(chapters))


def _chapter(title: str, *blocks) -> Chapter:
    return Chapter(title=title, blocks=list(blocks))


def _op(**kwargs) -> FootnoteEditOp:
    defaults = {"reason": "test", "confidence": 0.9}
    defaults.update(kwargs)
    return FootnoteEditOp(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _apply_fn_ops: pair
# ---------------------------------------------------------------------------

def test_pair_basic() -> None:
    """pair op links raw callout in source block to FN body."""
    book = _book(_chapter(
        "ch0",
        _para("text ① more", page=1),
        _fn("①", page=1, paired=False),
    ))
    report: list = []
    applied = _apply_fn_ops(
        book,
        [_op(op="pair", fn_block_id="0_1", source_block_id="0_0", callout="①")],
        ch_idx=0,
        report=report,
    )
    assert applied == 1
    fn = book.chapters[0].blocks[1]
    assert isinstance(fn, Footnote) and fn.paired is True
    para = book.chapters[0].blocks[0]
    assert isinstance(para, Paragraph) and "\x02fn-1-①\x03" in para.text
    assert report[0]["op"] == "pair"


def test_pair_with_occurrence_index() -> None:
    """pair with occurrence_index=1 replaces the second raw callout."""
    book = _book(_chapter(
        "ch0",
        _para("A① B① C", page=2),
        _fn("①", page=2, paired=False),
    ))
    report: list = []
    _apply_fn_ops(
        book,
        [_op(op="pair", fn_block_id="0_1", source_block_id="0_0",
             callout="①", occurrence_index=1)],
        ch_idx=0,
        report=report,
    )
    para_text = book.chapters[0].blocks[0].text  # type: ignore[union-attr]
    assert "A①" in para_text  # first occurrence untouched
    assert "\x02fn-2-①\x03" in para_text  # second replaced


def test_pair_callout_mismatch_skipped() -> None:
    """pair op with wrong callout field is skipped."""
    book = _book(_chapter(
        "ch0",
        _para("text ①", page=1),
        _fn("①", page=1, paired=False),
    ))
    report: list = []
    applied = _apply_fn_ops(
        book,
        [_op(op="pair", fn_block_id="0_1", source_block_id="0_0", callout="②")],
        ch_idx=0,
        report=report,
    )
    assert applied == 0
    fn = book.chapters[0].blocks[1]
    assert isinstance(fn, Footnote) and fn.paired is False


def test_pair_invalid_source_block_id_skipped() -> None:
    book = _book(_chapter(
        "ch0",
        _para("text ①", page=1),
        _fn("①", page=1, paired=False),
    ))
    report: list = []
    applied = _apply_fn_ops(
        book,
        [_op(op="pair", fn_block_id="0_1", source_block_id="9_0", callout="①")],
        ch_idx=0,
        report=report,
    )
    assert applied == 0
    fn = book.chapters[0].blocks[1]
    assert isinstance(fn, Footnote) and fn.paired is False
    assert report == []


def test_pair_low_confidence_skipped() -> None:
    book = _book(_chapter(
        "ch0",
        _para("text ①", page=1),
        _fn("①", page=1, paired=False),
    ))
    report: list = []
    applied = _apply_fn_ops(
        book,
        [_op(op="pair", fn_block_id="0_1", source_block_id="0_0",
             callout="①", confidence=0.5)],
        ch_idx=0,
        report=report,
    )
    assert applied == 0


# ---------------------------------------------------------------------------
# _apply_fn_ops: unpair
# ---------------------------------------------------------------------------

def test_unpair_basic() -> None:
    marker = "\x02fn-5-①\x03"
    book = _book(_chapter(
        "ch0",
        _para(f"text {marker} end", page=5),
        _fn("①", page=5, paired=True),
    ))
    report: list = []
    applied = _apply_fn_ops(
        book,
        [_op(op="unpair", fn_block_id="0_1", source_block_id="0_0", callout="①")],
        ch_idx=0,
        report=report,
    )
    assert applied == 1
    fn = book.chapters[0].blocks[1]
    assert isinstance(fn, Footnote) and fn.paired is False
    para = book.chapters[0].blocks[0]
    assert isinstance(para, Paragraph) and "①" in para.text
    assert "\x02" not in para.text


# ---------------------------------------------------------------------------
# _apply_fn_ops: relink
# ---------------------------------------------------------------------------

def test_relink_basic() -> None:
    marker = "\x02fn-3-①\x03"
    book = _book(_chapter(
        "ch0",
        _para(f"wrong {marker} context", page=3),
        _para("correct ① context", page=3),
        _fn("①", page=3, paired=True),
    ))
    report: list = []
    applied = _apply_fn_ops(
        book,
        [_op(op="relink", fn_block_id="0_2", source_block_id="0_0",
             new_source_block_id="0_1", callout="①")],
        ch_idx=0,
        report=report,
    )
    assert applied == 1
    # Old source no longer has marker
    old_para = book.chapters[0].blocks[0]
    assert isinstance(old_para, Paragraph) and "\x02" not in old_para.text
    # New source has marker
    new_para = book.chapters[0].blocks[1]
    assert isinstance(new_para, Paragraph) and "\x02fn-3-①\x03" in new_para.text
    fn = book.chapters[0].blocks[2]
    assert isinstance(fn, Footnote) and fn.paired is True
    assert report[0]["op"] == "relink"


def test_relink_invalid_new_source_skipped() -> None:
    marker = "\x02fn-3-①\x03"
    book = _book(_chapter(
        "ch0",
        _para(f"wrong {marker} context", page=3),
        _para("correct ① context", page=3),
        _fn("①", page=3, paired=True),
    ))
    report: list = []
    applied = _apply_fn_ops(
        book,
        [_op(op="relink", fn_block_id="0_2", source_block_id="0_0",
             new_source_block_id="9_1", callout="①")],
        ch_idx=0,
        report=report,
    )
    assert applied == 0
    old_para = book.chapters[0].blocks[0]
    assert isinstance(old_para, Paragraph) and marker in old_para.text
    fn = book.chapters[0].blocks[2]
    assert isinstance(fn, Footnote) and fn.paired is True
    assert report == []


# ---------------------------------------------------------------------------
# _apply_fn_ops: mark_orphan
# ---------------------------------------------------------------------------

def test_mark_orphan_basic() -> None:
    book = _book(_chapter(
        "ch0",
        _fn("*", page=10, paired=False),
    ))
    report: list = []
    applied = _apply_fn_ops(
        book,
        [_op(op="mark_orphan", fn_block_id="0_0", callout="*", confidence=0.85)],
        ch_idx=0,
        report=report,
    )
    assert applied == 1
    fn = book.chapters[0].blocks[0]
    assert isinstance(fn, Footnote) and fn.orphan is True and fn.paired is False


def test_mark_orphan_low_confidence_skipped() -> None:
    book = _book(_chapter(
        "ch0",
        _fn("*", page=10, paired=False),
    ))
    report: list = []
    applied = _apply_fn_ops(
        book,
        [_op(op="mark_orphan", fn_block_id="0_0", callout="*", confidence=0.75)],
        ch_idx=0,
        report=report,
    )
    assert applied == 0
    fn = book.chapters[0].blocks[0]
    assert isinstance(fn, Footnote) and fn.orphan is False


# ---------------------------------------------------------------------------
# _apply_fn_ops: conflict dedup
# ---------------------------------------------------------------------------

def test_conflict_dedup_keeps_highest_confidence() -> None:
    """When two ops target the same fn_block_id, highest confidence wins."""
    book = _book(_chapter(
        "ch0",
        _para("text ①", page=1),
        _para("other ①", page=1),
        _fn("①", page=1, paired=False),
    ))
    report: list = []
    ops = [
        _op(op="pair", fn_block_id="0_2", source_block_id="0_0",
            callout="①", confidence=0.72),
        _op(op="pair", fn_block_id="0_2", source_block_id="0_1",
            callout="①", confidence=0.88),
    ]
    applied = _apply_fn_ops(book, ops, ch_idx=0, report=report)
    assert applied == 1
    # The higher-confidence op (source_block_id="0_1") should win
    para1 = book.chapters[0].blocks[1]
    assert isinstance(para1, Paragraph) and "\x02fn-1-①\x03" in para1.text
    # source 0_0 should be untouched
    para0 = book.chapters[0].blocks[0]
    assert isinstance(para0, Paragraph) and "①" in para0.text and "\x02" not in para0.text


# ---------------------------------------------------------------------------
# _apply_fn_ops: table html update
# ---------------------------------------------------------------------------

def test_pair_in_table() -> None:
    book = _book(_chapter(
        "ch0",
        _table("<table><td>cell ①</td></table>", page=6),
        _fn("①", page=6, paired=False),
    ))
    report: list = []
    applied = _apply_fn_ops(
        book,
        [_op(op="pair", fn_block_id="0_1", source_block_id="0_0", callout="①")],
        ch_idx=0,
        report=report,
    )
    assert applied == 1
    tbl = book.chapters[0].blocks[0]
    assert isinstance(tbl, Table) and "\x02fn-6-①\x03" in tbl.html


# ---------------------------------------------------------------------------
# _validate_paired_invariants
# ---------------------------------------------------------------------------

def test_invariant_downgrade_orphaned_paired() -> None:
    """A Footnote marked paired=True but with no marker in book is downgraded."""
    book = _book(_chapter(
        "ch0",
        _para("text without marker", page=7),
        _fn("①", page=7, paired=True),  # paired but no marker in book
    ))
    _validate_paired_invariants(book)
    fn = book.chapters[0].blocks[1]
    assert isinstance(fn, Footnote) and fn.paired is False


def test_invariant_ok_when_marker_present() -> None:
    """A Footnote with marker present keeps paired=True."""
    book = _book(_chapter(
        "ch0",
        _para("text \x02fn-7-①\x03 end", page=7),
        _fn("①", page=7, paired=True),
    ))
    _validate_paired_invariants(book)
    fn = book.chapters[0].blocks[1]
    assert isinstance(fn, Footnote) and fn.paired is True


# ---------------------------------------------------------------------------
# _collect_chapter_descriptors
# ---------------------------------------------------------------------------

def test_collect_descriptors_structure() -> None:
    book = _book(_chapter(
        "ch0",
        _para("text ①", page=1),
        _fn("①", page=1, paired=False),
    ))
    d = _collect_chapter_descriptors(book, ch_idx=0)
    assert d["chapter_idx"] == 0
    assert len(d["footnote_bodies"]) == 1
    assert d["footnote_bodies"][0]["callout"] == "①"
    assert len(d["source_blocks"]) == 1
    assert d["source_blocks"][0]["kind"] == "paragraph"


def test_collect_descriptors_adjacent_context() -> None:
    """Adjacent chapter blocks on shared pages appear in adjacent_context."""
    ch0 = _chapter("ch0", _para("shared page text", page=5))
    ch1 = _chapter("ch1",
                   _para("also page 5", page=5),
                   _fn("②", page=5, paired=False))
    book = _book(ch0, ch1)
    d = _collect_chapter_descriptors(book, ch_idx=1)
    # ch0 block on page 5 should appear in adjacent_context
    adj_pages = {item["page"] for item in d["adjacent_context"]}
    assert 5 in adj_pages


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def test_estimate_tokens_reasonable() -> None:
    d = {"footnote_bodies": [{"text_preview": "x" * 300}], "source_blocks": []}
    est = _estimate_tokens(d, chars_per_token=3.0)
    assert est > 0


# ---------------------------------------------------------------------------
# verify_footnotes integration (mocked client)
# ---------------------------------------------------------------------------

def test_verify_footnotes_end_to_end(tmp_path) -> None:
    """verify_footnotes applies LLM ops and writes output files."""
    from epubforge.footnote_verifier import verify_footnotes

    book = _book(_chapter(
        "ch0",
        _para("text ①", page=1),
        _fn("①", page=1, paired=False),
    ))

    src = tmp_path / "06_proofread.json"
    out = tmp_path / "07_footnote_verified.json"
    report_path = tmp_path / "07_report.json"
    src.write_text(book.model_dump_json(), encoding="utf-8")

    cfg = Config(
        llm_api_key="test",
        cache_dir=tmp_path / "cache",
        footnote_verify_thinking_budget_tokens=0,
    )

    mock_client = MagicMock()
    mock_client.chat_parsed.return_value = FootnoteVerifyOutput(ops=[
        FootnoteEditOp(
            op="pair", fn_block_id="0_1", source_block_id="0_0",
            callout="①", occurrence_index=0, reason="test", confidence=0.9,
        )
    ])

    import epubforge.footnote_verifier as fv_module
    original_client = fv_module.LLMClient if hasattr(fv_module, "LLMClient") else None

    # Patch LLMClient inside the module
    import epubforge.llm.client as client_module
    original = client_module.LLMClient

    class _MockLLMClient:
        def __init__(self, *a, **kw):
            pass
        def chat_parsed(self, *a, **kw):
            return mock_client.chat_parsed(*a, **kw)

    client_module.LLMClient = _MockLLMClient  # type: ignore[assignment]
    try:
        verify_footnotes(src, out, cfg, report_path=report_path)
    finally:
        client_module.LLMClient = original  # type: ignore[assignment]

    result_book = Book.model_validate_json(out.read_text(encoding="utf-8"))
    fn = result_book.chapters[0].blocks[1]
    assert isinstance(fn, Footnote) and fn.paired is True
    para = result_book.chapters[0].blocks[0]
    assert isinstance(para, Paragraph) and "\x02fn-1-①\x03" in para.text

    import json
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(report) == 1
    assert report[0]["op"] == "pair"


def test_verify_footnotes_token_limit_raises(tmp_path) -> None:
    """verify_footnotes raises RuntimeError when chapter exceeds token limit."""
    from epubforge.footnote_verifier import verify_footnotes

    # Build a chapter with many footnotes to exceed a tiny limit
    blocks = [_para("text " + "①" * 5, page=i) for i in range(1, 10)]
    blocks += [_fn("①", page=i) for i in range(1, 10)]
    book = _book(_chapter("big chapter", *blocks))

    src = tmp_path / "06.json"
    src.write_text(book.model_dump_json(), encoding="utf-8")

    cfg = Config(
        llm_api_key="test",
        cache_dir=tmp_path / "cache",
        footnote_verify_max_chapter_tokens=1,  # impossibly small
        footnote_verify_thinking_budget_tokens=0,
    )

    with pytest.raises(RuntimeError, match="tokens > limit"):
        verify_footnotes(src, tmp_path / "out.json", cfg)
