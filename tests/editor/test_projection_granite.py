"""Tests for granite dual-source support in render_chapter_projection (I4)."""

from __future__ import annotations

import pytest

from epubforge.editor.projection import _load_granite_per_page, render_chapter_projection
from epubforge.ir.semantic import Chapter, Paragraph, Provenance


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_prov(page: int) -> Provenance:
    return Provenance(page=page, bbox=None, source="docling")


@pytest.fixture
def three_block_chapter() -> Chapter:
    """Chapter with 3 blocks spanning pages 5 and 6."""
    return Chapter(
        uid="ch-test",
        title="Test Chapter",
        level=1,
        blocks=[
            Paragraph(
                uid="blk-p1",
                text="First paragraph on page 5.",
                role="body",
                provenance=_make_prov(5),
            ),
            Paragraph(
                uid="blk-p2",
                text="Second paragraph also on page 5.",
                role="body",
                provenance=_make_prov(5),
            ),
            Paragraph(
                uid="blk-p3",
                text="Third paragraph on page 6.",
                role="body",
                provenance=_make_prov(6),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Scenario 1: granite_pages=None — backward compatible
# ---------------------------------------------------------------------------


def test_no_granite_output_identical_to_legacy(three_block_chapter):
    """granite_pages=None must produce output identical to the legacy format."""
    result_default = render_chapter_projection(three_block_chapter)
    result_explicit_none = render_chapter_projection(three_block_chapter, granite_pages=None)

    # Both calls must be identical
    assert result_default == result_explicit_none

    # Must NOT contain any granite markers
    assert "[[granite-ref" not in result_default
    assert "granite cross-reference" not in result_default
    assert "ocr-cross-validation" not in result_default

    # Must still contain standard block markers
    assert "[[block blk-p1]]" in result_default
    assert "[[block blk-p2]]" in result_default
    assert "[[block blk-p3]]" in result_default


# ---------------------------------------------------------------------------
# Scenario 2: granite_pages provided — relevant pages included, unrelated excluded
# ---------------------------------------------------------------------------


def test_granite_pages_relevant_included_unrelated_excluded(three_block_chapter):
    """granite_pages with pages 5, 6, 999: only 5 and 6 appear in output."""
    granite_pages = {
        5: "page 5 md",
        6: "page 6 md",
        999: "unrelated page md",
    }

    result = render_chapter_projection(three_block_chapter, granite_pages=granite_pages)

    # Header comment with correct page list
    assert "<!-- granite cross-reference for pages 5, 6 -->" in result

    # Disclaimer comment referencing the rule
    assert "ocr-cross-validation.md" in result
    assert "SECONDARY evidence" in result

    # Granite markers for pages 5 and 6
    assert "[[granite-ref page=5]]" in result
    assert "[[granite-ref page=6]]" in result
    assert "page 5 md" in result
    assert "page 6 md" in result

    # Page 999 must NOT appear
    assert "[[granite-ref page=999]]" not in result
    assert "unrelated page md" not in result

    # Standard blocks must still be present after the granite section
    assert "[[block blk-p1]]" in result
    assert "[[block blk-p3]]" in result

    # Granite section must appear BEFORE the first block
    granite_pos = result.index("[[granite-ref page=5]]")
    first_block_pos = result.index("[[block blk-p1]]")
    assert granite_pos < first_block_pos


# ---------------------------------------------------------------------------
# Scenario 3: granite_pages={} — no granite header output
# ---------------------------------------------------------------------------


def test_empty_granite_pages_no_header(three_block_chapter):
    """granite_pages={} means no relevant pages, so no granite header is emitted."""
    result = render_chapter_projection(three_block_chapter, granite_pages={})

    assert "[[granite-ref" not in result
    assert "granite cross-reference" not in result

    # Standard blocks still present
    assert "[[block blk-p1]]" in result


# ---------------------------------------------------------------------------
# Scenario 4: _load_granite_per_page returns None when file missing
# ---------------------------------------------------------------------------


def test_load_granite_per_page_missing_file(tmp_path):
    """_load_granite_per_page returns None when 01_raw_granite.json does not exist."""
    result = _load_granite_per_page(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# Additional: page ordering in header
# ---------------------------------------------------------------------------


def test_granite_pages_ordered_in_header(three_block_chapter):
    """Pages in the granite header are listed in ascending numeric order."""
    granite_pages = {6: "page 6 md", 5: "page 5 md"}
    result = render_chapter_projection(three_block_chapter, granite_pages=granite_pages)

    # Verify pages appear in sorted order in the comment header
    assert "<!-- granite cross-reference for pages 5, 6 -->" in result

    # Also verify [[granite-ref]] markers appear in sorted order
    pos5 = result.index("[[granite-ref page=5]]")
    pos6 = result.index("[[granite-ref page=6]]")
    assert pos5 < pos6


# ---------------------------------------------------------------------------
# Additional: granite section separated from standard blocks by ---
# ---------------------------------------------------------------------------


def test_granite_section_separated_by_divider(three_block_chapter):
    """A horizontal rule (---) separates the granite header from the standard blocks."""
    granite_pages = {5: "page 5 md", 6: "page 6 md"}
    result = render_chapter_projection(three_block_chapter, granite_pages=granite_pages)

    # There should be a --- after the granite content and before the first block
    granite_end = result.index("page 6 md")
    first_block_pos = result.index("[[block blk-p1]]")
    section_between = result[granite_end:first_block_pos]
    assert "---" in section_between
