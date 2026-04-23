from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from epubforge.fields import FIELD_MAP, iter_text_fields, set_text_field
from epubforge.io import load_book, resolve_book_path, save_book
from epubforge.ir.semantic import Book, Chapter, Footnote, Heading, Paragraph, Provenance, Table
from epubforge.markers import (
    has_raw_callout,
    make_fn_marker,
    replace_all_raw,
    replace_first_raw,
    replace_nth_raw,
    strip_markers,
)
from epubforge.query import find_block_by_uid, find_footnotes, find_headings, find_marker_source, find_markers


def _editable_book(prov: Callable[..., Provenance]) -> Book:
    marker = make_fn_marker(1, "①")
    return Book(
        title="Editable",
        version=3,
        initialized_at="2026-04-23T00:00:00Z",
        uid_seed="seed-1",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Intro",
                blocks=[
                    Heading(uid="h-1", text="Intro", level=1, provenance=prov(1, source="llm")),
                    Paragraph(uid="p-1", text=f"text {marker}", provenance=prov(1, source="llm")),
                    Table(uid="t-1", html="<table><td>①</td></table>", table_title="表1①", caption="caption", provenance=prov(1, source="llm")),
                    Footnote(uid="f-1", callout="①", text="note body", paired=True, provenance=prov(1, source="llm")),
                ],
            )
        ],
    )


def test_load_book_accepts_legacy_pipeline_artifact(tmp_path: Path) -> None:
    legacy = {
        "title": "Legacy",
        "chapters": [
            {
                "title": "Chapter 1",
                "blocks": [
                    {"kind": "paragraph", "text": "hello", "provenance": {"page": 1, "source": "llm"}},
                ],
            }
        ],
    }
    (tmp_path / "05_semantic.json").write_text(json.dumps(legacy), encoding="utf-8")

    book = load_book(tmp_path)

    assert book.title == "Legacy"
    assert book.op_log_version == 0
    assert book.initialized_at == ""
    assert book.uid_seed == ""
    assert book.chapters[0].uid is None
    assert book.chapters[0].blocks[0].uid is None


def test_save_book_writes_editable_artifact(prov, tmp_path: Path) -> None:
    book = _editable_book(prov)

    out_path = save_book(book, tmp_path)

    assert out_path == resolve_book_path(tmp_path, for_write=True)
    restored = load_book(tmp_path)
    assert restored.uid_seed == "seed-1"
    assert restored.chapters[0].blocks[1].uid == "p-1"


def test_save_book_rejects_missing_stable_ids(tmp_path: Path) -> None:
    book = Book(title="Legacy")

    with pytest.raises(ValueError):
        save_book(book, tmp_path)


def test_marker_helpers_ignore_existing_markers() -> None:
    marker = make_fn_marker(5, "①")
    text = f"A① B{marker} C①"

    assert has_raw_callout(text, "①")
    assert strip_markers(text) == "A① B C①"
    assert replace_first_raw(text, "①", marker) == f"A{marker} B{marker} C①"
    assert replace_nth_raw(text, "①", marker, 1) == f"A① B{marker} C{marker}"
    assert replace_all_raw(text, "①", marker) == f"A{marker} B{marker} C{marker}"


def test_fields_and_query_helpers_share_single_source(prov) -> None:
    book = _editable_book(prov)
    footnote = book.chapters[0].blocks[3]
    assert isinstance(footnote, Footnote)

    field_names = {(ref.block.uid, ref.field) for ref in iter_text_fields(book)}
    assert FIELD_MAP["table"] == ("html", "table_title", "caption")
    assert ("p-1", "text") in field_names
    assert ("t-1", "caption") in field_names

    updated = set_text_field(book.chapters[0].blocks[1], "text", "changed")
    assert isinstance(updated, Paragraph)
    assert updated.text == "changed"

    assert find_block_by_uid(book, "p-1") is not None
    assert len(find_headings(book, level=1)) == 1
    assert len(find_footnotes(book, paired=True, callout="①")) == 1
    markers = find_markers(book, page=1, callout="①")
    assert len(markers) == 1
    assert markers[0].field == "text"
    assert find_marker_source(book, footnote) is not None
