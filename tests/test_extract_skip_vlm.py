"""Tests for extract_skip_vlm.py — skip-VLM Docling evidence-draft extractor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers: build minimal DoclingDocument fixtures
# ---------------------------------------------------------------------------


_BASE_DOC: dict[str, Any] = {
    "schema_name": "DoclingDocument",
    "version": "1.3.0",
    "name": "test",
    "origin": None,
    "furniture": {
        "self_ref": "#/furniture",
        "parent": None,
        "children": [],
        "content_layer": "furniture",
        "name": "_root_",
        "label": "unspecified",
    },
    "body": {
        "self_ref": "#/body",
        "parent": None,
        "children": [],
        "content_layer": "body",
        "name": "_root_",
        "label": "unspecified",
    },
    "groups": [],
    "texts": [],
    "tables": [],
    "pictures": [],
    "key_value_items": [],
    "form_items": [],
    "field_items": [],
    "field_regions": [],
    "pages": {},
}


def _page_entry(page_no: int) -> dict[str, Any]:
    return {"size": {"width": 612, "height": 792}, "image": None, "page_no": page_no}


def _prov(
    page_no: int,
    left: float = 10,
    top: float = 10,
    right: float = 100,
    bottom: float = 20,
) -> dict[str, Any]:
    return {
        "page_no": page_no,
        "bbox": {
            "l": left,
            "t": top,
            "r": right,
            "b": bottom,
            "coord_origin": "BOTTOMLEFT",
        },
        "charspan": [0, 5],
    }


def _text_item(
    ref: str,
    label: str,
    text: str,
    page_no: int,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "self_ref": ref,
        "parent": {"cref": "#/body"},
        "children": [],
        "content_layer": "body",
        "label": label,
        "prov": [_prov(page_no)],
        "orig": text,
        "text": text,
    }
    if extra:
        item.update(extra)
    return item


def _table_item(ref: str, page_no: int) -> dict[str, Any]:
    return {
        "self_ref": ref,
        "parent": {"cref": "#/body"},
        "children": [],
        "content_layer": "body",
        "label": "table",
        "prov": [_prov(page_no)],
        "captions": [],
        "references": [],
        "footnotes": [],
        "image": None,
        "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
        "annotations": [],
    }


def _picture_item(ref: str, page_no: int, label: str = "picture") -> dict[str, Any]:
    return {
        "self_ref": ref,
        "parent": {"cref": "#/body"},
        "children": [],
        "content_layer": "body",
        "label": label,
        "prov": [_prov(page_no)],
        "captions": [],
        "references": [],
        "footnotes": [],
        "image": None,
        "annotations": [],
    }


def _make_doc(
    texts: list[dict[str, Any]] | None = None,
    tables: list[dict[str, Any]] | None = None,
    pictures: list[dict[str, Any]] | None = None,
    pages: list[int] | None = None,
) -> dict[str, Any]:
    """Build a minimal DoclingDocument dict."""
    import copy

    data = copy.deepcopy(_BASE_DOC)
    all_items = list(texts or []) + list(tables or []) + list(pictures or [])

    data["body"]["children"] = [{"cref": item["self_ref"]} for item in all_items]
    data["texts"] = texts or []
    data["tables"] = tables or []
    data["pictures"] = pictures or []

    for pno in pages or []:
        data["pages"][str(pno)] = _page_entry(pno)

    return data


def _make_pages_json(pages: list[dict[str, Any]]) -> str:
    """Build pages JSON as would appear in 02_pages.json."""
    return json.dumps({"pages": pages})


def _write_inputs(
    tmp_path: Path,
    doc_data: dict[str, Any],
    pages: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    """Write 01_raw.json and 02_pages.json; return (raw_path, pages_path, out_dir)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw_path = tmp_path / "01_raw.json"
    raw_path.write_text(json.dumps(doc_data), encoding="utf-8")

    pages_path = tmp_path / "02_pages.json"
    pages_path.write_text(_make_pages_json(pages), encoding="utf-8")

    out_dir = tmp_path / "artifact_test01"
    return raw_path, pages_path, out_dir


# ---------------------------------------------------------------------------
# Minimal smoke test
# ---------------------------------------------------------------------------


def test_smoke_single_text_page(tmp_path: Path) -> None:
    """Basic extract_skip_vlm call produces unit file and sidecars."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[_text_item("#/texts/0", "text", "Hello world", page_no=1)],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )

    result = extract_skip_vlm(raw_path, pages_path, out_dir)

    assert result.mode == "skip_vlm"
    assert result.selected_pages == [1]
    assert len(result.unit_files) == 1
    assert result.unit_files[0].exists()
    assert result.audit_notes_path.exists()
    assert result.book_memory_path.exists()
    assert result.evidence_index_path.exists()


# ---------------------------------------------------------------------------
# Label family mapping tests
# ---------------------------------------------------------------------------


def _run_single_item(
    tmp_path: Path,
    label: str,
    text: str = "sample",
    *,
    extra_text_fields: dict[str, Any] | None = None,
    picture: bool = False,
    table: bool = False,
) -> dict[str, Any]:
    """Helper: extract a single item and return the first draft_block (or {})."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    if table:
        items_tables = [_table_item("#/tables/0", page_no=1)]
        items_texts: list[dict[str, Any]] = []
        items_pics: list[dict[str, Any]] = []
    elif picture:
        items_pics = [_picture_item("#/pictures/0", page_no=1, label=label)]
        items_texts = []
        items_tables = []
    else:
        items_texts = [
            _text_item("#/texts/0", label, text, page_no=1, extra=extra_text_fields)
        ]
        items_pics = []
        items_tables = []

    doc_data = _make_doc(
        texts=items_texts,
        tables=items_tables,
        pictures=items_pics,
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    unit_data = json.loads(result.unit_files[0].read_text(encoding="utf-8"))
    blocks = unit_data["draft_blocks"]
    return blocks[0] if blocks else {}


@pytest.mark.parametrize("label", ["text", "paragraph", "reference"])
def test_body_labels_produce_body_paragraph(tmp_path: Path, label: str) -> None:
    block = _run_single_item(tmp_path / label, label, "body text")
    assert block["kind"] == "paragraph"
    assert block["role"] == "body"
    assert block["text"] == "body text"


def test_title_produces_title_candidate_not_heading(tmp_path: Path) -> None:
    block = _run_single_item(tmp_path, "title", "My Title")
    assert block["kind"] == "paragraph"
    assert block["role"] == "docling_title_candidate"
    # Must NOT be a heading
    assert block.get("level") is None  # no level field on Paragraph


def test_section_header_produces_heading_candidate_not_heading(tmp_path: Path) -> None:
    block = _run_single_item(tmp_path, "section_header", "Chapter 1")
    assert block["kind"] == "paragraph"
    assert block["role"] == "docling_heading_candidate"
    assert block.get("level") is None


def test_footnote_text_produces_footnote_candidate_not_footnote_ir(
    tmp_path: Path,
) -> None:
    """Footnote-looking text should produce a Paragraph, not Footnote IR."""
    block = _run_single_item(tmp_path, "footnote", "①This is a footnote")
    assert block["kind"] == "paragraph"
    assert block["role"] == "docling_footnote_candidate"
    # NOT a Footnote IR — those have 'callout' field
    assert "callout" not in block


def test_footnote_looking_text_does_not_produce_footnote_ir(tmp_path: Path) -> None:
    """Even footnote-symbol text with PARAGRAPH label → body paragraph, not Footnote."""
    block = _run_single_item(tmp_path / "para", "paragraph", "①blah")
    assert block["kind"] == "paragraph"
    assert block["role"] == "body"
    assert "callout" not in block


def test_list_item_produces_list_item_candidate(tmp_path: Path) -> None:
    block = _run_single_item(
        tmp_path,
        "list_item",
        "Item text",
        extra_text_fields={"enumerated": False, "marker": "•"},
    )
    assert block["kind"] == "paragraph"
    assert block["role"] == "docling_list_item_candidate"


def test_caption_produces_caption_candidate(tmp_path: Path) -> None:
    block = _run_single_item(tmp_path, "caption", "Figure caption text")
    assert block["kind"] == "paragraph"
    assert block["role"] == "docling_caption_candidate"


def test_code_produces_code_paragraph(tmp_path: Path) -> None:
    block = _run_single_item(tmp_path, "code", "print('hello')")
    assert block["kind"] == "paragraph"
    assert block["role"] == "code"


def test_handwritten_text_produces_candidate_and_warning(tmp_path: Path) -> None:
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[
            _text_item("#/texts/0", "handwritten_text", "handwritten note", page_no=1)
        ],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    unit_data = json.loads(result.unit_files[0].read_text(encoding="utf-8"))
    blocks = unit_data["draft_blocks"]

    assert len(blocks) == 1
    assert blocks[0]["role"] == "docling_handwritten_candidate"
    assert len(result.warnings) == 1
    assert "handwritten" in result.warnings[0].message.lower()


def test_field_heading_produces_field_candidate_when_text_present(
    tmp_path: Path,
) -> None:
    block = _run_single_item(tmp_path, "field_heading", "Name:")
    assert block["kind"] == "paragraph"
    assert block["role"] == "docling_field_candidate"


@pytest.mark.parametrize("label", ["field_key", "field_hint"])
def test_field_labels_with_text(tmp_path: Path, label: str) -> None:
    block = _run_single_item(tmp_path / label, label, "value")
    assert block["kind"] == "paragraph"
    assert block["role"] == "docling_field_candidate"


def test_field_value_without_text_produces_no_block(tmp_path: Path) -> None:
    block = _run_single_item(tmp_path, "field_value", "")
    assert block == {}


def test_checkbox_selected_with_text_produces_checkbox_candidate(
    tmp_path: Path,
) -> None:
    block = _run_single_item(tmp_path, "checkbox_selected", "Yes")
    assert block["kind"] == "paragraph"
    assert block["role"] == "docling_checkbox_candidate"


def test_checkbox_unselected_without_text_produces_no_block(tmp_path: Path) -> None:
    block = _run_single_item(tmp_path / "unsel", "checkbox_unselected", "")
    assert block == {}


def test_formula_produces_equation_not_paragraph(tmp_path: Path) -> None:
    block = _run_single_item(tmp_path, "formula", r"E=mc^2")
    assert block["kind"] == "equation"
    assert block["latex"] == r"E=mc^2"


def test_table_produces_table_block(tmp_path: Path) -> None:
    block = _run_single_item(tmp_path, "table", table=True)
    assert block["kind"] == "table"
    assert block["table_title"] == ""
    assert block["caption"] == ""
    # continuation and multi_page must be false
    assert block["continuation"] is False
    assert block["multi_page"] is False


def test_adjacent_same_shape_tables_do_not_set_continuation(tmp_path: Path) -> None:
    """Two table blocks on consecutive pages must not set continuation=True."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        tables=[
            _table_item("#/tables/0", page_no=1),
            _table_item("#/tables/1", page_no=2),
        ],
        pages=[1, 2],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}, {"page": 2, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)

    for unit_path in result.unit_files:
        unit_data = json.loads(unit_path.read_text(encoding="utf-8"))
        for block in unit_data["draft_blocks"]:
            if block["kind"] == "table":
                assert block["continuation"] is False, (
                    "Table should never set continuation in skip-VLM"
                )


def test_picture_produces_figure_with_mechanical_image_ref(tmp_path: Path) -> None:
    block = _run_single_item(tmp_path, "picture", picture=True)
    assert block["kind"] == "figure"
    assert block["image_ref"] == "p0001_pictures_0.png"


def test_chart_produces_figure_with_mechanical_image_ref(tmp_path: Path) -> None:
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        pictures=[_picture_item("#/pictures/0", page_no=1, label="chart")],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    unit_data = json.loads(result.unit_files[0].read_text(encoding="utf-8"))
    blocks = unit_data["draft_blocks"]

    assert len(blocks) == 1
    assert blocks[0]["kind"] == "figure"
    assert blocks[0]["image_ref"] == "p0001_pictures_0.png"


@pytest.mark.parametrize("label", ["page_header", "page_footer"])
def test_page_header_footer_produce_no_draft_block(tmp_path: Path, label: str) -> None:
    """PAGE_HEADER and PAGE_FOOTER are evidence only."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[_text_item("#/texts/0", label, "My Book p.1", page_no=1)],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    unit_data = json.loads(result.unit_files[0].read_text(encoding="utf-8"))
    # No draft blocks from evidence-only labels
    assert unit_data["draft_blocks"] == []
    # But evidence_refs must still include the item
    assert "#/texts/0" in unit_data["evidence_refs"]


def test_marker_with_text_produces_candidate_paragraph(tmp_path: Path) -> None:
    block = _run_single_item(tmp_path, "marker", "* footnote marker")
    assert block["kind"] == "paragraph"
    assert block["role"] == "docling_unknown_candidate"


def test_marker_without_text_produces_no_block(tmp_path: Path) -> None:
    block = _run_single_item(tmp_path / "notext", "marker", "")
    assert block == {}


def test_empty_value_produces_no_block_but_in_evidence(tmp_path: Path) -> None:
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[_text_item("#/texts/0", "empty_value", "", page_no=1)],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    unit_data = json.loads(result.unit_files[0].read_text(encoding="utf-8"))
    assert unit_data["draft_blocks"] == []
    assert "#/texts/0" in unit_data["evidence_refs"]


# ---------------------------------------------------------------------------
# Provenance field tests
# ---------------------------------------------------------------------------


def test_provenance_fields_written(tmp_path: Path) -> None:
    """All required provenance fields must be present on every draft block."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[_text_item("#/texts/0", "text", "hello", page_no=1)],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    unit_data = json.loads(result.unit_files[0].read_text(encoding="utf-8"))
    block = unit_data["draft_blocks"][0]
    prov = block["provenance"]

    assert prov["source"] == "docling"
    assert prov["raw_ref"] == "#/texts/0"
    assert prov["raw_label"] == "text"
    assert prov["artifact_id"] == out_dir.name
    assert prov["evidence_ref"] == "#/texts/0"
    assert prov["page"] == 1
    # bbox should be present (we set one in _prov())
    assert prov["bbox"] is not None
    assert len(prov["bbox"]) == 4


# ---------------------------------------------------------------------------
# Cross-page continuation must NOT be set
# ---------------------------------------------------------------------------


def test_missing_punctuation_does_not_set_cross_page_continuation(
    tmp_path: Path,
) -> None:
    """Paragraph blocks should never have cross_page=True from skip-VLM extractor."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    # Two pages where first paragraph ends without punctuation
    doc_data = _make_doc(
        texts=[
            _text_item("#/texts/0", "text", "no punctuation at end", page_no=1),
            _text_item("#/texts/1", "text", "continues here", page_no=2),
        ],
        pages=[1, 2],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}, {"page": 2, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)

    for unit_path in result.unit_files:
        unit_data = json.loads(unit_path.read_text(encoding="utf-8"))
        for block in unit_data["draft_blocks"]:
            if block["kind"] == "paragraph":
                assert block.get("cross_page", False) is False


# ---------------------------------------------------------------------------
# Candidate edges tests
# ---------------------------------------------------------------------------


def test_candidate_edges_physical_adjacent_pages(tmp_path: Path) -> None:
    """Candidate edges must reference physically adjacent selected pages."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[
            _text_item("#/texts/0", "text", "p1 text", page_no=1),
            _text_item("#/texts/1", "text", "p2 text", page_no=2),
            _text_item("#/texts/2", "text", "p3 text", page_no=3),
        ],
        pages=[1, 2, 3],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [
            {"page": 1, "kind": "complex"},
            {"page": 2, "kind": "complex"},
            {"page": 3, "kind": "complex"},
        ],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)

    units_by_page: dict[int, dict] = {}
    for unit_path in result.unit_files:
        data = json.loads(unit_path.read_text(encoding="utf-8"))
        pno = data["unit"]["pages"][0]
        units_by_page[pno] = data

    # page 1: prev=None, next=2
    edges1 = units_by_page[1]["candidate_edges"]
    assert "previous_selected_page" not in edges1
    assert edges1.get("next_selected_page") == 2

    # page 2: prev=1, next=3
    edges2 = units_by_page[2]["candidate_edges"]
    assert edges2.get("previous_selected_page") == 1
    assert edges2.get("next_selected_page") == 3

    # page 3: prev=2, next=None
    edges3 = units_by_page[3]["candidate_edges"]
    assert edges3.get("previous_selected_page") == 2
    assert "next_selected_page" not in edges3


def test_candidate_edges_respect_page_filter_gaps(tmp_path: Path) -> None:
    """Candidate edges should not cross page_filter gaps."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    # pages 1, 2, 3, 4 in raw doc but filter only 1 and 4
    doc_data = _make_doc(
        texts=[
            _text_item("#/texts/0", "text", "p1 text", page_no=1),
            _text_item("#/texts/1", "text", "p4 text", page_no=4),
        ],
        pages=[1, 2, 3, 4],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [
            {"page": 1, "kind": "complex"},
            {"page": 2, "kind": "complex"},
            {"page": 3, "kind": "complex"},
            {"page": 4, "kind": "complex"},
        ],
    )
    # Apply page_filter: only pages 1 and 4
    result = extract_skip_vlm(raw_path, pages_path, out_dir, page_filter={1, 4})

    assert result.selected_pages == [1, 4]

    units_by_page: dict[int, dict] = {}
    for unit_path in result.unit_files:
        data = json.loads(unit_path.read_text(encoding="utf-8"))
        pno = data["unit"]["pages"][0]
        units_by_page[pno] = data

    # page 1 should NOT have next=4 because 4 is not adjacent (gap at 2, 3)
    edges1 = units_by_page[1]["candidate_edges"]
    assert "next_selected_page" not in edges1

    # page 4 should NOT have prev=1
    edges4 = units_by_page[4]["candidate_edges"]
    assert "previous_selected_page" not in edges4


def test_toc_pages_excluded_from_selected(tmp_path: Path) -> None:
    """TOC pages must not appear in selected_pages output."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    # page 2 is classified as TOC in 02_pages.json — it should be excluded
    doc_data = _make_doc(
        texts=[
            _text_item("#/texts/0", "text", "content", page_no=1),
            _text_item("#/texts/1", "text", "TOC item text", page_no=2),
        ],
        pages=[1, 2],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}, {"page": 2, "kind": "toc"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)

    assert 2 not in result.selected_pages
    assert 2 in result.toc_pages
    assert len(result.unit_files) == 1


def test_candidate_edges_leading_trailing_refs(tmp_path: Path) -> None:
    """Leading and trailing item refs must contain first/last items."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    texts = [
        _text_item(f"#/texts/{i}", "text", f"text {i}", page_no=1) for i in range(6)
    ]
    doc_data = _make_doc(texts=texts, pages=[1])
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    unit_data = json.loads(result.unit_files[0].read_text(encoding="utf-8"))
    edges = unit_data["candidate_edges"]

    assert "#/texts/0" in edges["leading_item_refs"]
    assert "#/texts/5" in edges["trailing_item_refs"]
    # Should not exceed 3 items each
    assert len(edges["leading_item_refs"]) <= 3
    assert len(edges["trailing_item_refs"]) <= 3


# ---------------------------------------------------------------------------
# Evidence index tests
# ---------------------------------------------------------------------------


def test_evidence_index_covers_all_items(tmp_path: Path) -> None:
    """Evidence index refs must include all item refs present on selected pages."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[
            _text_item("#/texts/0", "text", "hello", page_no=1),
            _text_item("#/texts/1", "section_header", "Intro", page_no=1),
        ],
        tables=[_table_item("#/tables/0", page_no=1)],
        pictures=[_picture_item("#/pictures/0", page_no=1)],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    ei = json.loads(result.evidence_index_path.read_text(encoding="utf-8"))

    assert "#/texts/0" in ei["refs"]
    assert "#/texts/1" in ei["refs"]
    assert "#/tables/0" in ei["refs"]
    assert "#/pictures/0" in ei["refs"]


def test_evidence_index_schema_and_mode(tmp_path: Path) -> None:
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[_text_item("#/texts/0", "text", "hello", page_no=1)],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    ei = json.loads(result.evidence_index_path.read_text(encoding="utf-8"))

    assert ei["schema_version"] == 3
    assert ei["mode"] == "skip_vlm"
    assert ei["artifact_id"] == out_dir.name


def test_evidence_index_only_selected_pages(tmp_path: Path) -> None:
    """Evidence index must not include TOC pages."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[
            _text_item("#/texts/0", "text", "content", page_no=1),
            _text_item("#/texts/1", "text", "toc", page_no=2),
        ],
        pages=[1, 2],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}, {"page": 2, "kind": "toc"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    ei = json.loads(result.evidence_index_path.read_text(encoding="utf-8"))

    # TOC page should not be in evidence index
    assert "2" not in ei["pages"]
    assert "1" in ei["pages"]


# ---------------------------------------------------------------------------
# Sidecar tests
# ---------------------------------------------------------------------------


def test_sidecars_always_written(tmp_path: Path) -> None:
    """audit_notes.json, book_memory.json, evidence_index.json must all exist."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[_text_item("#/texts/0", "text", "hello", page_no=1)],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)

    assert result.audit_notes_path.exists()
    assert result.book_memory_path.exists()
    assert result.evidence_index_path.exists()


def test_book_memory_is_empty(tmp_path: Path) -> None:
    """skip-VLM extractor must write an empty BookMemory."""
    from epubforge.extract_skip_vlm import extract_skip_vlm
    from epubforge.ir.book_memory import BookMemory

    doc_data = _make_doc(
        texts=[_text_item("#/texts/0", "text", "hello", page_no=1)],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    mem = BookMemory.model_validate_json(
        result.book_memory_path.read_text(encoding="utf-8")
    )
    # BookMemory should be essentially empty (default-constructed)
    empty = BookMemory()
    assert mem.model_dump() == empty.model_dump()


# ---------------------------------------------------------------------------
# Unit schema tests
# ---------------------------------------------------------------------------


def test_unit_schema_shape(tmp_path: Path) -> None:
    """Unit files must have the required keys and values."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[_text_item("#/texts/0", "text", "hello", page_no=5)],
        pages=[5],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 5, "kind": "simple"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    unit_data = json.loads(result.unit_files[0].read_text(encoding="utf-8"))

    u = unit_data["unit"]
    assert u["kind"] == "docling_page"
    assert u["pages"] == [5]
    assert u["page_kinds"] == ["simple"]
    assert u["extractor"] == "skip_vlm"
    assert u["contract_version"] == 3

    assert "draft_blocks" in unit_data
    assert "evidence_refs" in unit_data
    assert "candidate_edges" in unit_data
    assert "audit_notes" in unit_data


# ---------------------------------------------------------------------------
# RefItem resolve → evidence only
# ---------------------------------------------------------------------------


def test_explicit_docling_refitem_refs_go_to_evidence_only(tmp_path: Path) -> None:
    """Caption/footnote/reference refs on table items must be in evidence data, not blocks."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    # Build a doc with a table that has a caption reference to a text item
    import copy

    doc_data = copy.deepcopy(_BASE_DOC)
    doc_data["pages"]["1"] = _page_entry(1)
    doc_data["body"]["children"] = [
        {"cref": "#/texts/0"},
        {"cref": "#/tables/0"},
    ]
    doc_data["texts"] = [
        _text_item("#/texts/0", "caption", "Table caption text", page_no=1),
    ]
    doc_data["tables"] = [
        {
            "self_ref": "#/tables/0",
            "parent": {"cref": "#/body"},
            "children": [],
            "content_layer": "body",
            "label": "table",
            "prov": [_prov(1)],
            "captions": [{"cref": "#/texts/0"}],
            "references": [],
            "footnotes": [],
            "image": None,
            "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
            "annotations": [],
        }
    ]

    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    ei = json.loads(result.evidence_index_path.read_text(encoding="utf-8"))

    # The table entry in evidence should have caption_refs
    table_entry = None
    for item_entry in ei["pages"]["1"]["items"]:
        if item_entry["ref"] == "#/tables/0":
            table_entry = item_entry
            break

    assert table_entry is not None
    assert "#/texts/0" in table_entry["caption_refs"]


# ---------------------------------------------------------------------------
# Force flag / reuse test
# ---------------------------------------------------------------------------


def test_unit_files_reused_when_force_false(tmp_path: Path) -> None:
    """Existing unit files must be reused when force=False."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[_text_item("#/texts/0", "text", "hello", page_no=1)],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )

    # First run
    result1 = extract_skip_vlm(raw_path, pages_path, out_dir)
    mtime1 = result1.unit_files[0].stat().st_mtime

    # Second run without force
    result2 = extract_skip_vlm(raw_path, pages_path, out_dir, force=False)
    mtime2 = result2.unit_files[0].stat().st_mtime

    assert mtime1 == mtime2, "Unit file should not be rewritten when force=False"


def test_unit_files_overwritten_when_force_true(tmp_path: Path) -> None:
    """Existing unit files must be overwritten when force=True."""
    from epubforge.extract_skip_vlm import extract_skip_vlm
    import time

    doc_data = _make_doc(
        texts=[_text_item("#/texts/0", "text", "hello", page_no=1)],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )

    # First run
    result1 = extract_skip_vlm(raw_path, pages_path, out_dir)
    mtime1 = result1.unit_files[0].stat().st_mtime

    # Ensure filesystem time advances
    time.sleep(0.01)

    # Second run with force
    result2 = extract_skip_vlm(raw_path, pages_path, out_dir, force=True)
    mtime2 = result2.unit_files[0].stat().st_mtime

    assert mtime2 > mtime1, "Unit file should be rewritten when force=True"


# ---------------------------------------------------------------------------
# Evidence refs in unit
# ---------------------------------------------------------------------------


def test_table_export_failure_produces_warning_and_audit_note(tmp_path: Path) -> None:
    """When table.export_to_html raises, a Stage3Warning and audit_note must be emitted."""
    from unittest.mock import patch
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        tables=[_table_item("#/tables/0", page_no=1)],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )

    with patch(
        "docling_core.types.doc.TableItem.export_to_html",
        side_effect=RuntimeError("mock failure"),
    ):
        result = extract_skip_vlm(raw_path, pages_path, out_dir)

    # Must produce exactly one warning
    assert len(result.warnings) == 1
    assert "table" in result.warnings[0].message.lower()
    assert result.warnings[0].page == 1
    assert result.warnings[0].item_ref == "#/tables/0"

    # The unit file must contain the table block with the placeholder HTML
    unit_data = json.loads(result.unit_files[0].read_text(encoding="utf-8"))
    table_blocks = [b for b in unit_data["draft_blocks"] if b["kind"] == "table"]
    assert len(table_blocks) == 1
    assert table_blocks[0]["html"] == "<!-- table export failed -->"

    # audit_notes must contain the failure note
    audit_notes = unit_data["audit_notes"]
    failure_notes = [n for n in audit_notes if n.get("hint") == "table_export_failed"]
    assert len(failure_notes) == 1
    assert failure_notes[0]["page"] == 1


def test_evidence_refs_include_all_page_items(tmp_path: Path) -> None:
    """evidence_refs in each unit must include refs for all items on that page."""
    from epubforge.extract_skip_vlm import extract_skip_vlm

    doc_data = _make_doc(
        texts=[
            _text_item("#/texts/0", "text", "first", page_no=1),
            _text_item("#/texts/1", "page_header", "Header", page_no=1),
        ],
        pages=[1],
    )
    raw_path, pages_path, out_dir = _write_inputs(
        tmp_path,
        doc_data,
        [{"page": 1, "kind": "complex"}],
    )
    result = extract_skip_vlm(raw_path, pages_path, out_dir)
    unit_data = json.loads(result.unit_files[0].read_text(encoding="utf-8"))

    # Both items (body text AND page_header) should be in evidence_refs
    assert "#/texts/0" in unit_data["evidence_refs"]
    assert "#/texts/1" in unit_data["evidence_refs"]
