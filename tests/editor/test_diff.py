"""Tests for the Phase 6C Book diff field and replacement semantics."""

from __future__ import annotations

import pytest

from epubforge.editor import DiffError as PublicDiffError
from epubforge.editor import diff_books as public_diff_books
from epubforge.editor.diff import DiffError, diff_books
from epubforge.editor.patches import (
    BookPatch,
    DeleteNodeChange,
    InsertNodeChange,
    MoveNodeChange,
    ReplaceNodeChange,
    SetFieldChange,
    apply_book_patch,
    validate_book_patch,
)
from epubforge.ir.semantic import (
    Block,
    Book,
    Chapter,
    Figure,
    Footnote,
    Heading,
    Paragraph,
    Provenance,
    Table,
    TableMergeRecord,
)


def _prov(page: int = 1) -> Provenance:
    return Provenance(page=page, bbox=None, source="passthrough")


def _paragraph(uid: str | None = "blk-1", text: str = "Hello") -> Paragraph:
    return Paragraph(uid=uid, text=text, role="body", provenance=_prov())


def _heading(
    uid: str | None = "heading-1",
    text: str = "Heading",
    *,
    level: int = 1,
    id: str | None = None,
) -> Heading:
    return Heading(uid=uid, text=text, level=level, id=id, provenance=_prov())


def _footnote(
    uid: str | None = "fn-1",
    *,
    paired: bool = False,
    orphan: bool = False,
) -> Footnote:
    return Footnote(
        uid=uid,
        callout="1",
        text="Footnote text",
        paired=paired,
        orphan=orphan,
        provenance=_prov(),
    )


def _table(
    uid: str | None = "tbl-1",
    html: str = "<table><tr><td>A</td></tr></table>",
    *,
    merge_record: TableMergeRecord | None = None,
) -> Table:
    return Table(uid=uid, html=html, provenance=_prov(), merge_record=merge_record)


def _figure(uid: str | None = "fig-1", caption: str = "Figure") -> Figure:
    return Figure(uid=uid, caption=caption, provenance=_prov())


def _chapter(
    uid: str | None = "ch-1",
    *,
    title: str = "Chapter 1",
    blocks: list[Block] | None = None,
) -> Chapter:
    return Chapter(
        uid=uid,
        title=title,
        level=1,
        blocks=[_paragraph()] if blocks is None else blocks,
    )


def _book(*, chapters: list[Chapter] | None = None, title: str = "Test Book") -> Book:
    return Book(title=title, chapters=chapters or [_chapter()])


def _assert_round_trip(base: Book, proposed: Book) -> BookPatch:
    patch = diff_books(base, proposed)
    validate_book_patch(base, patch)
    result = apply_book_patch(base, patch)
    assert result.model_dump(mode="json") == proposed.model_dump(mode="json")
    return patch


def test_identity_diff_returns_empty_patch_and_applies() -> None:
    base = _book()
    proposed = base.model_copy(deep=True)

    patch = diff_books(base, proposed)

    assert patch.agent_id == "diff-engine"
    assert patch.scope.chapter_uid is None
    assert patch.changes == []
    validate_book_patch(base, patch)
    result = apply_book_patch(base, patch)
    assert result.model_dump(mode="json") == proposed.model_dump(mode="json")


@pytest.mark.parametrize(
    ("book", "message"),
    [
        (_book(chapters=[_chapter(uid=None)]), "chapter.*uid=None"),
        (_book(chapters=[_chapter(blocks=[_paragraph(uid=None)])]), "block.*uid=None"),
        (
            _book(
                chapters=[
                    _chapter(uid="dup", blocks=[]),
                    _chapter(uid="dup", blocks=[]),
                ]
            ),
            "duplicate uid 'dup'.*chapter",
        ),
        (
            _book(
                chapters=[
                    _chapter(uid="ch-1", blocks=[_paragraph(uid="dup")]),
                    _chapter(uid="ch-2", blocks=[_paragraph(uid="dup")]),
                ]
            ),
            "duplicate uid 'dup'.*block",
        ),
        (
            _book(chapters=[_chapter(uid="collision", blocks=[_paragraph(uid="collision")])]),
            "duplicate uid 'collision'.*collides",
        ),
        (_book(chapters=[_chapter(uid="")]), "empty uid"),
    ],
)
def test_uid_validation_errors(book: Book, message: str) -> None:
    with pytest.raises(DiffError, match=message):
        diff_books(book, book.model_copy(deep=True))


def test_book_level_delta_is_unsupported() -> None:
    base = _book(title="Original")
    proposed = base.model_copy(update={"title": "Changed"}, deep=True)

    with pytest.raises(DiffError, match="Book-level.*title"):
        diff_books(base, proposed)


def test_provenance_only_delta_is_unsupported_immutable_delta() -> None:
    base = _book()
    proposed = base.model_copy(deep=True)
    proposed.chapters[0].blocks[0].provenance = _prov(page=2)

    with pytest.raises(DiffError, match="blk-1.*provenance"):
        diff_books(base, proposed)


def test_field_diff_round_trips_for_editable_chapter_and_block_fields() -> None:
    base = _book(
        chapters=[
            _chapter(
                blocks=[
                    _paragraph(uid="para-1"),
                    _heading(uid="head-1", id="old-heading"),
                    _table(uid="table-1"),
                ]
            )
        ]
    )
    proposed = base.model_copy(deep=True)

    proposed.chapters[0].title = "Changed chapter title"
    paragraph = proposed.chapters[0].blocks[0]
    assert isinstance(paragraph, Paragraph)
    paragraph.text = "Changed paragraph text"
    heading = proposed.chapters[0].blocks[1]
    assert isinstance(heading, Heading)
    heading.level = 2
    heading.id = "new-heading"
    table = proposed.chapters[0].blocks[2]
    assert isinstance(table, Table)
    table.html = "<table><tr><td>B</td></tr></table>"

    patch = _assert_round_trip(base, proposed)

    set_fields = {
        (change.target_uid, change.field)
        for change in patch.changes
        if isinstance(change, SetFieldChange)
    }
    assert set_fields == {
        ("ch-1", "title"),
        ("para-1", "text"),
        ("head-1", "id"),
        ("head-1", "level"),
        ("table-1", "html"),
    }


@pytest.mark.parametrize(
    ("field", "new_value"),
    [
        ("paired", True),
        ("orphan", True),
    ],
)
def test_footnote_single_flag_field_diff_applies(
    field: str, new_value: bool
) -> None:
    base = _book(chapters=[_chapter(blocks=[_footnote()])])
    proposed = base.model_copy(deep=True)
    footnote = proposed.chapters[0].blocks[0]
    assert isinstance(footnote, Footnote)
    setattr(footnote, field, new_value)

    patch = _assert_round_trip(base, proposed)

    assert len(patch.changes) == 1
    change = patch.changes[0]
    assert isinstance(change, SetFieldChange)
    assert change.target_uid == "fn-1"
    assert change.field == field


def test_footnote_paired_orphan_simultaneous_change_uses_replace_node() -> None:
    base = _book(chapters=[_chapter(blocks=[_footnote(paired=True, orphan=False)])])
    proposed = base.model_copy(deep=True)
    footnote = proposed.chapters[0].blocks[0]
    assert isinstance(footnote, Footnote)
    footnote.paired = False
    footnote.orphan = True

    patch = _assert_round_trip(base, proposed)

    assert len(patch.changes) == 1
    change = patch.changes[0]
    assert isinstance(change, ReplaceNodeChange)
    assert change.target_uid == "fn-1"
    assert change.new_node["kind"] == "footnote"
    assert "uid" not in change.new_node


@pytest.mark.parametrize(
    ("base_block", "proposed_block", "new_kind"),
    [
        (_paragraph(uid="replace-1"), _heading(uid="replace-1"), "heading"),
        (_table(uid="replace-1"), _figure(uid="replace-1"), "figure"),
    ],
)
def test_block_kind_change_uses_replace_node_round_trip(
    base_block: Block, proposed_block: Block, new_kind: str
) -> None:
    base = _book(chapters=[_chapter(blocks=[base_block])])
    proposed = _book(chapters=[_chapter(blocks=[proposed_block])])

    patch = _assert_round_trip(base, proposed)

    assert len(patch.changes) == 1
    change = patch.changes[0]
    assert isinstance(change, ReplaceNodeChange)
    assert change.target_uid == "replace-1"
    assert change.old_node["uid"] == "replace-1"
    assert change.new_node["kind"] == new_kind
    assert "uid" not in change.new_node


def test_table_merge_record_delta_uses_replace_node_round_trip() -> None:
    merge_record = TableMergeRecord(
        segment_html=["<tbody><tr><td>A</td></tr></tbody>"],
        segment_pages=[1],
        segment_order=[0],
        column_widths=[1],
    )
    base = _book(chapters=[_chapter(blocks=[_table(uid="table-merge")])])
    proposed = _book(
        chapters=[
            _chapter(
                blocks=[
                    _table(
                        uid="table-merge",
                        html="<table><tr><td>A</td></tr></table>",
                        merge_record=merge_record,
                    )
                ]
            )
        ]
    )

    patch = _assert_round_trip(base, proposed)

    assert len(patch.changes) == 1
    change = patch.changes[0]
    assert isinstance(change, ReplaceNodeChange)
    assert change.target_uid == "table-merge"
    assert change.new_node["kind"] == "table"
    assert "uid" not in change.new_node
    assert change.new_node["merge_record"] == merge_record.model_dump(mode="python")


@pytest.mark.parametrize(
    ("proposed_blocks", "expected_after_uid"),
    [
        (["x", "a", "c"], None),
        (["a", "x", "c"], "a"),
        (["a", "c", "x"], "c"),
    ],
)
def test_insert_block_head_middle_tail_round_trips(
    proposed_blocks: list[str], expected_after_uid: str | None
) -> None:
    base = _book(
        chapters=[_chapter(blocks=[_paragraph(uid="a"), _paragraph(uid="c")])]
    )
    block_by_uid = {
        "a": _paragraph(uid="a"),
        "c": _paragraph(uid="c"),
        "x": _paragraph(uid="x", text="Inserted"),
    }
    proposed = _book(
        chapters=[_chapter(blocks=[block_by_uid[uid] for uid in proposed_blocks])]
    )

    patch = _assert_round_trip(base, proposed)

    inserts = [change for change in patch.changes if isinstance(change, InsertNodeChange)]
    assert len(inserts) == 1
    assert inserts[0].parent_uid == "ch-1"
    assert inserts[0].after_uid == expected_after_uid
    assert inserts[0].node["uid"] == "x"


def test_delete_block_round_trips() -> None:
    base = _book(
        chapters=[
            _chapter(
                blocks=[_paragraph(uid="a"), _paragraph(uid="b"), _paragraph(uid="c")]
            )
        ]
    )
    proposed = _book(
        chapters=[_chapter(blocks=[_paragraph(uid="a"), _paragraph(uid="c")])]
    )

    patch = _assert_round_trip(base, proposed)

    deletes = [change for change in patch.changes if isinstance(change, DeleteNodeChange)]
    assert [change.target_uid for change in deletes] == ["b"]


@pytest.mark.parametrize(
    "proposed_order",
    [
        ["b", "a"],
        ["b", "a", "c"],
        ["b", "c", "a"],
    ],
)
def test_same_chapter_reorder_swap_rotate_round_trips(
    proposed_order: list[str],
) -> None:
    base_order = sorted(proposed_order)
    base = _book(
        chapters=[_chapter(blocks=[_paragraph(uid=uid) for uid in base_order])]
    )
    proposed = _book(
        chapters=[_chapter(blocks=[_paragraph(uid=uid) for uid in proposed_order])]
    )

    patch = _assert_round_trip(base, proposed)

    assert any(isinstance(change, MoveNodeChange) for change in patch.changes)


def test_cross_chapter_move_round_trips() -> None:
    base = _book(
        chapters=[
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a"), _paragraph(uid="b")]),
            _chapter(uid="ch-b", blocks=[_paragraph(uid="c")]),
        ]
    )
    proposed = _book(
        chapters=[
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a")]),
            _chapter(uid="ch-b", blocks=[_paragraph(uid="b"), _paragraph(uid="c")]),
        ]
    )

    patch = _assert_round_trip(base, proposed)

    moves = [change for change in patch.changes if isinstance(change, MoveNodeChange)]
    assert any(
        change.target_uid == "b"
        and change.from_parent_uid == "ch-a"
        and change.to_parent_uid == "ch-b"
        for change in moves
    )


def test_chapter_reorder_round_trips() -> None:
    base = _book(
        chapters=[
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a")]),
            _chapter(uid="ch-b", blocks=[_paragraph(uid="b")]),
            _chapter(uid="ch-c", blocks=[_paragraph(uid="c")]),
        ]
    )
    proposed = _book(
        chapters=[
            _chapter(uid="ch-c", blocks=[_paragraph(uid="c")]),
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a")]),
            _chapter(uid="ch-b", blocks=[_paragraph(uid="b")]),
        ]
    )

    patch = _assert_round_trip(base, proposed)

    assert isinstance(patch.changes[0], MoveNodeChange)
    assert patch.changes[0].target_uid == "ch-c"


def test_split_chapter_like_diff_round_trips() -> None:
    base = _book(
        chapters=[
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a"), _paragraph(uid="b"), _paragraph(uid="c")])
        ]
    )
    proposed = _book(
        chapters=[
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a")]),
            _chapter(uid="ch-b", blocks=[_paragraph(uid="b"), _paragraph(uid="c")]),
        ]
    )

    patch = _assert_round_trip(base, proposed)

    chapter_insert = next(
        change
        for change in patch.changes
        if isinstance(change, InsertNodeChange) and change.parent_uid is None
    )
    assert chapter_insert.node["uid"] == "ch-b"
    assert chapter_insert.node["blocks"] == []


def test_merge_chapter_like_diff_round_trips() -> None:
    base = _book(
        chapters=[
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a")]),
            _chapter(uid="ch-b", blocks=[_paragraph(uid="b"), _paragraph(uid="c")]),
        ]
    )
    proposed = _book(
        chapters=[
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a"), _paragraph(uid="b"), _paragraph(uid="c")])
        ]
    )

    patch = _assert_round_trip(base, proposed)

    assert isinstance(patch.changes[-1], DeleteNodeChange)
    assert patch.changes[-1].target_uid == "ch-b"
    assert patch.changes[-1].old_node["blocks"] == []


def test_new_chapter_mixed_existing_and_new_blocks_round_trips() -> None:
    base = _book(
        chapters=[
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a"), _paragraph(uid="b")])
        ]
    )
    proposed = _book(
        chapters=[
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a")]),
            _chapter(
                uid="ch-x",
                blocks=[
                    _paragraph(uid="x", text="New head"),
                    _paragraph(uid="b"),
                    _paragraph(uid="y", text="New tail"),
                ],
            ),
        ]
    )

    patch = _assert_round_trip(base, proposed)

    chapter_insert = next(
        change
        for change in patch.changes
        if isinstance(change, InsertNodeChange) and change.parent_uid is None
    )
    assert chapter_insert.node["blocks"] == []
    assert any(
        isinstance(change, MoveNodeChange)
        and change.target_uid == "b"
        and change.to_parent_uid == "ch-x"
        for change in patch.changes
    )


def test_delete_chapter_but_retain_subset_blocks_round_trips() -> None:
    base = _book(
        chapters=[
            _chapter(uid="ch-z", blocks=[_paragraph(uid="a"), _paragraph(uid="b"), _paragraph(uid="c")]),
            _chapter(uid="ch-keep", blocks=[_paragraph(uid="d")]),
        ]
    )
    proposed = _book(
        chapters=[_chapter(uid="ch-keep", blocks=[_paragraph(uid="d"), _paragraph(uid="b")])]
    )

    patch = _assert_round_trip(base, proposed)

    deleted_uids = [
        change.target_uid
        for change in patch.changes
        if isinstance(change, DeleteNodeChange)
    ]
    assert deleted_uids == ["a", "c", "ch-z"]


def test_pure_new_chapter_uses_empty_container_then_block_inserts() -> None:
    base = _book(
        chapters=[
            _chapter(uid="ch-b", blocks=[_paragraph(uid="b")]),
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a")]),
        ]
    )
    proposed = _book(
        chapters=[
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a")]),
            _chapter(
                uid="ch-new",
                blocks=[_paragraph(uid="x", text="X"), _paragraph(uid="y", text="Y")],
            ),
            _chapter(uid="ch-b", blocks=[_paragraph(uid="b")]),
        ]
    )

    patch = _assert_round_trip(base, proposed)

    chapter_inserts = [
        change
        for change in patch.changes
        if isinstance(change, InsertNodeChange) and change.parent_uid is None
    ]
    assert len(chapter_inserts) == 1
    assert chapter_inserts[0].node["uid"] == "ch-new"
    assert chapter_inserts[0].node["blocks"] == []
    block_insert_uids = [
        change.node["uid"]
        for change in patch.changes
        if isinstance(change, InsertNodeChange) and change.parent_uid == "ch-new"
    ]
    assert block_insert_uids == ["x", "y"]


def test_delete_non_empty_chapter_uses_block_deletes_then_empty_chapter_delete() -> None:
    base = _book(
        chapters=[
            _chapter(uid="ch-a", blocks=[_paragraph(uid="a")]),
            _chapter(uid="ch-z", blocks=[_paragraph(uid="b"), _paragraph(uid="c")]),
        ]
    )
    proposed = _book(chapters=[_chapter(uid="ch-a", blocks=[_paragraph(uid="a")])])

    patch = _assert_round_trip(base, proposed)

    delete_changes = [
        change for change in patch.changes if isinstance(change, DeleteNodeChange)
    ]
    assert [change.target_uid for change in delete_changes] == ["b", "c", "ch-z"]
    assert delete_changes[-1].old_node["blocks"] == []


def test_combined_topology_field_and_replace_round_trips() -> None:
    base = _book(
        chapters=[
            _chapter(
                uid="ch-a",
                blocks=[_paragraph(uid="a"), _paragraph(uid="b", text="Old B")],
            ),
            _chapter(uid="ch-b", blocks=[_paragraph(uid="c")]),
        ]
    )
    proposed = _book(
        chapters=[
            _chapter(
                uid="ch-b",
                title="Changed chapter",
                blocks=[_paragraph(uid="b", text="New B"), _paragraph(uid="c")],
            ),
            _chapter(
                uid="ch-a",
                blocks=[
                    _heading(uid="a", text="Promoted", level=2),
                    _paragraph(uid="x", text="Inserted X"),
                ],
            ),
        ]
    )

    patch = _assert_round_trip(base, proposed)

    assert any(isinstance(change, MoveNodeChange) for change in patch.changes)
    assert any(isinstance(change, InsertNodeChange) for change in patch.changes)
    assert any(isinstance(change, ReplaceNodeChange) for change in patch.changes)
    assert any(
        isinstance(change, SetFieldChange)
        and change.target_uid == "b"
        and change.field == "text"
        for change in patch.changes
    )


def test_public_imports_work() -> None:
    assert public_diff_books is diff_books
    assert PublicDiffError is DiffError
