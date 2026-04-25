"""Comprehensive tests for epubforge.editor.patches (Phase 1 BookPatch system)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from epubforge.editor.patches import (
    BookPatch,
    DeleteNodeChange,
    InsertNodeChange,
    MoveNodeChange,
    PatchError,
    PatchScope,
    ReplaceNodeChange,
    SetFieldChange,
    allowed_set_fields,
    apply_book_patch,
    serialize_patch_field_value,
    validate_book_patch,
)
from epubforge.ir.semantic import (
    Book,
    Chapter,
    Footnote,
    Heading,
    Paragraph,
    Provenance,
    Table,
    TableMergeRecord,
)
from uuid import uuid4


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _prov() -> Provenance:
    return Provenance(page=1, bbox=None, source="passthrough")


@pytest.fixture
def sample_book() -> Book:
    return Book(
        title="Test Book",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter 1",
                level=1,
                blocks=[
                    Paragraph(
                        uid="blk-1", text="Hello world", role="body", provenance=_prov()
                    ),
                    Heading(uid="blk-2", text="Section A", level=1, provenance=_prov()),
                    Footnote(
                        uid="blk-3",
                        callout="1",
                        text="A footnote",
                        paired=False,
                        orphan=False,
                        provenance=_prov(),
                    ),
                ],
            ),
            Chapter(
                uid="ch-2",
                title="Chapter 2",
                level=1,
                blocks=[
                    Paragraph(
                        uid="blk-4",
                        text="Second chapter text",
                        role="body",
                        provenance=_prov(),
                    ),
                    Table(
                        uid="blk-5",
                        html="<table><tr><td>data</td></tr></table>",
                        provenance=_prov(),
                    ),
                    Paragraph(
                        uid="blk-6", text="More text", role="body", provenance=_prov()
                    ),
                ],
            ),
        ],
    )


def _make_patch(changes, scope_chapter=None, agent_id="test-agent"):
    return BookPatch(
        patch_id=str(uuid4()),
        agent_id=agent_id,
        scope=PatchScope(chapter_uid=scope_chapter),
        changes=changes,
        rationale="test patch",
    )


# ---------------------------------------------------------------------------
# 8.1 SetFieldChange tests
# ---------------------------------------------------------------------------


class TestSetFieldChange:
    def test_set_paragraph_text(self, sample_book):
        """Modify paragraph.text — success, text updated."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="text",
                    old="Hello world",
                    new="Updated text",
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        block = result.chapters[0].blocks[0]
        assert block.text == "Updated text"  # type: ignore[union-attr]

    def test_set_heading_level_valid(self, sample_book):
        """Change heading level 1→2 — success."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field", target_uid="blk-2", field="level", old=1, new=2
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        block = result.chapters[0].blocks[1]
        assert block.level == 2  # type: ignore[union-attr]

    def test_set_heading_level_invalid(self, sample_book):
        """Change heading level 1→5 — PatchError (invalid level)."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field", target_uid="blk-2", field="level", old=1, new=5
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_set_chapter_level_invalid(self, sample_book):
        """Change chapter level 1→4 — PatchError (invalid level)."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field", target_uid="ch-1", field="level", old=1, new=4
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_set_footnote_paired(self, sample_book):
        """Change footnote.paired False→True — success."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-3",
                    field="paired",
                    old=False,
                    new=True,
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        block = result.chapters[0].blocks[2]
        assert block.paired is True  # type: ignore[union-attr]

    def test_set_footnote_paired_and_orphan_conflict(self, sample_book):
        """Setting paired=True + orphan=True in same patch — PatchError (mutual exclusion)."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-3",
                    field="paired",
                    old=False,
                    new=True,
                ),
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-3",
                    field="orphan",
                    old=False,
                    new=True,
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_set_role_valid(self, sample_book):
        """Change paragraph role to 'epigraph' — success."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="role",
                    old="body",
                    new="epigraph",
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        block = result.chapters[0].blocks[0]
        assert block.role == "epigraph"  # type: ignore[union-attr]

    def test_set_role_invalid(self, sample_book):
        """Change paragraph role to 'invalid_role' — PatchError."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="role",
                    old="body",
                    new="invalid_role",
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_old_mismatch(self, sample_book):
        """Old value doesn't match current value — PatchError."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="text",
                    old="wrong value",
                    new="Updated text",
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_old_equals_new(self):
        """old == new — Pydantic ValidationError (model validator rejects no-op)."""
        with pytest.raises(ValidationError):
            SetFieldChange(
                op="set_field", target_uid="blk-1", field="text", old="same", new="same"
            )

    def test_immutable_field_uid(self, sample_book):
        """Modifying uid via set_field — PatchError."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="uid",
                    old="blk-1",
                    new="blk-new",
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_immutable_field_kind(self, sample_book):
        """Modifying kind via set_field — PatchError."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="kind",
                    old="paragraph",
                    new="heading",
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_nonexistent_field_for_kind(self, sample_book):
        """Modifying paragraph.level (not a valid paragraph field) — PatchError."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field", target_uid="blk-1", field="level", old=1, new=2
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_target_uid_not_found(self, sample_book):
        """target_uid does not exist in book — PatchError."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="nonexistent",
                    field="text",
                    old="old",
                    new="new",
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_set_chapter_title(self, sample_book):
        """Modify chapter.title — success."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="ch-1",
                    field="title",
                    old="Chapter 1",
                    new="New Title",
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        assert result.chapters[0].title == "New Title"

    def test_set_table_html(self, sample_book):
        """Modify table.html — success."""
        new_html = "<table><tr><td>updated</td></tr></table>"
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-5",
                    field="html",
                    old="<table><tr><td>data</td></tr></table>",
                    new=new_html,
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        block = result.chapters[1].blocks[1]
        assert block.html == new_html  # type: ignore[union-attr]

    def test_set_table_bbox(self, sample_book):
        """Modify table.bbox — success (table.bbox in _ALLOWED_SET_FIELD)."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-5",
                    field="bbox",
                    old=None,
                    new=[1.0, 2.0, 3.0, 4.0],
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        block = result.chapters[1].blocks[1]
        assert block.bbox == [1.0, 2.0, 3.0, 4.0]  # type: ignore[union-attr]

    def test_set_paragraph_text_rejects_int(self, sample_book):
        """Setting paragraph.text to int is rejected before it can corrupt the Book."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="text",
                    old="Hello world",
                    new=123,
                ),
            ]
        )

        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

        assert sample_book.chapters[0].blocks[0].text == "Hello world"  # type: ignore[union-attr]

    @pytest.mark.parametrize("bad_bbox", ["not-a-list", ["not-a-float"]])
    def test_set_table_bbox_rejects_invalid_types(self, sample_book, bad_bbox):
        """Setting table.bbox to non-list/non-float values is rejected."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-5",
                    field="bbox",
                    old=None,
                    new=bad_bbox,
                ),
            ]
        )

        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

        assert sample_book.chapters[1].blocks[1].bbox is None  # type: ignore[union-attr]

    def test_set_style_class_rejects_invalid_value(self, sample_book):
        """style_class uses the same validator as editor ops."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="style_class",
                    old=None,
                    new="bad class",
                ),
            ]
        )

        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

        assert sample_book.chapters[0].blocks[0].style_class is None  # type: ignore[union-attr]

    def test_empty_target_uid(self):
        """Empty target_uid — Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            SetFieldChange(
                op="set_field", target_uid="", field="text", old="a", new="b"
            )

    def test_empty_field(self):
        """Empty field — Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            SetFieldChange(
                op="set_field", target_uid="blk-1", field="", old="a", new="b"
            )


# ---------------------------------------------------------------------------
# 8.2 ReplaceNodeChange tests
# ---------------------------------------------------------------------------


class TestReplaceNodeChange:
    def test_replace_paragraph_same_kind(self, sample_book):
        """Replace paragraph with updated content, uid preserved."""
        blk = sample_book.chapters[0].blocks[0]
        old_node = blk.model_dump(mode="python")
        patch = _make_patch(
            [
                ReplaceNodeChange(
                    op="replace_node",
                    target_uid="blk-1",
                    old_node=old_node,
                    new_node={
                        "kind": "paragraph",
                        "text": "Replaced text",
                        "role": "body",
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        block = result.chapters[0].blocks[0]
        assert block.uid == "blk-1"
        assert block.kind == "paragraph"
        assert block.text == "Replaced text"  # type: ignore[union-attr]

    def test_replace_paragraph_with_heading(self, sample_book):
        """Replace paragraph with heading — kind change, uid preserved."""
        blk = sample_book.chapters[0].blocks[0]
        old_node = blk.model_dump(mode="python")
        patch = _make_patch(
            [
                ReplaceNodeChange(
                    op="replace_node",
                    target_uid="blk-1",
                    old_node=old_node,
                    new_node={
                        "kind": "heading",
                        "text": "New Heading",
                        "level": 2,
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        block = result.chapters[0].blocks[0]
        assert block.uid == "blk-1"
        assert block.kind == "heading"
        assert block.text == "New Heading"  # type: ignore[union-attr]

    def test_old_node_mismatch(self, sample_book):
        """old_node doesn't match current state — PatchError."""
        # Use a stale snapshot (wrong text)
        stale_node = sample_book.chapters[0].blocks[0].model_dump(mode="python")
        stale_node["text"] = "wrong content"
        patch = _make_patch(
            [
                ReplaceNodeChange(
                    op="replace_node",
                    target_uid="blk-1",
                    old_node=stale_node,
                    new_node={
                        "kind": "paragraph",
                        "text": "New text",
                        "role": "body",
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_new_node_has_uid(self, sample_book):
        """new_node contains uid — Pydantic ValidationError."""
        blk = sample_book.chapters[0].blocks[0]
        old_node = blk.model_dump(mode="python")
        with pytest.raises(ValidationError):
            ReplaceNodeChange(
                op="replace_node",
                target_uid="blk-1",
                old_node=old_node,
                new_node={
                    "uid": "blk-1",
                    "kind": "paragraph",
                    "text": "Text",
                    "provenance": {"page": 1, "source": "passthrough"},
                },
            )

    def test_new_node_missing_kind(self, sample_book):
        """new_node missing kind — Pydantic ValidationError."""
        blk = sample_book.chapters[0].blocks[0]
        old_node = blk.model_dump(mode="python")
        with pytest.raises(ValidationError):
            ReplaceNodeChange(
                op="replace_node",
                target_uid="blk-1",
                old_node=old_node,
                new_node={"text": "No kind"},
            )

    def test_target_not_found(self, sample_book):
        """target_uid not in book — PatchError."""
        blk = sample_book.chapters[0].blocks[0]
        old_node = blk.model_dump(mode="python")
        patch = _make_patch(
            [
                ReplaceNodeChange(
                    op="replace_node",
                    target_uid="nonexistent",
                    old_node=old_node,
                    new_node={
                        "kind": "paragraph",
                        "text": "Text",
                        "role": "body",
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_replace_chapter_rejected(self, sample_book):
        """target_uid is a chapter uid — PatchError (replace_node is blocks-only)."""
        ch = sample_book.chapters[0]
        old_node = ch.model_dump(mode="python")
        patch = _make_patch(
            [
                ReplaceNodeChange(
                    op="replace_node",
                    target_uid="ch-1",
                    old_node=old_node,
                    new_node={
                        "kind": "paragraph",
                        "text": "Should fail",
                        "provenance": {"page": 1, "source": "passthrough"},
                    },
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_replace_table_preserves_merge_record(self, sample_book):
        """Table replace_node payload round-trips merge_record."""
        table = sample_book.chapters[1].blocks[1]
        assert isinstance(table, Table)
        old_node = table.model_dump(mode="python")
        merge_record = {
            "segment_html": [
                "<tbody><tr><td>A</td></tr></tbody>",
                "<tbody><tr><td>B</td></tr></tbody>",
            ],
            "segment_pages": [1, 2],
            "segment_order": [0, 1],
            "column_widths": [1, 1],
        }
        patch = _make_patch(
            [
                ReplaceNodeChange(
                    op="replace_node",
                    target_uid="blk-5",
                    old_node=old_node,
                    new_node={
                        "kind": "table",
                        "html": "<table><tbody><tr><td>A</td></tr>"
                        "<tr><td>B</td></tr></tbody></table>",
                        "table_title": "Merged table",
                        "caption": "",
                        "continuation": False,
                        "multi_page": True,
                        "bbox": None,
                        "provenance": _prov().model_dump(mode="python"),
                        "merge_record": merge_record,
                    },
                )
            ]
        )

        result = apply_book_patch(sample_book, patch)
        updated = result.chapters[1].blocks[1]
        assert isinstance(updated, Table)
        assert updated.uid == "blk-5"
        assert updated.merge_record == TableMergeRecord.model_validate(merge_record)

    def test_replace_table_legacy_payload_without_merge_record(self, sample_book):
        """Legacy table payloads without merge_record remain valid."""
        table = sample_book.chapters[1].blocks[1]
        assert isinstance(table, Table)
        old_node = table.model_dump(mode="python")
        old_node.pop("merge_record")
        patch = _make_patch(
            [
                ReplaceNodeChange(
                    op="replace_node",
                    target_uid="blk-5",
                    old_node=old_node,
                    new_node={
                        "kind": "table",
                        "html": "<table><tr><td>legacy</td></tr></table>",
                        "provenance": _prov().model_dump(mode="python"),
                    },
                )
            ]
        )

        result = apply_book_patch(sample_book, patch)
        updated = result.chapters[1].blocks[1]
        assert isinstance(updated, Table)
        assert updated.html == "<table><tr><td>legacy</td></tr></table>"
        assert updated.merge_record is None

    def test_replace_table_old_node_missing_merge_record_fails_when_present(self):
        """Legacy old_node without merge_record is rejected when current table has one."""
        table = Table(
            uid="tbl-merged",
            html="<table><tbody><tr><td>A</td></tr></tbody></table>",
            multi_page=True,
            provenance=_prov(),
            merge_record=TableMergeRecord(
                segment_html=[
                    "<tbody><tr><td>A</td></tr></tbody>",
                    "<tbody><tr><td>B</td></tr></tbody>",
                ],
                segment_pages=[1, 2],
                segment_order=[0, 1],
                column_widths=[1, 1],
            ),
        )
        book = Book(
            title="Merged Table Book",
            chapters=[Chapter(uid="ch-table", title="Tables", blocks=[table])],
        )
        old_node = table.model_dump(mode="python")
        old_node.pop("merge_record")
        new_node = table.model_dump(mode="python")
        new_node.pop("uid")
        new_node["table_title"] = "Updated title"
        patch = _make_patch(
            [
                ReplaceNodeChange(
                    op="replace_node",
                    target_uid="tbl-merged",
                    old_node=old_node,
                    new_node=new_node,
                )
            ]
        )

        with pytest.raises(PatchError):
            apply_book_patch(book, patch)

    def test_replace_table_old_node_precondition_includes_merge_record(self, sample_book):
        """Table old_node precondition accepts a full snapshot with merge_record."""
        table = Table(
            uid="tbl-merged",
            html="<table><tbody><tr><td>A</td></tr></tbody></table>",
            multi_page=True,
            provenance=_prov(),
            merge_record=TableMergeRecord(
                segment_html=[
                    "<tbody><tr><td>A</td></tr></tbody>",
                    "<tbody><tr><td>B</td></tr></tbody>",
                ],
                segment_pages=[1, 2],
                segment_order=[0, 1],
                column_widths=[1, 1],
            ),
        )
        book = Book(
            title="Merged Table Book",
            chapters=[Chapter(uid="ch-table", title="Tables", blocks=[table])],
        )
        old_node = table.model_dump(mode="python")
        new_node = old_node.copy()
        new_node.pop("uid")
        new_node["table_title"] = "Updated title"
        patch = _make_patch(
            [
                ReplaceNodeChange(
                    op="replace_node",
                    target_uid="tbl-merged",
                    old_node=old_node,
                    new_node=new_node,
                )
            ]
        )

        result = apply_book_patch(book, patch)
        updated = result.chapters[0].blocks[0]
        assert isinstance(updated, Table)
        assert updated.table_title == "Updated title"
        assert updated.merge_record == table.merge_record


# ---------------------------------------------------------------------------
# 8.3 InsertNodeChange tests
# ---------------------------------------------------------------------------


class TestInsertNodeChange:
    def test_insert_block_at_end(self, sample_book):
        """Insert new block after the last block — appears at end."""
        patch = _make_patch(
            [
                InsertNodeChange(
                    op="insert_node",
                    parent_uid="ch-1",
                    after_uid="blk-3",
                    node={
                        "uid": "blk-new",
                        "kind": "paragraph",
                        "text": "End block",
                        "role": "body",
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        ch = result.chapters[0]
        assert len(ch.blocks) == 4
        assert ch.blocks[-1].uid == "blk-new"

    def test_insert_block_at_beginning(self, sample_book):
        """Insert new block at the beginning (after_uid=None)."""
        patch = _make_patch(
            [
                InsertNodeChange(
                    op="insert_node",
                    parent_uid="ch-1",
                    after_uid=None,
                    node={
                        "uid": "blk-new",
                        "kind": "paragraph",
                        "text": "First block",
                        "role": "body",
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        ch = result.chapters[0]
        assert len(ch.blocks) == 4
        assert ch.blocks[0].uid == "blk-new"

    def test_insert_table_preserves_merge_record(self, sample_book):
        """Table insert_node payload round-trips merge_record."""
        merge_record = {
            "segment_html": [
                "<tbody><tr><td>1</td></tr></tbody>",
                "<tbody><tr><td>2</td></tr></tbody>",
            ],
            "segment_pages": [10, 11],
            "segment_order": [0, 1],
            "column_widths": [1, 1],
        }
        patch = _make_patch(
            [
                InsertNodeChange(
                    op="insert_node",
                    parent_uid="ch-1",
                    after_uid="blk-3",
                    node={
                        "uid": "tbl-new",
                        "kind": "table",
                        "html": "<table><tbody><tr><td>1</td></tr>"
                        "<tr><td>2</td></tr></tbody></table>",
                        "table_title": "Inserted merged table",
                        "caption": "",
                        "continuation": False,
                        "multi_page": True,
                        "bbox": None,
                        "provenance": _prov().model_dump(mode="python"),
                        "merge_record": merge_record,
                    },
                )
            ]
        )

        result = apply_book_patch(sample_book, patch)
        inserted = result.chapters[0].blocks[-1]
        assert isinstance(inserted, Table)
        assert inserted.uid == "tbl-new"
        assert inserted.merge_record == TableMergeRecord.model_validate(merge_record)

    def test_insert_block_in_middle(self, sample_book):
        """Insert block in the middle of a chapter."""
        patch = _make_patch(
            [
                InsertNodeChange(
                    op="insert_node",
                    parent_uid="ch-1",
                    after_uid="blk-1",
                    node={
                        "uid": "blk-mid",
                        "kind": "paragraph",
                        "text": "Middle block",
                        "role": "body",
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        ch = result.chapters[0]
        assert len(ch.blocks) == 4
        assert ch.blocks[1].uid == "blk-mid"
        assert ch.blocks[0].uid == "blk-1"
        assert ch.blocks[2].uid == "blk-2"

    def test_insert_chapter(self, sample_book):
        """Insert new chapter (parent_uid=None) — success.

        Note: InsertNodeChange model validator requires a 'kind' key in the node dict.
        When inserting a chapter, 'kind' is included and is preserved by Chapter.model_validate
        because Chapter now has a Literal["chapter"] kind field (round-trip safe).
        """
        patch = _make_patch(
            [
                InsertNodeChange(
                    op="insert_node",
                    parent_uid=None,
                    after_uid="ch-2",
                    node={
                        "uid": "ch-new",
                        "kind": "chapter",
                        "title": "New Chapter",
                        "level": 1,
                        "blocks": [],
                    },
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        assert len(result.chapters) == 3
        assert result.chapters[2].uid == "ch-new"

    def test_uid_collision(self, sample_book):
        """Inserting with existing uid — PatchError."""
        patch = _make_patch(
            [
                InsertNodeChange(
                    op="insert_node",
                    parent_uid="ch-1",
                    after_uid=None,
                    node={
                        "uid": "blk-1",
                        "kind": "paragraph",
                        "text": "Collision",
                        "role": "body",
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_parent_uid_not_found(self, sample_book):
        """parent_uid chapter does not exist — PatchError."""
        patch = _make_patch(
            [
                InsertNodeChange(
                    op="insert_node",
                    parent_uid="ch-nonexistent",
                    after_uid=None,
                    node={
                        "uid": "blk-new",
                        "kind": "paragraph",
                        "text": "Text",
                        "role": "body",
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_after_uid_not_in_parent(self, sample_book):
        """after_uid not in the specified parent chapter — PatchError."""
        patch = _make_patch(
            [
                InsertNodeChange(
                    op="insert_node",
                    parent_uid="ch-1",
                    after_uid="blk-4",  # blk-4 is in ch-2, not ch-1
                    node={
                        "uid": "blk-new",
                        "kind": "paragraph",
                        "text": "Text",
                        "role": "body",
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_node_missing_uid(self):
        """node dict missing uid — Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            InsertNodeChange(
                op="insert_node",
                parent_uid="ch-1",
                after_uid=None,
                node={"kind": "paragraph", "text": "No uid"},
            )

    def test_node_missing_kind(self):
        """node dict missing kind — Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            InsertNodeChange(
                op="insert_node",
                parent_uid="ch-1",
                after_uid=None,
                node={"uid": "blk-new", "text": "No kind"},
            )


# ---------------------------------------------------------------------------
# 8.4 DeleteNodeChange tests
# ---------------------------------------------------------------------------


class TestDeleteNodeChange:
    def test_delete_block(self, sample_book):
        """Delete an existing block — block removed from chapter."""
        blk = sample_book.chapters[0].blocks[0]
        old_node = blk.model_dump(mode="python")
        patch = _make_patch(
            [
                DeleteNodeChange(
                    op="delete_node", target_uid="blk-1", old_node=old_node
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        ch = result.chapters[0]
        assert len(ch.blocks) == 2
        assert all(b.uid != "blk-1" for b in ch.blocks)

    def test_delete_table_old_node_missing_merge_record_passes_when_none(self, sample_book):
        """Legacy table old_node without merge_record can delete a table with merge_record=None."""
        table = sample_book.chapters[1].blocks[1]
        assert isinstance(table, Table)
        old_node = table.model_dump(mode="python")
        old_node.pop("merge_record")
        patch = _make_patch(
            [
                DeleteNodeChange(
                    op="delete_node", target_uid="blk-5", old_node=old_node
                ),
            ]
        )

        result = apply_book_patch(sample_book, patch)

        assert [block.uid for block in result.chapters[1].blocks] == ["blk-4", "blk-6"]

    def test_delete_table_old_node_missing_merge_record_fails_when_present(self):
        """Legacy table old_node without merge_record is rejected for merged tables."""
        table = Table(
            uid="tbl-merged",
            html="<table><tbody><tr><td>A</td></tr></tbody></table>",
            multi_page=True,
            provenance=_prov(),
            merge_record=TableMergeRecord(
                segment_html=[
                    "<tbody><tr><td>A</td></tr></tbody>",
                    "<tbody><tr><td>B</td></tr></tbody>",
                ],
                segment_pages=[1, 2],
                segment_order=[0, 1],
                column_widths=[1, 1],
            ),
        )
        book = Book(
            title="Merged Table Book",
            chapters=[Chapter(uid="ch-table", title="Tables", blocks=[table])],
        )
        old_node = table.model_dump(mode="python")
        old_node.pop("merge_record")
        patch = _make_patch(
            [
                DeleteNodeChange(
                    op="delete_node", target_uid="tbl-merged", old_node=old_node
                ),
            ]
        )

        with pytest.raises(PatchError):
            apply_book_patch(book, patch)

    def test_delete_empty_chapter(self, sample_book):
        """Delete an empty chapter — success."""
        # Create a book with an empty chapter
        book = Book(
            title="Test",
            chapters=[
                Chapter(uid="ch-1", title="Chap 1", level=1, blocks=[]),
                Chapter(
                    uid="ch-2",
                    title="Chap 2",
                    level=1,
                    blocks=[
                        Paragraph(
                            uid="blk-1", text="Text", role="body", provenance=_prov()
                        ),
                    ],
                ),
            ],
        )
        empty_ch = book.chapters[0]
        old_node = empty_ch.model_dump(mode="python")
        patch = _make_patch(
            [
                DeleteNodeChange(
                    op="delete_node", target_uid="ch-1", old_node=old_node
                ),
            ]
        )
        result = apply_book_patch(book, patch)
        assert len(result.chapters) == 1
        assert result.chapters[0].uid == "ch-2"

    def test_delete_non_empty_chapter(self, sample_book):
        """Delete non-empty chapter — PatchError."""
        ch = sample_book.chapters[0]
        old_node = ch.model_dump(mode="python")
        patch = _make_patch(
            [
                DeleteNodeChange(
                    op="delete_node", target_uid="ch-1", old_node=old_node
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_old_node_mismatch(self, sample_book):
        """old_node doesn't match current state — PatchError."""
        stale = sample_book.chapters[0].blocks[0].model_dump(mode="python")
        stale["text"] = "stale content"
        patch = _make_patch(
            [
                DeleteNodeChange(op="delete_node", target_uid="blk-1", old_node=stale),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_target_not_found(self, sample_book):
        """target_uid not in book — PatchError."""
        dummy_node = sample_book.chapters[0].blocks[0].model_dump(mode="python")
        patch = _make_patch(
            [
                DeleteNodeChange(
                    op="delete_node", target_uid="nonexistent", old_node=dummy_node
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)


# ---------------------------------------------------------------------------
# 8.5 MoveNodeChange tests
# ---------------------------------------------------------------------------


class TestMoveNodeChange:
    def test_move_block_within_chapter(self, sample_book):
        """Move block within same chapter — order updated."""
        patch = _make_patch(
            [
                MoveNodeChange(
                    op="move_node",
                    target_uid="blk-1",
                    from_parent_uid="ch-1",
                    to_parent_uid="ch-1",
                    after_uid="blk-3",
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        ch = result.chapters[0]
        uids = [b.uid for b in ch.blocks]
        # blk-1 should now be last (after blk-3)
        assert uids == ["blk-2", "blk-3", "blk-1"]

    def test_move_block_across_chapters(self, sample_book):
        """Move block from ch-1 to ch-2 — block removed from source, appears in target."""
        patch = _make_patch(
            [
                MoveNodeChange(
                    op="move_node",
                    target_uid="blk-1",
                    from_parent_uid="ch-1",
                    to_parent_uid="ch-2",
                    after_uid="blk-4",
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        ch1_uids = [b.uid for b in result.chapters[0].blocks]
        ch2_uids = [b.uid for b in result.chapters[1].blocks]
        assert "blk-1" not in ch1_uids
        assert ch2_uids[1] == "blk-1"  # after blk-4

    def test_move_block_to_beginning(self, sample_book):
        """Move block to beginning of target chapter (after_uid=None)."""
        patch = _make_patch(
            [
                MoveNodeChange(
                    op="move_node",
                    target_uid="blk-3",
                    from_parent_uid="ch-1",
                    to_parent_uid="ch-1",
                    after_uid=None,
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        ch = result.chapters[0]
        assert ch.blocks[0].uid == "blk-3"

    def test_move_chapter_reorder(self, sample_book):
        """Move chapter within book (from_parent_uid=None, to_parent_uid=None)."""
        patch = _make_patch(
            [
                MoveNodeChange(
                    op="move_node",
                    target_uid="ch-1",
                    from_parent_uid=None,
                    to_parent_uid=None,
                    after_uid="ch-2",
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        ch_uids = [c.uid for c in result.chapters]
        assert ch_uids == ["ch-2", "ch-1"]

    def test_from_parent_mismatch(self, sample_book):
        """from_parent_uid doesn't match actual parent — PatchError."""
        patch = _make_patch(
            [
                MoveNodeChange(
                    op="move_node",
                    target_uid="blk-1",
                    from_parent_uid="ch-2",  # wrong parent
                    to_parent_uid="ch-2",
                    after_uid=None,
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_block_move_requires_from_parent_uid(self, sample_book):
        """Block moves must include the current source chapter uid."""
        patch = _make_patch(
            [
                MoveNodeChange(
                    op="move_node",
                    target_uid="blk-1",
                    from_parent_uid=None,
                    to_parent_uid="ch-2",
                    after_uid=None,
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_to_parent_not_found(self, sample_book):
        """to_parent_uid doesn't exist — PatchError."""
        patch = _make_patch(
            [
                MoveNodeChange(
                    op="move_node",
                    target_uid="blk-1",
                    from_parent_uid="ch-1",
                    to_parent_uid="ch-nonexistent",
                    after_uid=None,
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_after_uid_equals_target(self):
        """after_uid == target_uid — Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            MoveNodeChange(
                op="move_node",
                target_uid="blk-1",
                from_parent_uid="ch-1",
                to_parent_uid="ch-1",
                after_uid="blk-1",
            )

    def test_after_uid_not_in_target(self, sample_book):
        """after_uid not in target chapter — PatchError."""
        patch = _make_patch(
            [
                MoveNodeChange(
                    op="move_node",
                    target_uid="blk-1",
                    from_parent_uid="ch-1",
                    to_parent_uid="ch-2",
                    after_uid="blk-2",  # blk-2 is in ch-1, not ch-2
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_target_not_found(self, sample_book):
        """target_uid not found — PatchError."""
        patch = _make_patch(
            [
                MoveNodeChange(
                    op="move_node",
                    target_uid="nonexistent",
                    from_parent_uid="ch-1",
                    to_parent_uid="ch-1",
                    after_uid=None,
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)


# ---------------------------------------------------------------------------
# 8.6 PatchScope tests
# ---------------------------------------------------------------------------


class TestPatchScope:
    def test_scope_violation_different_chapter(self, sample_book):
        """scope.chapter_uid=ch-1 but change targets ch-2's block — PatchError."""
        patch = _make_patch(
            changes=[
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-4",
                    field="text",
                    old="Second chapter text",
                    new="Updated",
                ),
            ],
            scope_chapter="ch-1",
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_scope_violation_cross_chapter_move(self, sample_book):
        """scope=ch-1, cross-chapter move — PatchError."""
        patch = _make_patch(
            changes=[
                MoveNodeChange(
                    op="move_node",
                    target_uid="blk-1",
                    from_parent_uid="ch-1",
                    to_parent_uid="ch-2",
                    after_uid=None,
                ),
            ],
            scope_chapter="ch-1",
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_scope_none_allows_cross_chapter(self, sample_book):
        """scope=None, cross-chapter move — success."""
        patch = _make_patch(
            changes=[
                MoveNodeChange(
                    op="move_node",
                    target_uid="blk-1",
                    from_parent_uid="ch-1",
                    to_parent_uid="ch-2",
                    after_uid=None,
                ),
            ],
            scope_chapter=None,
        )
        result = apply_book_patch(sample_book, patch)
        ch2_uids = [b.uid for b in result.chapters[1].blocks]
        assert "blk-1" in ch2_uids


# ---------------------------------------------------------------------------
# 8.7 BookPatch model tests
# ---------------------------------------------------------------------------


class TestBookPatchModel:
    def _minimal_change(self):
        return [
            SetFieldChange(
                op="set_field", target_uid="blk-1", field="text", old="a", new="b"
            ),
        ]

    def test_invalid_patch_id(self):
        """patch_id not UUID4 — Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            BookPatch(
                patch_id="not-a-uuid",
                agent_id="test-agent",
                scope=PatchScope(),
                changes=self._minimal_change(),
                rationale="test",
            )

    def test_empty_agent_id(self):
        """Empty agent_id — Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            BookPatch(
                patch_id=str(uuid4()),
                agent_id="",
                scope=PatchScope(),
                changes=self._minimal_change(),
                rationale="test",
            )

    def test_whitespace_agent_id(self):
        """Whitespace-only agent_id — Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            BookPatch(
                patch_id=str(uuid4()),
                agent_id="   ",
                scope=PatchScope(),
                changes=self._minimal_change(),
                rationale="test",
            )

    def test_empty_rationale(self):
        """Empty rationale — Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            BookPatch(
                patch_id=str(uuid4()),
                agent_id="test-agent",
                scope=PatchScope(),
                changes=self._minimal_change(),
                rationale="",
            )

    def test_whitespace_rationale(self):
        """Whitespace-only rationale — Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            BookPatch(
                patch_id=str(uuid4()),
                agent_id="test-agent",
                scope=PatchScope(),
                changes=self._minimal_change(),
                rationale="   ",
            )

    def test_empty_changes(self):
        """Empty changes list is a legal no-op patch."""
        patch = BookPatch(
            patch_id=str(uuid4()),
            agent_id="test-agent",
            scope=PatchScope(),
            changes=[],
            rationale="test",
        )
        assert patch.changes == []


class TestPublicPatchHelpers:
    def test_allowed_set_fields_returns_public_frozenset(self):
        """allowed_set_fields exposes the editable field mapping for diff code."""
        assert allowed_set_fields("paragraph") == frozenset(
            {"text", "role", "style_class", "cross_page", "display_lines"}
        )
        assert "merge_record" not in allowed_set_fields("table")
        assert allowed_set_fields("unknown") == frozenset()

    def test_serialize_patch_field_value_serializes_models_recursively(self):
        """serialize_patch_field_value matches patch precondition serialization."""
        value = {
            "provenance": _prov(),
            "records": [
                TableMergeRecord(
                    segment_html=["<tbody></tbody>", "<tbody></tbody>"],
                    segment_pages=[1, 2],
                    segment_order=[0, 1],
                    column_widths=[1, 1],
                )
            ],
        }

        assert serialize_patch_field_value(value) == {
            "provenance": _prov().model_dump(mode="json"),
            "records": [
                {
                    "segment_html": ["<tbody></tbody>", "<tbody></tbody>"],
                    "segment_pages": [1, 2],
                    "segment_order": [0, 1],
                    "column_widths": [1, 1],
                }
            ],
        }


# ---------------------------------------------------------------------------
# 8.8 Apply result tests
# ---------------------------------------------------------------------------


class TestApplyResults:
    def test_empty_patch_validates_and_applies_as_noop(self, sample_book):
        """Empty BookPatch validates and applies without changing book semantics."""
        patch = _make_patch([])

        validate_book_patch(sample_book, patch)
        result = apply_book_patch(sample_book, patch)

        assert result.model_dump(mode="python") == sample_book.model_dump(mode="python")
        assert result is not sample_book

    def test_original_book_unchanged(self, sample_book):
        """Deep copy semantics: original book not modified after apply."""
        original_title = sample_book.chapters[0].blocks[0].text  # type: ignore[union-attr]
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="text",
                    old="Hello world",
                    new="Changed",
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        # Result is changed
        assert result.chapters[0].blocks[0].text == "Changed"  # type: ignore[union-attr]
        # Original is not
        assert sample_book.chapters[0].blocks[0].text == original_title  # type: ignore[union-attr]

    def test_failed_apply_leaves_book_unchanged(self, sample_book):
        """Failed apply: original book remains intact."""
        original_text = sample_book.chapters[0].blocks[0].text  # type: ignore[union-attr]
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="text",
                    old="wrong precondition",
                    new="Changed",
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)
        # Original unchanged
        assert sample_book.chapters[0].blocks[0].text == original_text  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 8.9 Multi-change combo tests
# ---------------------------------------------------------------------------


class TestMultiChange:
    def test_insert_then_set_field_on_new_node(self, sample_book):
        """Insert a new node, then set_field on it in a subsequent patch — success.

        Note: The static pre-checker in validate_book_patch validates set_field target_uid
        against the original book's index. A set_field targeting a uid inserted in the same
        patch would be rejected by the static check. The incremental execution in apply
        supports order dependency, so to demonstrate this capability we use two separate
        patches: first insert the node, then set_field on it.
        """
        # Patch 1: insert the new node
        patch1 = _make_patch(
            [
                InsertNodeChange(
                    op="insert_node",
                    parent_uid="ch-1",
                    after_uid=None,
                    node={
                        "uid": "blk-new",
                        "kind": "paragraph",
                        "text": "Initial text",
                        "role": "body",
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
            ]
        )
        book_with_new = apply_book_patch(sample_book, patch1)

        # Patch 2: set_field on the newly inserted node (now it exists in the book)
        patch2 = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-new",
                    field="text",
                    old="Initial text",
                    new="Updated text",
                ),
            ]
        )
        result = apply_book_patch(book_with_new, patch2)

        # Find the updated block
        all_blocks = {b.uid: b for ch in result.chapters for b in ch.blocks}
        assert all_blocks["blk-new"].text == "Updated text"  # type: ignore[union-attr]

    def test_duplicate_insert_uid(self, sample_book):
        """Two inserts with same uid in one patch — PatchError."""
        patch = _make_patch(
            [
                InsertNodeChange(
                    op="insert_node",
                    parent_uid="ch-1",
                    after_uid=None,
                    node={
                        "uid": "blk-dup",
                        "kind": "paragraph",
                        "text": "First",
                        "role": "body",
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
                InsertNodeChange(
                    op="insert_node",
                    parent_uid="ch-2",
                    after_uid=None,
                    node={
                        "uid": "blk-dup",
                        "kind": "paragraph",
                        "text": "Second",
                        "role": "body",
                        "provenance": {
                            "page": 1,
                            "bbox": None,
                            "source": "passthrough",
                            "raw_ref": None,
                            "raw_label": None,
                            "artifact_id": None,
                            "evidence_ref": None,
                        },
                    },
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_delete_then_set_field_on_deleted(self, sample_book):
        """Delete a block then try to set_field on it — PatchError (uid no longer exists)."""
        blk = sample_book.chapters[0].blocks[0]
        old_node = blk.model_dump(mode="python")
        patch = _make_patch(
            [
                DeleteNodeChange(
                    op="delete_node", target_uid="blk-1", old_node=old_node
                ),
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="text",
                    old="Hello world",
                    new="This should fail",
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_chained_set_field(self, sample_book):
        """Chained set_field: a→b then b→c — success, final text == 'c'."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="text",
                    old="Hello world",
                    new="intermediate",
                ),
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="text",
                    old="intermediate",
                    new="final value",
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        block = result.chapters[0].blocks[0]
        assert block.text == "final value"  # type: ignore[union-attr]

    def test_chained_set_field_non_incremental_fails(self, sample_book):
        """Both changes use old='Hello world' — second fails because first changed it."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="text",
                    old="Hello world",
                    new="intermediate",
                ),
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-1",
                    field="text",
                    old="Hello world",
                    new="also changed",
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)


# ---------------------------------------------------------------------------
# 8.10 uid=None guard tests
# ---------------------------------------------------------------------------


class TestUidNoneGuard:
    def test_chapter_uid_none(self):
        """Book with chapter.uid=None — PatchError."""
        book = Book(
            title="Test",
            chapters=[
                Chapter(uid=None, title="No UID", level=1, blocks=[]),
            ],
        )
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="anything",
                    field="text",
                    old="a",
                    new="b",
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(book, patch)

    def test_block_uid_none(self):
        """Book with block.uid=None — PatchError."""
        book = Book(
            title="Test",
            chapters=[
                Chapter(
                    uid="ch-1",
                    title="Chapter",
                    level=1,
                    blocks=[
                        Paragraph(
                            uid=None,
                            text="No uid block",
                            role="body",
                            provenance=_prov(),
                        ),
                    ],
                ),
            ],
        )
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="ch-1",
                    field="title",
                    old="Chapter",
                    new="New title",
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(book, patch)

    def test_validate_also_catches_uid_none(self):
        """validate_book_patch also raises PatchError for uid=None."""
        book = Book(
            title="Test",
            chapters=[
                Chapter(uid=None, title="No UID", level=1, blocks=[]),
            ],
        )
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="anything",
                    field="text",
                    old="a",
                    new="b",
                ),
            ]
        )
        with pytest.raises(PatchError):
            validate_book_patch(book, patch)


# ---------------------------------------------------------------------------
# 8.11 Complex type comparison tests
# ---------------------------------------------------------------------------


class TestComplexTypeComparison:
    def test_old_new_both_none(self):
        """old=None, new=None — Pydantic ValidationError (old == new)."""
        with pytest.raises(ValidationError):
            SetFieldChange(
                op="set_field", target_uid="blk-1", field="bbox", old=None, new=None
            )

    def test_old_new_same_list(self):
        """old=[1.0, 2.0], new=[1.0, 2.0] — Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            SetFieldChange(
                op="set_field",
                target_uid="blk-1",
                field="bbox",
                old=[1.0, 2.0],
                new=[1.0, 2.0],
            )

    def test_old_new_same_dict(self):
        """old={"a": 1}, new={"a": 1} — Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            SetFieldChange(
                op="set_field",
                target_uid="blk-1",
                field="style_class",
                old={"a": 1},
                new={"a": 1},
            )


# ---------------------------------------------------------------------------
# 8.12 IR invariant tests
# ---------------------------------------------------------------------------


class TestIRInvariants:
    def test_footnote_paired_and_orphan_via_set_field(self, sample_book):
        """Setting footnote paired=True when orphan=True (or vice versa) — PatchError."""
        # First set orphan=True
        patch1 = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-3",
                    field="orphan",
                    old=False,
                    new=True,
                ),
            ]
        )
        book_with_orphan = apply_book_patch(sample_book, patch1)
        # Now try to set paired=True — should fail due to mutual exclusion
        patch2 = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="blk-3",
                    field="paired",
                    old=False,
                    new=True,
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(book_with_orphan, patch2)

    def test_heading_level_invalid_via_set_field(self, sample_book):
        """Setting heading level to 5 via set_field — PatchError."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field", target_uid="blk-2", field="level", old=1, new=5
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)


# ---------------------------------------------------------------------------
# 8.13 ReplaceNodeChange + chapter tests
# ---------------------------------------------------------------------------


class TestReplaceNodeChapter:
    def test_replace_node_on_chapter_uid_rejected(self, sample_book):
        """ReplaceNodeChange targeting a chapter uid — PatchError."""
        ch = sample_book.chapters[0]
        old_node = ch.model_dump(mode="python")
        patch = _make_patch(
            [
                ReplaceNodeChange(
                    op="replace_node",
                    target_uid="ch-1",
                    old_node=old_node,
                    new_node={
                        "kind": "paragraph",
                        "text": "Should fail",
                        "provenance": {"page": 1, "source": "passthrough"},
                    },
                ),
            ]
        )
        with pytest.raises(PatchError):
            apply_book_patch(sample_book, patch)

    def test_chapter_metadata_via_set_field(self, sample_book):
        """Chapter metadata (title) modification via SetFieldChange — success."""
        patch = _make_patch(
            [
                SetFieldChange(
                    op="set_field",
                    target_uid="ch-2",
                    field="title",
                    old="Chapter 2",
                    new="Modified Chapter 2",
                ),
            ]
        )
        result = apply_book_patch(sample_book, patch)
        assert result.chapters[1].title == "Modified Chapter 2"
