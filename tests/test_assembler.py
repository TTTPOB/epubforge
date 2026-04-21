"""Tests for assembler._pair_footnotes page-scoped pairing."""

from epubforge.assembler import _pair_footnotes
from epubforge.ir.semantic import Block, Footnote, Paragraph, Provenance


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
