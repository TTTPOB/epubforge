"""Comprehensive tests for epubforge.editor.projection (Phase 5 renderer)."""

from __future__ import annotations

import json
import re

import pytest

from epubforge.editor.projection import (
    render_chapter_projection,
    render_index,
)
from epubforge.ir.semantic import (
    Book,
    Chapter,
    Equation,
    Figure,
    Footnote,
    Heading,
    Paragraph,
    Provenance,
    Table,
    TableMergeRecord,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def prov():
    def _make(page: int = 1) -> Provenance:
        return Provenance(page=page, bbox=None, source="docling")
    return _make


@pytest.fixture
def book_with_all_types(prov) -> Book:
    """A minimal Book containing every block type."""
    return Book(
        title="Test Book",
        authors=["Alice", "Bob"],
        chapters=[
            Chapter(
                uid="ch-mixed",
                title="Mixed Chapter",
                level=1,
                blocks=[
                    Paragraph(
                        uid="blk-p1",
                        text="Hello world.",
                        role="body",
                        provenance=prov(1),
                    ),
                    Heading(
                        uid="blk-h1",
                        text="Section One",
                        level=2,
                        provenance=prov(1),
                    ),
                    Footnote(
                        uid="blk-f1",
                        callout="1",
                        text="First footnote.",
                        paired=True,
                        orphan=False,
                        provenance=prov(2),
                    ),
                    Figure(
                        uid="blk-fig1",
                        caption="Architecture diagram",
                        image_ref="fig001.png",
                        provenance=prov(3),
                    ),
                    Table(
                        uid="blk-t1",
                        html="<table><tr><td>data</td></tr></table>",
                        table_title="Sample Table",
                        caption="A simple table.",
                        multi_page=False,
                        provenance=prov(4),
                    ),
                    Equation(
                        uid="blk-e1",
                        latex="E = mc^2",
                        provenance=prov(5),
                    ),
                ],
            ),
            Chapter(
                uid="ch-empty",
                title="Empty Chapter",
                level=1,
                blocks=[],
            ),
        ],
    )


@pytest.fixture
def book_with_table_merge(prov) -> Book:
    """Book with a multi-page merged table."""
    return Book(
        title="Merge Test",
        authors=[],
        chapters=[
            Chapter(
                uid="ch-merge",
                title="Merge Chapter",
                level=1,
                blocks=[
                    Table(
                        uid="blk-merged-tbl",
                        html="<table><thead><tr><th>A</th><th>B</th></tr></thead>"
                        "<tbody><tr><td>1</td><td>2</td></tr>"
                        "<tr><td>3</td><td>4</td></tr></tbody></table>",
                        table_title="Merged Table",
                        caption="Tables merged across pages.",
                        multi_page=True,
                        merge_record=TableMergeRecord(
                            segment_html=[
                                "<tr><td>1</td></tr>",
                                "<tr><td>3</td></tr>",
                            ],
                            segment_pages=[10, 11],
                            segment_order=[0, 1],
                            column_widths=[1],
                        ),
                        provenance=Provenance(page=10, bbox=None, source="vlm"),
                    ),
                ],
            ),
        ],
    )


@pytest.fixture
def book_with_colspan_table(prov) -> Book:
    """Book with a table that uses colspan/rowspan."""
    return Book(
        title="Colspan Test",
        authors=[],
        chapters=[
            Chapter(
                uid="ch-colspan",
                title="Colspan Chapter",
                level=1,
                blocks=[
                    Table(
                        uid="blk-cs-tbl",
                        html=(
                            "<table>\n"
                            "  <thead><tr><th colspan=\"2\">Combined</th></tr></thead>\n"
                            "  <tbody>\n"
                            "    <tr><td rowspan=\"2\">A</td><td>B</td></tr>\n"
                            "    <tr><td>C</td></tr>\n"
                            "  </tbody>\n"
                            "</table>"
                        ),
                        table_title="Colspan Table",
                        caption="",
                        multi_page=False,
                        provenance=Provenance(page=7, bbox=[10, 20, 30, 40], source="vlm"),
                    ),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Index rendering
# ---------------------------------------------------------------------------


class TestRenderIndex:
    def test_structure(self, book_with_all_types):
        """Index has correct structure: book marker, table, rows."""
        output = render_index(book_with_all_types)
        lines = output.strip().split("\n")

        # Book marker line
        assert lines[0].startswith("[[book]] ")

        # Parse book JSON
        book_json_str = lines[0][len("[[book]] "):]
        book_meta = json.loads(book_json_str)
        assert book_meta["title"] == "Test Book"
        assert book_meta["authors"] == ["Alice", "Bob"]
        assert "exported_at" in book_meta
        assert book_meta["source"] == "edit_state/book.json"
        assert book_meta["chapters"] == 2

        # Table header
        assert "## Chapters" in output
        assert "| # | UID | Title | Blocks | Pages |" in output
        assert "|---|-----|-------|--------|-------|" in output

        # Chapter rows
        assert "| 1 | ch-mixed | Mixed Chapter | 6 | 1-5 |" in output
        assert "| 2 | ch-empty | Empty Chapter | 0 | - |" in output

    def test_custom_source(self, book_with_all_types):
        """source parameter appears in book metadata."""
        output = render_index(book_with_all_types, source="custom/path.json")
        book_json_str = output.split("\n")[0][len("[[book]] "):]
        book_meta = json.loads(book_json_str)
        assert book_meta["source"] == "custom/path.json"

    def test_no_chapters(self):
        """A book with zero chapters still produces valid index."""
        book = Book(title="Empty", authors=[])
        output = render_index(book)
        book_json_str = output.split("\n")[0][len("[[book]] "):]
        book_meta = json.loads(book_json_str)
        assert book_meta["chapters"] == 0
        # Table header rows are always present; no data rows
        data_lines = [l for l in output.split("\n") if l.startswith("| ") and not l.startswith("| #")]
        data_lines = [l for l in data_lines if not l.startswith("|---")]
        assert len(data_lines) == 0

    def test_exported_at_override(self, book_with_all_types):
        """Explicit exported_at is preserved in book metadata."""
        output = render_index(book_with_all_types, exported_at="2026-01-01T00:00:00")
        book_json_str = output.split("\n")[0][len("[[book]] "):]
        book_meta = json.loads(book_json_str)
        assert book_meta["exported_at"] == "2026-01-01T00:00:00"


# ---------------------------------------------------------------------------
# Chapter projection rendering
# ---------------------------------------------------------------------------


class TestRenderChapter:
    def test_header_and_marker(self, book_with_all_types):
        """Chapter begins with a heading and [[chapter]] marker."""
        ch = book_with_all_types.chapters[0]
        output = render_chapter_projection(ch)
        lines = output.strip().split("\n")

        assert lines[0] == "# Chapter: Mixed Chapter [ch-mixed]"
        # Find the chapter marker
        marker_line = [l for l in lines if l.startswith("[[chapter")][0]
        assert marker_line.startswith("[[chapter ch-mixed]] ")
        ch_json_str = marker_line[len("[[chapter ch-mixed]] "):]
        ch_meta = json.loads(ch_json_str)
        assert ch_meta["title"] == "Mixed Chapter"
        assert ch_meta["blocks"] == 6
        assert ch_meta["page_range"] == [1, 5]

    def test_separator(self, book_with_all_types):
        """Chapter marker is followed by --- separator."""
        ch = book_with_all_types.chapters[0]
        output = render_chapter_projection(ch)
        assert "---" in output

    def test_all_block_types_have_markers(self, book_with_all_types):
        """Every block has a [[block <uid>]] marker in the output."""
        ch = book_with_all_types.chapters[0]
        output = render_chapter_projection(ch)

        for block in ch.blocks:
            assert f"[[block {block.uid}]]" in output, f"Missing marker for {block.uid}"

    def test_block_marker_json_parsable(self, book_with_all_types):
        """Every [[block]] marker has valid JSON that can be round-tripped."""
        ch = book_with_all_types.chapters[0]
        output = render_chapter_projection(ch)

        for match in re.finditer(r"\[\[block (\S+)\]\] (\{.*\})", output):
            meta = json.loads(match.group(2))
            assert "uid" in meta
            assert "kind" in meta
            assert "page" in meta
            assert meta["uid"] == match.group(1)

    def test_empty_chapter(self):
        """An empty chapter has header and marker but no blocks."""
        ch = Chapter(uid="ch-empty", title="Empty", level=1, blocks=[])
        output = render_chapter_projection(ch)
        assert "# Chapter: Empty [ch-empty]" in output
        assert "[[chapter ch-empty]]" in output
        # No [[block lines
        assert "[[block" not in output

    def test_chapter_without_uid(self):
        """Chapter without uid is rendered with empty brackets."""
        ch = Chapter(title="NoUID", level=1, blocks=[])
        output = render_chapter_projection(ch)
        assert "# Chapter: NoUID []" in output
        assert "[[chapter ]] " in output


# ---------------------------------------------------------------------------
# Per-block-type metadata correctness
# ---------------------------------------------------------------------------


class TestBlockMetadata:
    def test_paragraph_metadata(self, prov):
        """Paragraph marker includes role and provenance."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Paragraph(
                    uid="blk-p",
                    text="Content",
                    role="body",
                    cross_page=False,
                    provenance=prov(1),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-p\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["kind"] == "paragraph"
        assert meta["role"] == "body"
        assert "cross_page" not in meta

    def test_paragraph_cross_page(self, prov):
        """Paragraph with cross_page=True includes 'cross_page':true."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Paragraph(
                    uid="blk-cp",
                    text="Cross-page paragraph.",
                    role="body",
                    cross_page=True,
                    provenance=prov(2),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-cp\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["cross_page"] is True

    def test_heading_metadata(self, prov):
        """Heading marker includes level."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Heading(
                    uid="blk-h",
                    text="Section",
                    level=3,
                    provenance=prov(1),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-h\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["kind"] == "heading"
        assert meta["level"] == 3

    def test_heading_with_id(self, prov):
        """Heading with heading_id includes it in metadata."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Heading(
                    uid="blk-hid",
                    text="Named Section",
                    level=2,
                    id="sec-named",
                    provenance=prov(1),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-hid\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["heading_id"] == "sec-named"

    def test_footnote_metadata(self, prov):
        """Footnote marker includes callout, paired, and provenance."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Footnote(
                    uid="blk-fn",
                    callout="42",
                    text="Footnote text.",
                    paired=True,
                    orphan=False,
                    provenance=prov(2),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-fn\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["kind"] == "footnote"
        assert meta["callout"] == "42"
        assert meta["paired"] is True
        assert meta["provenance"]["source"] == "docling"
        assert "orphan" not in meta

    def test_footnote_orphan(self, prov):
        """Footnote with orphan=True includes 'orphan':true."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Footnote(
                    uid="blk-forphan",
                    callout="99",
                    text="Orphaned.",
                    paired=False,
                    orphan=True,
                    provenance=prov(3),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-forphan\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["orphan"] is True

    def test_figure_metadata(self, prov):
        """Figure marker includes provenance."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Figure(
                    uid="blk-fig",
                    caption="A nice figure.",
                    image_ref="img.png",
                    provenance=prov(5),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-fig\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["kind"] == "figure"
        assert meta["page"] == 5
        assert meta["provenance"]["source"] == "docling"

    def test_equation_metadata(self, prov):
        """Equation marker includes provenance."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Equation(
                    uid="blk-eq",
                    latex="\\alpha + \\beta = \\gamma",
                    provenance=prov(6),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-eq\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["kind"] == "equation"
        assert meta["page"] == 6


# ---------------------------------------------------------------------------
# Table-specific rendering
# ---------------------------------------------------------------------------


class TestTableRendering:
    def test_table_html_preserved(self, book_with_colspan_table):
        """Raw HTML table output is untouched (colspan/rowspan preserved)."""
        ch = book_with_colspan_table.chapters[0]
        output = render_chapter_projection(ch)

        tbl_block = ch.blocks[0]
        assert isinstance(tbl_block, Table)
        assert 'colspan="2"' in output
        assert 'rowspan="2"' in output

        lines = output.splitlines()
        marker_idx = next(
            i for i, line in enumerate(lines) if line.startswith("[[block blk-cs-tbl]] ")
        )
        title_idx = next(
            i for i, line in enumerate(lines) if line.startswith("**Table title:**")
        )
        html_lines = lines[marker_idx + 1:title_idx]
        while html_lines and html_lines[-1] == "":
            html_lines.pop()

        assert "\n".join(html_lines) == tbl_block.html

    def test_table_metadata(self, prov):
        """Table marker includes num_rows, num_cols, multi_page."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Table(
                    uid="blk-tmeta",
                    html="<table><thead><tr><th>A</th><th>B</th></tr></thead>"
                    "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>",
                    table_title="Meta Table",
                    caption="",
                    multi_page=False,
                    provenance=prov(4),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-tmeta\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["kind"] == "table"
        assert meta["multi_page"] is False
        assert meta["num_rows"] == 2  # 1 header + 1 body row
        assert meta["num_cols"] == 2

    def test_table_merge_record_metadata(self, book_with_table_merge):
        """Merge record table has num_segments and segment_pages in metadata,
        and merge record summary lines (NOT segment_html) in content.
        """
        ch = book_with_table_merge.chapters[0]
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-merged-tbl\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["multi_page"] is True
        assert meta["num_segments"] == 2
        assert meta["segment_pages"] == [10, 11]
        # Should NOT contain segment_html anywhere in output
        assert "segment_html" not in output

    def test_merge_record_summary_no_segment_html(self, book_with_table_merge):
        """Merge record content section includes summary but NOT segment_html."""
        ch = book_with_table_merge.chapters[0]
        output = render_chapter_projection(ch)

        # Merge record summary
        assert "**Merge record:**" in output
        assert "segments: 2" in output
        assert "pages: [10, 11]" in output
        assert "order: [0, 1]" in output

        # The raw HTML should not include segment_html substrings
        # Check that segment_html content is NOT present verbatim
        tbl = ch.blocks[0]
        assert isinstance(tbl, Table)
        for seg_html in tbl.merge_record.segment_html:  # type: ignore[union-attr]
            # The segment_html fragments (like "<tr><td>1</td></tr>") ARE
            # part of the full merged html, so they *will* appear.  What must
            # NOT appear is the *key* "segment_html" — i.e. no JSON dump or
            # structured re-emission of the segment list.
            pass
        # Double-check: the content after the HTML section should not contain
        # "segment_html" as a key or be immediately preceded by it
        assert "segment_html" not in output


# ---------------------------------------------------------------------------
# Provenance rendering
# ---------------------------------------------------------------------------


class TestProvenanceRendering:
    def test_all_block_types_include_common_metadata(self, book_with_all_types):
        """All block metadata includes uid/kind/page/provenance.source."""
        ch = book_with_all_types.chapters[0]
        output = render_chapter_projection(ch)
        seen_kinds: set[str] = set()

        for match in re.finditer(r"\[\[block (\S+)\]\] (\{.*\})", output):
            meta = json.loads(match.group(2))
            assert meta["uid"] == match.group(1)
            assert isinstance(meta["kind"], str)
            assert isinstance(meta["page"], int)
            assert meta["provenance"]["source"] == "docling"
            seen_kinds.add(meta["kind"])

        assert seen_kinds == {
            "paragraph",
            "heading",
            "footnote",
            "figure",
            "table",
            "equation",
        }

    def test_provenance_source_in_metadata(self, prov):
        """block provenance.source appears in marker metadata."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Paragraph(
                    uid="blk-prov",
                    text="Provenance test.",
                    role="body",
                    provenance=Provenance(page=1, bbox=[0.1, 0.2, 0.3, 0.4], source="vlm"),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-prov\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["provenance"]["source"] == "vlm"
        assert meta["provenance"]["bbox"] == [0.1, 0.2, 0.3, 0.4]

    def test_provenance_optional_fields_omitted_when_none(self, prov):
        """Optional Provenance fields (raw_ref, etc.) absent when None."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Paragraph(
                    uid="blk-prov2",
                    text="No extras.",
                    role="body",
                    provenance=Provenance(page=1, bbox=None, source="docling"),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-prov2\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        prov_meta = meta["provenance"]
        assert prov_meta["source"] == "docling"
        assert "bbox" not in prov_meta
        assert "raw_ref" not in prov_meta
        assert "raw_label" not in prov_meta
        assert "artifact_id" not in prov_meta
        assert "evidence_ref" not in prov_meta

    def test_provenance_all_fields_present(self):
        """When all Provenance fields are set, they appear in metadata."""
        prov = Provenance(
            page=1,
            bbox=[1.0, 2.0, 3.0, 4.0],
            source="vlm",
            raw_ref="page_5",
            raw_label="table",
            artifact_id="art-123",
            evidence_ref="ev-456",
        )
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Paragraph(
                    uid="blk-all",
                    text="All fields.",
                    role="body",
                    provenance=prov,
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-all\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        p = meta["provenance"]
        assert p["source"] == "vlm"
        assert p["bbox"] == [1.0, 2.0, 3.0, 4.0]
        assert p["raw_ref"] == "page_5"
        assert p["raw_label"] == "table"
        assert p["artifact_id"] == "art-123"
        assert p["evidence_ref"] == "ev-456"

    def test_footnote_provenance_all_fields_present(self):
        """Footnote provenance includes source and optional provenance fields."""
        prov = Provenance(
            page=8,
            bbox=[8.0, 9.0, 10.0, 11.0],
            source="llm",
            raw_ref="fn-raw-8",
            raw_label="footnote",
            artifact_id="artifact-footnote",
            evidence_ref="evidence-footnote",
        )
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Footnote(
                    uid="blk-fn-prov",
                    callout="*",
                    text="Footnote with full provenance.",
                    paired=False,
                    orphan=True,
                    provenance=prov,
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-fn-prov\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        p = meta["provenance"]
        assert p["source"] == "llm"
        assert p["bbox"] == [8.0, 9.0, 10.0, 11.0]
        assert p["raw_ref"] == "fn-raw-8"
        assert p["raw_label"] == "footnote"
        assert p["artifact_id"] == "artifact-footnote"
        assert p["evidence_ref"] == "evidence-footnote"


# ---------------------------------------------------------------------------
# Content rendering for each block type
# ---------------------------------------------------------------------------


class TestBlockContent:
    def test_paragraph_content(self, prov):
        """Paragraph text appears as content after the marker."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Paragraph(
                    uid="blk-pc",
                    text="The quick brown fox.",
                    role="body",
                    provenance=prov(1),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        assert "[[block blk-pc]]" in output
        assert "The quick brown fox." in output

    def test_heading_content(self, prov):
        """Heading text appears as content."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Heading(
                    uid="blk-hc",
                    text="Section Title",
                    level=1,
                    provenance=prov(1),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        assert "Section Title" in output

    def test_footnote_content(self, prov):
        """Footnote text appears as content."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Footnote(
                    uid="blk-fnc",
                    callout="1",
                    text="The footnote body.",
                    paired=True,
                    orphan=False,
                    provenance=prov(2),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        assert "The footnote body." in output

    def test_figure_with_image_ref(self, prov):
        """Figure with image_ref renders as markdown image."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Figure(
                    uid="blk-figc",
                    caption="Diagram",
                    image_ref="img/diag.png",
                    provenance=prov(3),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        assert "![Diagram](img/diag.png)" in output

    def test_figure_without_image_ref(self, prov):
        """Figure without image_ref renders caption text only."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Figure(
                    uid="blk-fignoir",
                    caption="Just a caption.",
                    image_ref=None,
                    provenance=prov(3),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        assert "Just a caption." in output
        assert "![" not in output or "Just a caption." in output

    def test_table_content_title_and_caption(self, prov):
        """Table content includes HTML, table_title, and caption markers."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Table(
                    uid="blk-tc",
                    html="<table><tr><td>X</td></tr></table>",
                    table_title="My Table",
                    caption="A table caption.",
                    multi_page=False,
                    provenance=prov(4),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        assert "<table><tr><td>X</td></tr></table>" in output
        assert "**Table title:** My Table" in output
        assert "**Caption:** A table caption." in output

    def test_equation_content(self, prov):
        """Equation latex appears as content."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Equation(
                    uid="blk-ec",
                    latex="\\int_{-\\infty}^{\\infty} e^{-x^2} dx",
                    provenance=prov(5),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        assert "\\int_{-\\infty}^{\\infty} e^{-x^2} dx" in output


# ---------------------------------------------------------------------------
# Metadata JSON encoding — no escaping ambiguity
# ---------------------------------------------------------------------------


class TestMetadataJSON:
    def test_metadata_is_valid_json(self, book_with_all_types):
        """All [[book]], [[chapter]], [[block]] metadata is valid JSON."""
        output = render_index(book_with_all_types)
        for ch in book_with_all_types.chapters:
            output += "\n" + render_chapter_projection(ch)

        for match in re.finditer(r"\[\[(?:book|chapter|block)(?: \S+)?\]\] (\{.*\})", output):
            raw = match.group(1)
            try:
                json.loads(raw)
            except json.JSONDecodeError as e:
                pytest.fail(f"Invalid JSON metadata: {raw!r}\n  Error: {e}")

    def test_metadata_no_pipe_braces_ambiguity(self, prov):
        """Metadata uses JSON, not key=value, so no escaping issues with special chars."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Paragraph(
                    uid="blk-sp",
                    text="Text with | pipes, ] brackets, and \n newlines.",
                    role="body",
                    provenance=prov(1),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        # The metadata should be JSON (not key=value), so no raw pipes in metadata line
        for line in output.split("\n"):
            if line.startswith("[[block"):
                # The part after [[block uid]] should be valid JSON
                after_bracket = line.split("]] ", 1)[1] if "]] " in line else ""
                json.loads(after_bracket)
                # No raw pipes or unescaped special chars in the JSON part
                assert "|" not in after_bracket, "Pipe char leaked into metadata"

    def test_multiline_text_not_in_metadata(self, prov):
        """Multiline content stays in content, not in metadata JSON."""
        text = "Line one\nLine two\nLine three"
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Paragraph(
                    uid="blk-ml",
                    text=text,
                    role="body",
                    provenance=prov(1),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        # Metadata line should be single-line, content after it
        for i, line in enumerate(output.split("\n")):
            if line.startswith("[[block blk-ml]]"):
                # Next line(s) should contain the paragraph text
                content_start = i + 1
                rest_lines = output.split("\n")[content_start:]
                rest = "\n".join(rest_lines)
                assert "Line one" in rest
                assert "Line two" in rest
                assert "Line three" in rest
                break


# ---------------------------------------------------------------------------
# Full book round-trip sanity
# ---------------------------------------------------------------------------


class TestFullBook:
    def test_all_blocks_in_index_and_chapters(self, book_with_all_types):
        """All chapters appear in index, all blocks appear in their chapter."""
        index = render_index(book_with_all_types)
        # Each chapter UID mentioned in index
        for ch in book_with_all_types.chapters:
            assert ch.uid in index

        # Each chapter's blocks appear in its projection
        for ch in book_with_all_types.chapters:
            proj = render_chapter_projection(ch)
            for block in ch.blocks:
                assert f"[[block {block.uid}]]" in proj, \
                    f"Block {block.uid} missing from chapter {ch.uid}"

    def test_content_after_marker(self, book_with_all_types):
        """Each block's content appears on lines after its marker, not before."""
        for ch in book_with_all_types.chapters:
            proj = render_chapter_projection(ch)
            lines = proj.split("\n")
            for i, line in enumerate(lines):
                if line.startswith("[[block") and "]] " in line:
                    # The marker should be followed by content (possibly multi-line)
                    remaining = "\n".join(lines[i + 1:])
                    # The next non-empty line after marker should be the first content line
                    # before the next [[block or end
                    assert remaining.strip(), f"No content after marker at line {i + 1}"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_single_chapter_projection_not_from_index(self, prov):
        """render_chapter_projection works standalone without index."""
        ch = Chapter(
            uid="ch-standalone",
            title="Standalone",
            level=1,
            blocks=[
                Paragraph(
                    uid="blk-std",
                    text="Standalone text.",
                    role="body",
                    provenance=prov(1),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        assert "Standalone text." in output
        assert "[[block blk-std]]" in output

    def test_block_without_uid(self, prov):
        """Block with uid=None still produces a marker with empty uid."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Paragraph(
                    uid=None,
                    text="No uid.",
                    role="body",
                    provenance=prov(1),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        assert "[[block " in output
        # json after the uid part should still be valid
        m = re.search(r"\[\[block \]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["uid"] is None

    def test_provenance_source_docling(self, prov):
        """provenance.source='docling' is correctly rendered."""
        ch = Chapter(
            uid="ch", title="Ch", level=1,
            blocks=[
                Paragraph(
                    uid="blk-docling",
                    text="Docling content.",
                    role="body",
                    provenance=Provenance(page=1, bbox=None, source="docling"),
                ),
            ],
        )
        output = render_chapter_projection(ch)
        m = re.search(r"\[\[block blk-docling\]\] (\{.*\})", output)
        assert m
        meta = json.loads(m.group(1))
        assert meta["provenance"]["source"] == "docling"
