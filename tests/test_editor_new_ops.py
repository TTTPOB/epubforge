"""Tests for the three new editor ops: replace_block, set_paragraph_cross_page, set_table_metadata."""

from __future__ import annotations

from typing import Callable
from uuid import uuid4

import pytest

from epubforge.editor.apply import ApplyError, apply_envelope
from epubforge.editor.leases import ChapterLease, LeaseState
from epubforge.editor.ops import (
    OpEnvelope,
    ReplaceBlock,
    RevertOp,
    SetParagraphCrossPage,
    SetTableMetadata,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prov(page: int = 1) -> Provenance:
    return Provenance(page=page, source="passthrough")


def _book_single_chapter() -> Book:
    return Book(
        title="Test Book",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter 1",
                blocks=[
                    Paragraph(uid="p-1", text="Alpha body text", provenance=_prov()),
                    Heading(uid="h-1", text="Section One", level=2, provenance=_prov()),
                    Footnote(uid="fn-1", callout="①", text="A footnote", provenance=_prov()),
                    Table(
                        uid="t-1",
                        html="<table><tr><td>cell</td></tr></table>",
                        table_title="Table 1",
                        caption="Some caption",
                        continuation=False,
                        multi_page=False,
                        provenance=_prov(),
                    ),
                ],
            )
        ],
    )


def _multi_page_table_book() -> Book:
    """Book with a multi_page table that has a merge_record."""
    merge_record = TableMergeRecord(
        segment_html=["<tbody><tr><td>A</td></tr></tbody>", "<tbody><tr><td>B</td></tr></tbody>"],
        segment_pages=[1, 2],
        segment_order=[0, 1],
        column_widths=[1, 1],
    )
    return Book(
        title="Multi-page Book",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter 1",
                blocks=[
                    Table(
                        uid="t-mp",
                        html="<table><tr><td>merged</td></tr></table>",
                        table_title="Big Table",
                        caption="Big caption",
                        continuation=False,
                        multi_page=True,
                        provenance=_prov(),
                        merge_record=merge_record,
                    ),
                ],
            )
        ],
    )


def _env(
    op,
    *,
    base_version: int = 0,
    op_id: str | None = None,
    preconditions: list[dict] | None = None,
) -> OpEnvelope:
    return OpEnvelope.model_validate(
        {
            "op_id": op_id or str(uuid4()),
            "ts": "2026-04-23T08:00:00Z",
            "agent_id": "test-agent",
            "base_version": base_version,
            "preconditions": preconditions or [],
            "op": op if isinstance(op, dict) else op.model_dump(mode="json"),
            "rationale": "test",
        }
    )


_FUTURE_TS = "2099-01-01T00:00:00Z"


def _chapter_lease(chapter_uid: str, holder: str = "test-agent") -> LeaseState:
    """Return a LeaseState with a far-future chapter lease so it won't expire during tests."""
    state = LeaseState()
    state.acquire_chapter(chapter_uid, holder, "test task", now=_FUTURE_TS, ttl=86400)
    return state


# ---------------------------------------------------------------------------
# replace_block: schema validation
# ---------------------------------------------------------------------------


class TestReplaceBlockSchema:
    def test_valid_schema_all_fields(self) -> None:
        prov_data = _prov().model_dump(mode="json")
        original = {
            "kind": "paragraph",
            "uid": "p-1",
            "text": "Alpha body text",
            "role": "body",
            "display_lines": None,
            "style_class": None,
            "cross_page": False,
            "provenance": prov_data,
        }
        op = ReplaceBlock(
            op="replace_block",
            block_uid="p-1",
            block_kind="heading",
            block_data={"level": 2, "text": "New Heading", "provenance": prov_data},
            new_block_uid="h-new",
            original_block=original,
        )
        assert op.block_uid == "p-1"
        assert op.block_kind == "heading"
        assert op.new_block_uid == "h-new"

    def test_valid_schema_without_new_uid(self) -> None:
        prov_data = _prov().model_dump(mode="json")
        original = {
            "kind": "paragraph",
            "uid": "p-1",
            "text": "Alpha body text",
            "role": "body",
            "display_lines": None,
            "style_class": None,
            "cross_page": False,
            "provenance": prov_data,
        }
        op = ReplaceBlock(
            op="replace_block",
            block_uid="p-1",
            block_kind="paragraph",
            block_data={"text": "New text", "role": "body", "provenance": prov_data},
            original_block=original,
        )
        assert op.new_block_uid is None

    def test_block_data_validated_against_kind(self) -> None:
        prov_data = _prov().model_dump(mode="json")
        original = {"kind": "paragraph", "uid": "p-1", "text": "Alpha body text", "role": "body", "display_lines": None, "style_class": None, "cross_page": False, "provenance": prov_data}
        # heading requires "text" and "level" — but text is there; heading without text should fail
        with pytest.raises(Exception):
            ReplaceBlock(
                op="replace_block",
                block_uid="p-1",
                block_kind="heading",
                block_data={"level": 2, "provenance": prov_data},  # missing "text"
                original_block=original,
            )

    def test_empty_block_uid_rejected(self) -> None:
        prov_data = _prov().model_dump(mode="json")
        with pytest.raises(Exception):
            ReplaceBlock(
                op="replace_block",
                block_uid="",
                block_kind="paragraph",
                block_data={"text": "x", "role": "body", "provenance": prov_data},
                original_block={"kind": "paragraph", "uid": "p-1"},
            )


# ---------------------------------------------------------------------------
# replace_block: apply
# ---------------------------------------------------------------------------


class TestReplaceBlockApply:
    def _snapshot(self, block) -> dict:
        return block.model_dump(mode="json")

    def test_apply_paragraph_to_heading(self) -> None:
        book = _book_single_chapter()
        prov_data = _prov().model_dump(mode="json")
        para = book.chapters[0].blocks[0]
        assert isinstance(para, Paragraph)

        op = ReplaceBlock(
            op="replace_block",
            block_uid="p-1",
            block_kind="heading",
            block_data={"level": 1, "text": "Now a Heading", "provenance": prov_data},
            original_block=self._snapshot(para),
        )
        result = apply_envelope(
            book,
            _env(op),
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
        )
        replaced = result.book.chapters[0].blocks[0]
        assert isinstance(replaced, Heading)
        assert replaced.text == "Now a Heading"
        assert replaced.uid == "p-1"  # uid preserved by default

    def test_apply_paragraph_to_footnote(self) -> None:
        book = _book_single_chapter()
        prov_data = _prov().model_dump(mode="json")
        para = book.chapters[0].blocks[0]
        assert isinstance(para, Paragraph)

        op = ReplaceBlock(
            op="replace_block",
            block_uid="p-1",
            block_kind="footnote",
            block_data={"callout": "①", "text": "A footnote body", "provenance": prov_data},
            original_block=self._snapshot(para),
        )
        result = apply_envelope(
            book,
            _env(op),
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
        )
        replaced = result.book.chapters[0].blocks[0]
        assert isinstance(replaced, Footnote)
        assert replaced.callout == "①"
        assert replaced.uid == "p-1"

    def test_apply_rejects_stale_original_block(self) -> None:
        book = _book_single_chapter()
        prov_data = _prov().model_dump(mode="json")
        # Pass a snapshot with wrong text
        stale_snapshot = {
            "kind": "paragraph",
            "uid": "p-1",
            "text": "THIS IS NOT THE ACTUAL TEXT",
            "role": "body",
            "display_lines": None,
            "style_class": None,
            "cross_page": False,
            "provenance": prov_data,
        }
        op = ReplaceBlock(
            op="replace_block",
            block_uid="p-1",
            block_kind="heading",
            block_data={"level": 2, "text": "Heading", "provenance": prov_data},
            original_block=stale_snapshot,
        )
        with pytest.raises(ApplyError, match="does not match original_block"):
            apply_envelope(
                book,
                _env(op),
                lease_state=_chapter_lease("ch-1"),
                lease_holder="test-agent",
            )

    def test_uid_preserved_when_new_block_uid_is_none(self) -> None:
        book = _book_single_chapter()
        prov_data = _prov().model_dump(mode="json")
        para = book.chapters[0].blocks[0]
        op = ReplaceBlock(
            op="replace_block",
            block_uid="p-1",
            block_kind="paragraph",
            block_data={"text": "Updated text", "role": "body", "provenance": prov_data},
            new_block_uid=None,
            original_block=para.model_dump(mode="json"),
        )
        result = apply_envelope(
            book,
            _env(op),
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
        )
        assert result.book.chapters[0].blocks[0].uid == "p-1"

    def test_explicit_new_block_uid_used_when_unique(self) -> None:
        book = _book_single_chapter()
        prov_data = _prov().model_dump(mode="json")
        para = book.chapters[0].blocks[0]
        op = ReplaceBlock(
            op="replace_block",
            block_uid="p-1",
            block_kind="paragraph",
            block_data={"text": "Updated text", "role": "body", "provenance": prov_data},
            new_block_uid="p-new",
            original_block=para.model_dump(mode="json"),
        )
        result = apply_envelope(
            book,
            _env(op),
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
        )
        assert result.book.chapters[0].blocks[0].uid == "p-new"
        # original uid p-1 is gone
        uids = [b.uid for b in result.book.chapters[0].blocks]
        assert "p-1" not in uids

    def test_new_block_uid_conflict_rejected(self) -> None:
        book = _book_single_chapter()
        prov_data = _prov().model_dump(mode="json")
        para = book.chapters[0].blocks[0]
        # "h-1" already exists in the book
        op = ReplaceBlock(
            op="replace_block",
            block_uid="p-1",
            block_kind="paragraph",
            block_data={"text": "New text", "role": "body", "provenance": prov_data},
            new_block_uid="h-1",
            original_block=para.model_dump(mode="json"),
        )
        with pytest.raises(ApplyError, match="uid collision"):
            apply_envelope(
                book,
                _env(op),
                lease_state=_chapter_lease("ch-1"),
                lease_holder="test-agent",
            )

    def test_lease_enforcement_no_lease_rejected(self) -> None:
        book = _book_single_chapter()
        prov_data = _prov().model_dump(mode="json")
        para = book.chapters[0].blocks[0]
        op = ReplaceBlock(
            op="replace_block",
            block_uid="p-1",
            block_kind="heading",
            block_data={"level": 2, "text": "H", "provenance": prov_data},
            original_block=para.model_dump(mode="json"),
        )
        # Provide an empty lease state — no chapter lease held
        with pytest.raises(ApplyError, match="chapter lease"):
            apply_envelope(
                book,
                _env(op),
                lease_state=LeaseState(),
                lease_holder="test-agent",
            )

    def test_lease_enforcement_wrong_holder_rejected(self) -> None:
        book = _book_single_chapter()
        prov_data = _prov().model_dump(mode="json")
        para = book.chapters[0].blocks[0]
        op = ReplaceBlock(
            op="replace_block",
            block_uid="p-1",
            block_kind="heading",
            block_data={"level": 2, "text": "H", "provenance": prov_data},
            original_block=para.model_dump(mode="json"),
        )
        # Lease held by "other-agent", not "test-agent"
        lease_state = _chapter_lease("ch-1", holder="other-agent")
        with pytest.raises(ApplyError, match="chapter lease"):
            apply_envelope(
                book,
                _env(op),
                lease_state=lease_state,
                lease_holder="test-agent",
            )

    def test_revert_produces_correct_reverse_op(self) -> None:
        book = _book_single_chapter()
        prov_data = _prov().model_dump(mode="json")
        para = book.chapters[0].blocks[0]
        assert isinstance(para, Paragraph)
        original_snapshot = para.model_dump(mode="json")

        op = ReplaceBlock(
            op="replace_block",
            block_uid="p-1",
            block_kind="heading",
            block_data={"level": 2, "text": "New Heading", "provenance": prov_data},
            original_block=original_snapshot,
        )
        applied = apply_envelope(
            book,
            _env(op),
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
            now=lambda: "2026-04-23T08:00:01Z",
        )
        assert applied.book.op_log_version == 1

        revert = apply_envelope(
            applied.book,
            _env(RevertOp(op="revert", target_op_id=applied.accepted_envelopes[0].op_id), base_version=1),
            existing_op_ids={applied.accepted_envelopes[0].op_id},
            resolve_target=lambda tid: applied.accepted_envelopes[0] if tid == applied.accepted_envelopes[0].op_id else None,
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
            now=lambda: "2026-04-23T08:00:02Z",
        )
        # After revert the block should be a Paragraph again with original text
        restored = revert.book.chapters[0].blocks[0]
        assert isinstance(restored, Paragraph)
        assert restored.text == "Alpha body text"
        assert restored.uid == "p-1"


# ---------------------------------------------------------------------------
# set_paragraph_cross_page: schema and apply
# ---------------------------------------------------------------------------


class TestSetParagraphCrossPage:
    def test_apply_succeeds_on_paragraph(self) -> None:
        book = _book_single_chapter()
        para = book.chapters[0].blocks[0]
        assert isinstance(para, Paragraph)
        assert para.cross_page is False

        op = SetParagraphCrossPage(op="set_paragraph_cross_page", block_uid="p-1", value=True)
        result = apply_envelope(
            book,
            _env(op),
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
        )
        updated = result.book.chapters[0].blocks[0]
        assert isinstance(updated, Paragraph)
        assert updated.cross_page is True

    def test_apply_rejects_on_non_paragraph_table(self) -> None:
        book = _book_single_chapter()
        op = SetParagraphCrossPage(op="set_paragraph_cross_page", block_uid="t-1", value=True)
        with pytest.raises(ApplyError, match="Paragraph"):
            apply_envelope(
                book,
                _env(op),
                lease_state=_chapter_lease("ch-1"),
                lease_holder="test-agent",
            )

    def test_apply_rejects_on_non_paragraph_heading(self) -> None:
        book = _book_single_chapter()
        op = SetParagraphCrossPage(op="set_paragraph_cross_page", block_uid="h-1", value=True)
        with pytest.raises(ApplyError, match="Paragraph"):
            apply_envelope(
                book,
                _env(op),
                lease_state=_chapter_lease("ch-1"),
                lease_holder="test-agent",
            )

    def test_revert_restores_previous_value(self) -> None:
        book = _book_single_chapter()
        op = SetParagraphCrossPage(op="set_paragraph_cross_page", block_uid="p-1", value=True)
        applied = apply_envelope(
            book,
            _env(
                op,
                preconditions=[{"kind": "field_equals", "block_uid": "p-1", "field": "cross_page", "expected": False}],
            ),
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
            now=lambda: "2026-04-23T08:00:01Z",
        )
        assert applied.book.op_log_version == 1
        assert isinstance(applied.book.chapters[0].blocks[0], Paragraph)
        assert applied.book.chapters[0].blocks[0].cross_page is True

        revert = apply_envelope(
            applied.book,
            _env(RevertOp(op="revert", target_op_id=applied.accepted_envelopes[0].op_id), base_version=1),
            existing_op_ids={applied.accepted_envelopes[0].op_id},
            resolve_target=lambda tid: applied.accepted_envelopes[0] if tid == applied.accepted_envelopes[0].op_id else None,
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
            now=lambda: "2026-04-23T08:00:02Z",
        )
        restored = revert.book.chapters[0].blocks[0]
        assert isinstance(restored, Paragraph)
        assert restored.cross_page is False

    def test_lease_enforcement_no_lease_rejected(self) -> None:
        book = _book_single_chapter()
        op = SetParagraphCrossPage(op="set_paragraph_cross_page", block_uid="p-1", value=True)
        with pytest.raises(ApplyError, match="chapter lease"):
            apply_envelope(
                book,
                _env(op),
                lease_state=LeaseState(),
                lease_holder="test-agent",
            )


# ---------------------------------------------------------------------------
# set_table_metadata: schema validation
# ---------------------------------------------------------------------------


class TestSetTableMetadataSchema:
    def _merge_record_dict(self) -> dict:
        return {
            "segment_html": ["<tbody>A</tbody>", "<tbody>B</tbody>"],
            "segment_pages": [1, 2],
            "segment_order": [0, 1],
            "column_widths": [1, 1],
        }

    def _original_simple(self) -> dict:
        return {"table_title": "T", "caption": "C", "continuation": False, "multi_page": False, "merge_record": None}

    def test_valid_simple_metadata(self) -> None:
        op = SetTableMetadata(
            op="set_table_metadata",
            block_uid="t-1",
            table_title="New Title",
            caption="New Caption",
            continuation=False,
            multi_page=False,
            merge_record=None,
            original_metadata=self._original_simple(),
        )
        assert op.table_title == "New Title"

    def test_valid_multi_page_with_merge_record(self) -> None:
        op = SetTableMetadata(
            op="set_table_metadata",
            block_uid="t-1",
            table_title="Big Table",
            caption="",
            continuation=False,
            multi_page=True,
            merge_record=self._merge_record_dict(),
            original_metadata=self._original_simple(),
        )
        assert op.multi_page is True
        assert op.merge_record is not None

    def test_merge_record_without_multi_page_rejected(self) -> None:
        with pytest.raises(Exception, match="multi_page"):
            SetTableMetadata(
                op="set_table_metadata",
                block_uid="t-1",
                table_title="T",
                caption="",
                continuation=False,
                multi_page=False,
                merge_record=self._merge_record_dict(),
                original_metadata=self._original_simple(),
            )

    def test_multi_page_without_merge_record_rejected(self) -> None:
        with pytest.raises(Exception, match="merge_record"):
            SetTableMetadata(
                op="set_table_metadata",
                block_uid="t-1",
                table_title="T",
                caption="",
                continuation=False,
                multi_page=True,
                merge_record=None,
                original_metadata=self._original_simple(),
            )

    def test_multi_page_and_continuation_mutually_exclusive(self) -> None:
        with pytest.raises(Exception, match="mutually exclusive"):
            SetTableMetadata(
                op="set_table_metadata",
                block_uid="t-1",
                table_title="T",
                caption="",
                continuation=True,
                multi_page=True,
                merge_record=self._merge_record_dict(),
                original_metadata=self._original_simple(),
            )

    def test_continuation_with_merge_record_rejected(self) -> None:
        with pytest.raises(Exception, match="merge_record"):
            SetTableMetadata(
                op="set_table_metadata",
                block_uid="t-1",
                table_title="T",
                caption="",
                continuation=True,
                multi_page=False,
                merge_record=self._merge_record_dict(),
                original_metadata=self._original_simple(),
            )

    def test_merge_record_arrays_misaligned_rejected(self) -> None:
        bad_record = {
            "segment_html": ["<tbody>A</tbody>", "<tbody>B</tbody>"],
            "segment_pages": [1],  # wrong length
            "segment_order": [0, 1],
            "column_widths": [1, 1],
        }
        with pytest.raises(Exception, match="aligned"):
            SetTableMetadata(
                op="set_table_metadata",
                block_uid="t-1",
                table_title="T",
                caption="",
                continuation=False,
                multi_page=True,
                merge_record=bad_record,
                original_metadata=self._original_simple(),
            )

    def test_merge_record_arrays_length_less_than_2_rejected(self) -> None:
        short_record = {
            "segment_html": ["<tbody>A</tbody>"],
            "segment_pages": [1],
            "segment_order": [0],
            "column_widths": [1],
        }
        with pytest.raises(Exception, match="length >= 2"):
            SetTableMetadata(
                op="set_table_metadata",
                block_uid="t-1",
                table_title="T",
                caption="",
                continuation=False,
                multi_page=True,
                merge_record=short_record,
                original_metadata=self._original_simple(),
            )

    def test_non_multi_page_non_continuation_with_merge_record_rejected(self) -> None:
        # Redundant consistency check (same as merge_record_without_multi_page), but covers the
        # explicit "multi_page=False and continuation=False => merge_record=None" rule.
        with pytest.raises(Exception, match="merge_record"):
            SetTableMetadata(
                op="set_table_metadata",
                block_uid="t-1",
                table_title="T",
                caption="",
                continuation=False,
                multi_page=False,
                merge_record=self._merge_record_dict(),
                original_metadata=self._original_simple(),
            )


# ---------------------------------------------------------------------------
# set_table_metadata: apply
# ---------------------------------------------------------------------------


class TestSetTableMetadataApply:
    def _simple_original_metadata(self) -> dict:
        return {
            "table_title": "Table 1",
            "caption": "Some caption",
            "continuation": False,
            "multi_page": False,
            "merge_record": None,
        }

    def _merge_record_dict(self) -> dict:
        return {
            "segment_html": ["<tbody>A</tbody>", "<tbody>B</tbody>"],
            "segment_pages": [1, 2],
            "segment_order": [0, 1],
            "column_widths": [1, 1],
        }

    def test_apply_succeeds_with_valid_metadata(self) -> None:
        book = _book_single_chapter()
        op = SetTableMetadata(
            op="set_table_metadata",
            block_uid="t-1",
            table_title="Updated Title",
            caption="Updated Caption",
            continuation=False,
            multi_page=False,
            merge_record=None,
            original_metadata=self._simple_original_metadata(),
        )
        result = apply_envelope(
            book,
            _env(op),
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
        )
        updated = result.book.chapters[0].blocks[3]
        assert isinstance(updated, Table)
        assert updated.table_title == "Updated Title"
        assert updated.caption == "Updated Caption"

    def test_apply_does_not_modify_html(self) -> None:
        book = _book_single_chapter()
        original_html = "<table><tr><td>cell</td></tr></table>"
        op = SetTableMetadata(
            op="set_table_metadata",
            block_uid="t-1",
            table_title="New Title",
            caption="",
            continuation=False,
            multi_page=False,
            merge_record=None,
            original_metadata=self._simple_original_metadata(),
        )
        result = apply_envelope(
            book,
            _env(op),
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
        )
        updated = result.book.chapters[0].blocks[3]
        assert isinstance(updated, Table)
        assert updated.html == original_html

    def test_apply_rejects_metadata_mismatch(self) -> None:
        book = _book_single_chapter()
        wrong_original = {
            "table_title": "WRONG TITLE",
            "caption": "Some caption",
            "continuation": False,
            "multi_page": False,
            "merge_record": None,
        }
        op = SetTableMetadata(
            op="set_table_metadata",
            block_uid="t-1",
            table_title="New Title",
            caption="",
            continuation=False,
            multi_page=False,
            merge_record=None,
            original_metadata=wrong_original,
        )
        with pytest.raises(ApplyError, match="does not match original_metadata"):
            apply_envelope(
                book,
                _env(op),
                lease_state=_chapter_lease("ch-1"),
                lease_holder="test-agent",
            )

    def test_apply_rejects_on_non_table_block(self) -> None:
        book = _book_single_chapter()
        op = SetTableMetadata(
            op="set_table_metadata",
            block_uid="p-1",
            table_title="T",
            caption="",
            continuation=False,
            multi_page=False,
            merge_record=None,
            original_metadata=self._simple_original_metadata(),
        )
        with pytest.raises(ApplyError, match="Table"):
            apply_envelope(
                book,
                _env(op),
                lease_state=_chapter_lease("ch-1"),
                lease_holder="test-agent",
            )

    def test_apply_multi_page_with_merge_record(self) -> None:
        book = _book_single_chapter()
        op = SetTableMetadata(
            op="set_table_metadata",
            block_uid="t-1",
            table_title="Big Table",
            caption="Big Caption",
            continuation=False,
            multi_page=True,
            merge_record=self._merge_record_dict(),
            original_metadata=self._simple_original_metadata(),
        )
        result = apply_envelope(
            book,
            _env(op),
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
        )
        updated = result.book.chapters[0].blocks[3]
        assert isinstance(updated, Table)
        assert updated.multi_page is True
        assert updated.merge_record is not None
        assert updated.merge_record.segment_pages == [1, 2]

    def test_lease_enforcement_no_lease_rejected(self) -> None:
        book = _book_single_chapter()
        op = SetTableMetadata(
            op="set_table_metadata",
            block_uid="t-1",
            table_title="T",
            caption="",
            continuation=False,
            multi_page=False,
            merge_record=None,
            original_metadata=self._simple_original_metadata(),
        )
        with pytest.raises(ApplyError, match="chapter lease"):
            apply_envelope(
                book,
                _env(op),
                lease_state=LeaseState(),
                lease_holder="test-agent",
            )

    def test_revert_works_correctly(self) -> None:
        book = _book_single_chapter()
        op = SetTableMetadata(
            op="set_table_metadata",
            block_uid="t-1",
            table_title="Updated Title",
            caption="Updated Caption",
            continuation=False,
            multi_page=False,
            merge_record=None,
            original_metadata=self._simple_original_metadata(),
        )
        applied = apply_envelope(
            book,
            _env(op),
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
            now=lambda: "2026-04-23T08:00:01Z",
        )

        revert = apply_envelope(
            applied.book,
            _env(RevertOp(op="revert", target_op_id=applied.accepted_envelopes[0].op_id), base_version=1),
            existing_op_ids={applied.accepted_envelopes[0].op_id},
            resolve_target=lambda tid: applied.accepted_envelopes[0] if tid == applied.accepted_envelopes[0].op_id else None,
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
            now=lambda: "2026-04-23T08:00:02Z",
        )
        restored = revert.book.chapters[0].blocks[3]
        assert isinstance(restored, Table)
        assert restored.table_title == "Table 1"
        assert restored.caption == "Some caption"

    def test_revert_restores_multi_page_metadata(self) -> None:
        book = _multi_page_table_book()
        # Build the "current" metadata snapshot from the book
        mp_table = book.chapters[0].blocks[0]
        assert isinstance(mp_table, Table)
        current_metadata = {
            "table_title": mp_table.table_title,
            "caption": mp_table.caption,
            "continuation": mp_table.continuation,
            "multi_page": mp_table.multi_page,
            "merge_record": mp_table.merge_record.model_dump(mode="json") if mp_table.merge_record else None,
        }
        # Simplify it (set multi_page=False, clear merge_record)
        op = SetTableMetadata(
            op="set_table_metadata",
            block_uid="t-mp",
            table_title="Big Table",
            caption="",
            continuation=False,
            multi_page=False,
            merge_record=None,
            original_metadata=current_metadata,
        )
        applied = apply_envelope(
            book,
            _env(op),
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
            now=lambda: "2026-04-23T08:00:01Z",
        )
        simplified = applied.book.chapters[0].blocks[0]
        assert isinstance(simplified, Table)
        assert simplified.multi_page is False
        assert simplified.merge_record is None

        revert = apply_envelope(
            applied.book,
            _env(RevertOp(op="revert", target_op_id=applied.accepted_envelopes[0].op_id), base_version=1),
            existing_op_ids={applied.accepted_envelopes[0].op_id},
            resolve_target=lambda tid: applied.accepted_envelopes[0] if tid == applied.accepted_envelopes[0].op_id else None,
            lease_state=_chapter_lease("ch-1"),
            lease_holder="test-agent",
            now=lambda: "2026-04-23T08:00:02Z",
        )
        restored = revert.book.chapters[0].blocks[0]
        assert isinstance(restored, Table)
        assert restored.multi_page is True
        assert restored.merge_record is not None
        assert restored.merge_record.segment_pages == [1, 2]
