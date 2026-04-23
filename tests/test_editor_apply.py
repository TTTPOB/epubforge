from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from epubforge.editor.apply import ApplyError, apply_envelope, apply_log
from epubforge.editor.leases import LeaseState
from epubforge.editor.memory import ChapterStatus, EditMemory, MemoryPatch
from epubforge.editor.ops import (
    CompactMarker,
    DeleteBlock,
    InsertBlock,
    MergeBlocks,
    MergeChapters,
    NoopOp,
    OpEnvelope,
    RelocateBlock,
    RevertOp,
    SetFootnoteFlag,
    SetHeadingId,
    SetHeadingLevel,
    SetRole,
    SetStyleClass,
    SetText,
    SplitBlock,
    SplitChapter,
    SplitMergedTable,
)
from epubforge.ir.semantic import Book, Chapter, Footnote, Heading, Paragraph, Provenance, Table, TableMergeRecord


def _book(prov: Callable[..., Provenance]) -> Book:
    return Book(
        version=0,
        initialized_at="2026-04-23T08:00:00Z",
        uid_seed="seed-1",
        title="Test Book",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter 1",
                blocks=[
                    Paragraph(uid="p-1", text="Alpha", provenance=prov()),
                    Heading(uid="h-1", text="Heading", level=2, id="sec-1", provenance=prov()),
                    Footnote(uid="fn-1", callout="①", text="Note", paired=False, orphan=False, provenance=prov()),
                ],
            )
        ],
    )


def _topology_book(prov: Callable[..., Provenance]) -> Book:
    return Book(
        version=0,
        initialized_at="2026-04-23T08:00:00Z",
        uid_seed="seed-2",
        title="Topology Book",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter 1",
                blocks=[
                    Paragraph(uid="p-1", text="Alpha", provenance=prov(1)),
                    Paragraph(uid="p-2", text="Beta", provenance=prov(1)),
                ],
            ),
            Chapter(
                uid="ch-2",
                title="Chapter 2",
                blocks=[
                    Paragraph(uid="p-3", text="Gamma", provenance=prov(2)),
                    Paragraph(uid="p-4", text="Delta", provenance=prov(2)),
                ],
            ),
        ],
    )


def _memory_for(book: Book) -> EditMemory:
    return EditMemory.create(
        book_id="book-1",
        updated_at="2026-04-23T08:00:00Z",
        updated_by="tester",
        chapter_uids=[chapter.uid for chapter in book.chapters if chapter.uid is not None],
    )


def _env(op, *, base_version: int, op_id: str | None = None, preconditions: list[dict[str, object]] | None = None) -> OpEnvelope:
    return OpEnvelope.model_validate(
        {
            "op_id": op_id or str(uuid4()),
            "ts": "2026-04-23T08:00:00Z",
            "agent_id": "agent-1",
            "base_version": base_version,
            "preconditions": preconditions or [],
            "op": op if isinstance(op, dict) else op.model_dump(mode="json"),
            "rationale": "test",
        }
    )


def test_apply_basic_path_increments_version(prov) -> None:
    result = apply_envelope(
        _book(prov),
        _env(SetText(op="set_text", block_uid="p-1", field="text", value="Beta"), base_version=0),
        now=lambda: "2026-04-23T08:00:01Z",
    )

    block = result.book.chapters[0].blocks[0]
    assert isinstance(block, Paragraph)
    assert block.text == "Beta"
    assert result.book.op_log_version == 1
    assert result.accepted_envelopes[0].applied_version == 1
    assert result.accepted_envelopes[0].applied_at == "2026-04-23T08:00:01Z"


def test_apply_rejects_duplicate_future_precondition_and_uid_collision(prov) -> None:
    env = _env(SetText(op="set_text", block_uid="p-1", field="text", value="Beta"), base_version=0)
    with pytest.raises(ApplyError, match="duplicate op_id"):
        apply_envelope(_book(prov), env, existing_op_ids={env.op_id})

    with pytest.raises(ApplyError, match="future-version rejection"):
        apply_envelope(_book(prov), _env(SetText(op="set_text", block_uid="p-1", field="text", value="Beta"), base_version=1))

    with pytest.raises(ApplyError, match="precondition failed"):
        apply_envelope(
            _book(prov),
            _env(
                SetText(op="set_text", block_uid="p-1", field="text", value="Beta"),
                base_version=0,
                preconditions=[{"kind": "field_equals", "block_uid": "p-1", "field": "text", "expected": "Gamma"}],
            ),
        )

    with pytest.raises(ApplyError, match="new block uid collision"):
        apply_envelope(
            _book(prov),
            _env(
                InsertBlock(
                    op="insert_block",
                    chapter_uid="ch-1",
                    after_uid="p-1",
                    block_kind="paragraph",
                    new_block_uid="p-1",
                    block_data={"text": "Inserted", "role": "body", "provenance": prov().model_dump(mode="json")},
                ),
                base_version=0,
            ),
        )


def test_noop_compact_marker_and_revert_semantics(prov) -> None:
    noop_result = apply_envelope(
        _book(prov),
        _env(NoopOp(op="noop", purpose="milestone"), base_version=0),
        now=lambda: "2026-04-23T08:00:01Z",
    )
    assert noop_result.book.op_log_version == 1

    compact_result = apply_envelope(
        noop_result.book,
        _env(
            CompactMarker(
                op="compact_marker",
                compacted_at_version=1,
                archive_path="log.archive/2026-04-23T08-00-00Z",
                archived_op_count=3,
            ),
            base_version=1,
        ),
        now=lambda: "2026-04-23T08:00:02Z",
    )
    assert compact_result.book.op_log_version == 1
    assert compact_result.accepted_envelopes[0].applied_version == 1

    insert_env = _env(
        InsertBlock(
            op="insert_block",
            chapter_uid="ch-1",
            after_uid="p-1",
            block_kind="paragraph",
            new_block_uid="p-2",
            block_data={"text": "Inserted", "role": "body", "provenance": prov().model_dump(mode="json")},
        ),
        base_version=1,
    )
    insert_result = apply_envelope(compact_result.book, insert_env, now=lambda: "2026-04-23T08:00:03Z")

    revert_env = _env(RevertOp(op="revert", target_op_id=insert_env.op_id), base_version=2)
    revert_result = apply_envelope(
        insert_result.book,
        revert_env,
        existing_op_ids={insert_env.op_id, noop_result.accepted_envelopes[0].op_id, compact_result.accepted_envelopes[0].op_id},
        reverted_target_op_ids=set(),
        resolve_target=lambda target_op_id: insert_result.accepted_envelopes[0] if target_op_id == insert_env.op_id else None,
        now=lambda: "2026-04-23T08:00:04Z",
    )

    assert len(revert_result.accepted_envelopes) == 2
    revert_request, inverse = revert_result.accepted_envelopes
    assert revert_request.applied_version == 2
    assert inverse.base_version == 2
    assert inverse.preconditions[0].kind == "block_exists"
    assert inverse.preconditions[0].block_uid == "p-2"
    assert revert_result.book.op_log_version == 3
    assert [block.uid for block in revert_result.book.chapters[0].blocks] == ["p-1", "h-1", "fn-1"]


def test_revert_target_effect_preconditions_block_later_edits(prov) -> None:
    insert_env = _env(
        InsertBlock(
            op="insert_block",
            chapter_uid="ch-1",
            after_uid="p-1",
            block_kind="paragraph",
            new_block_uid="p-2",
            block_data={"text": "Inserted", "role": "body", "provenance": prov().model_dump(mode="json")},
        ),
        base_version=0,
    )
    inserted = apply_envelope(_book(prov), insert_env, now=lambda: "2026-04-23T08:00:01Z")
    deleted = apply_envelope(
        inserted.book,
        _env(DeleteBlock(op="delete_block", block_uid="p-2"), base_version=1),
        existing_op_ids={insert_env.op_id},
        now=lambda: "2026-04-23T08:00:02Z",
    )

    with pytest.raises(ApplyError, match="precondition failed"):
        apply_envelope(
            deleted.book,
            _env(RevertOp(op="revert", target_op_id=insert_env.op_id), base_version=2),
            existing_op_ids={insert_env.op_id, deleted.accepted_envelopes[0].op_id},
            resolve_target=lambda target_op_id: inserted.accepted_envelopes[0] if target_op_id == insert_env.op_id else None,
            now=lambda: "2026-04-23T08:00:03Z",
        )


def test_apply_log_replays_inverse_and_skips_revert_request(prov, tmp_path: Path) -> None:
    baseline = _book(prov)
    insert_env = _env(
        InsertBlock(
            op="insert_block",
            chapter_uid="ch-1",
            after_uid="p-1",
            block_kind="paragraph",
            new_block_uid="p-2",
            block_data={"text": "Inserted", "role": "body", "provenance": prov().model_dump(mode="json")},
        ),
        base_version=0,
    )
    insert_result = apply_envelope(baseline, insert_env, now=lambda: "2026-04-23T08:00:01Z")
    revert_env = _env(RevertOp(op="revert", target_op_id=insert_env.op_id), base_version=1)
    revert_result = apply_envelope(
        insert_result.book,
        revert_env,
        existing_op_ids={insert_env.op_id},
        resolve_target=lambda target_op_id: insert_result.accepted_envelopes[0] if target_op_id == insert_env.op_id else None,
        now=lambda: "2026-04-23T08:00:02Z",
    )

    log_path = tmp_path / "edit_log.jsonl"
    lines = [env.model_dump_json() for env in (*insert_result.accepted_envelopes, *revert_result.accepted_envelopes)]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    replayed = apply_log(_book(prov), log_path)

    assert revert_result.accepted_envelopes[0].applied_version == 1
    assert revert_result.accepted_envelopes[1].applied_version == 2
    assert replayed.op_log_version == revert_result.accepted_envelopes[1].applied_version
    assert [block.uid for block in replayed.chapters[0].blocks] == ["p-1", "h-1", "fn-1"]


@pytest.mark.parametrize(
    ("op", "preconditions", "assertion"),
    [
        (
            SetRole(op="set_role", block_uid="p-1", value="epigraph"),
            [{"kind": "field_equals", "block_uid": "p-1", "field": "role", "expected": "body"}],
            lambda block: isinstance(block, Paragraph) and block.role == "body",
        ),
        (
            SetStyleClass(op="set_style_class", block_uid="p-1", value="intro"),
            [{"kind": "field_equals", "block_uid": "p-1", "field": "style_class", "expected": None}],
            lambda block: isinstance(block, Paragraph) and block.style_class is None,
        ),
        (
            SetText(op="set_text", block_uid="p-1", field="text", value="Beta"),
            [{"kind": "field_equals", "block_uid": "p-1", "field": "text", "expected": "Alpha"}],
            lambda block: isinstance(block, Paragraph) and block.text == "Alpha",
        ),
        (
            SetHeadingLevel(op="set_heading_level", block_uid="h-1", value=3),
            [{"kind": "field_equals", "block_uid": "h-1", "field": "level", "expected": 2}],
            lambda block: isinstance(block, Heading) and block.level == 2,
        ),
        (
            SetHeadingId(op="set_heading_id", block_uid="h-1", value="sec-2"),
            [{"kind": "field_equals", "block_uid": "h-1", "field": "id", "expected": "sec-1"}],
            lambda block: isinstance(block, Heading) and block.id == "sec-1",
        ),
        (
            SetFootnoteFlag(op="set_footnote_flag", block_uid="fn-1", paired=True),
            [
                {"kind": "field_equals", "block_uid": "fn-1", "field": "paired", "expected": False},
            ],
            lambda block: isinstance(block, Footnote) and block.paired is False and block.orphan is False,
        ),
    ],
)
def test_revert_supports_set_ops(prov, op, preconditions, assertion) -> None:
    applied = apply_envelope(_book(prov), _env(op, base_version=0, preconditions=preconditions), now=lambda: "2026-04-23T08:00:01Z")
    revert = apply_envelope(
        applied.book,
        _env(RevertOp(op="revert", target_op_id=applied.accepted_envelopes[0].op_id), base_version=1),
        existing_op_ids={applied.accepted_envelopes[0].op_id},
        resolve_target=lambda target_op_id: applied.accepted_envelopes[0] if target_op_id == applied.accepted_envelopes[0].op_id else None,
        now=lambda: "2026-04-23T08:00:02Z",
    )

    blocks = {block.uid: block for block in revert.book.chapters[0].blocks}
    target_uid = getattr(op, "block_uid")
    assert assertion(blocks[target_uid])
    assert revert.accepted_envelopes[1].base_version == 1


def test_revert_supports_split_block_and_at_sentence_max_splits(prov) -> None:
    book = _book(prov)
    book.chapters[0].blocks[0] = Paragraph(
        uid="p-1",
        text="Alpha. Beta. Gamma. Delta.",
        provenance=prov(),
    )
    split = SplitBlock(
        op="split_block",
        block_uid="p-1",
        strategy="at_sentence",
        max_splits=2,
        new_block_uids=["p-2", "p-3"],
    )
    applied = apply_envelope(
        book,
        _env(
            split,
            base_version=0,
            preconditions=[{"kind": "field_equals", "block_uid": "p-1", "field": "text", "expected": "Alpha. Beta. Gamma. Delta."}],
        ),
        now=lambda: "2026-04-23T08:00:01Z",
    )

    texts = [block.text for block in applied.book.chapters[0].blocks if isinstance(block, Paragraph)]
    assert texts[:3] == ["Alpha. ", "Beta. ", "Gamma. Delta."]

    revert = apply_envelope(
        applied.book,
        _env(RevertOp(op="revert", target_op_id=applied.accepted_envelopes[0].op_id), base_version=1),
        existing_op_ids={applied.accepted_envelopes[0].op_id},
        resolve_target=lambda target_op_id: applied.accepted_envelopes[0] if target_op_id == applied.accepted_envelopes[0].op_id else None,
        now=lambda: "2026-04-23T08:00:02Z",
    )

    blocks = revert.book.chapters[0].blocks
    assert [block.uid for block in blocks] == ["p-1", "h-1", "fn-1"]
    restored = blocks[0]
    assert isinstance(restored, Paragraph)
    assert restored.text == "Alpha. Beta. Gamma. Delta."


def test_revert_rejects_merge_blocks_even_with_original_blocks_snapshot(prov) -> None:
    merge = MergeBlocks(
        op="merge_blocks",
        block_uids=["p-1", "p-2"],
        join="concat",
        original_blocks=[
            {"kind": "paragraph", "uid": "p-1", "text": "Alpha", "role": "body", "provenance": prov().model_dump(mode="json")},
            {"kind": "paragraph", "uid": "p-2", "text": "Beta", "role": "body", "provenance": prov().model_dump(mode="json")},
        ],
    )
    book = _book(prov)
    book.chapters[0].blocks.insert(1, Paragraph(uid="p-2", text="Beta", provenance=prov()))
    applied = apply_envelope(book, _env(merge, base_version=0), now=lambda: "2026-04-23T08:00:01Z")

    assert applied.accepted_envelopes[0].irreversible is True
    with pytest.raises(ApplyError, match="irreversible"):
        apply_envelope(
            applied.book,
            _env(RevertOp(op="revert", target_op_id=applied.accepted_envelopes[0].op_id), base_version=1),
            existing_op_ids={applied.accepted_envelopes[0].op_id},
            resolve_target=lambda target_op_id: applied.accepted_envelopes[0] if target_op_id == applied.accepted_envelopes[0].op_id else None,
            now=lambda: "2026-04-23T08:00:02Z",
        )


def test_apply_rejects_intra_chapter_op_without_matching_lease(prov) -> None:
    with pytest.raises(ApplyError, match="chapter lease"):
        apply_envelope(
            _book(prov),
            _env(SetText(op="set_text", block_uid="p-1", field="text", value="Beta"), base_version=0),
            lease_state=LeaseState(),
            now=lambda: "2026-04-23T08:00:01Z",
        )


def test_apply_rejects_topology_op_without_book_lock(prov) -> None:
    book = _topology_book(prov)
    memory = _memory_for(book)
    op = MergeChapters(
        op="merge_chapters",
        source_chapter_uids=["ch-1", "ch-2"],
        new_title="Merged",
        new_chapter_uid="ch-merged",
        sections=[
            {"text": "Section 1", "new_block_uid": "h-merge-1"},
            {"text": "Section 2", "new_block_uid": "h-merge-2"},
        ],
    )

    with pytest.raises(ApplyError, match="book-exclusive"):
        apply_envelope(
            book,
            _env(op, base_version=0),
            lease_state=LeaseState(),
            memory=memory,
            now=lambda: "2026-04-23T08:00:01Z",
        )


def test_merge_chapters_migrates_chapter_status_into_new_uid(prov) -> None:
    book = _topology_book(prov)
    memory = _memory_for(book).model_copy(
        update={
            "chapter_status": {
                "ch-1": ChapterStatus(
                    chapter_uid="ch-1",
                    read_passes=2,
                    last_reader="reader-a",
                    issues_found=3,
                    issues_fixed=1,
                    notes="alpha notes",
                ),
                "ch-2": ChapterStatus(
                    chapter_uid="ch-2",
                    read_passes=5,
                    last_reader="reader-b",
                    issues_found=4,
                    issues_fixed=2,
                    notes="beta notes",
                ),
            }
        }
    )
    leases = LeaseState()
    assert leases.acquire_book_exclusive("agent-1", "topology_op", now="2026-04-23T08:00:00Z") is not None

    result = apply_envelope(
        book,
        _env(
            MergeChapters(
                op="merge_chapters",
                source_chapter_uids=["ch-1", "ch-2"],
                new_title="Merged",
                new_chapter_uid="ch-merged",
                sections=[
                    {"text": "Section 1", "new_block_uid": "h-merge-1"},
                    {"text": "Section 2", "new_block_uid": "h-merge-2"},
                ],
            ),
            base_version=0,
        ),
        lease_state=leases,
        memory=memory,
        now=lambda: "2026-04-23T08:00:01Z",
    )

    assert [chapter.uid for chapter in result.book.chapters] == ["ch-merged"]
    assert result.memory is not None
    assert set(result.memory.chapter_status) == {"ch-merged"}
    merged = result.memory.chapter_status["ch-merged"]
    assert merged.read_passes == 5
    assert merged.last_reader == "reader-b"
    assert merged.issues_found == 7
    assert merged.issues_fixed == 3
    assert merged.notes == "alpha notes\nbeta notes"


def test_split_chapter_preserves_old_status_and_creates_fresh_new_status(prov) -> None:
    book = _topology_book(prov)
    memory = _memory_for(book).model_copy(
        update={
            "chapter_status": {
                "ch-1": ChapterStatus(
                    chapter_uid="ch-1",
                    read_passes=3,
                    last_reader="reader-a",
                    issues_found=2,
                    issues_fixed=1,
                    notes="existing status",
                ),
                "ch-2": ChapterStatus(chapter_uid="ch-2", read_passes=1),
            }
        }
    )
    leases = LeaseState()
    assert leases.acquire_book_exclusive("agent-1", "topology_op", now="2026-04-23T08:00:00Z") is not None

    result = apply_envelope(
        book,
        _env(
            SplitChapter(
                op="split_chapter",
                chapter_uid="ch-1",
                split_at_block_uid="p-2",
                new_chapter_title="Chapter 1B",
                new_chapter_uid="ch-1b",
            ),
            base_version=0,
        ),
        lease_state=leases,
        memory=memory,
        now=lambda: "2026-04-23T08:00:01Z",
    )

    assert [chapter.uid for chapter in result.book.chapters] == ["ch-1", "ch-1b", "ch-2"]
    assert [block.uid for block in result.book.chapters[0].blocks] == ["p-1"]
    assert [block.uid for block in result.book.chapters[1].blocks] == ["p-2"]
    assert result.memory is not None
    assert result.memory.chapter_status["ch-1"].read_passes == 3
    new_status = result.memory.chapter_status["ch-1b"]
    assert new_status.read_passes == 0
    assert new_status.issues_found == 0
    assert new_status.notes == "split from ch-1"


def test_relocate_block_keeps_existing_chapter_uids_and_status_map(prov) -> None:
    book = _topology_book(prov)
    memory = _memory_for(book)
    leases = LeaseState()
    assert leases.acquire_book_exclusive("agent-1", "topology_op", now="2026-04-23T08:00:00Z") is not None

    result = apply_envelope(
        book,
        _env(
            RelocateBlock(
                op="relocate_block",
                block_uid="p-2",
                target_chapter_uid="ch-2",
                after_uid="p-3",
            ),
            base_version=0,
        ),
        lease_state=leases,
        memory=memory,
        now=lambda: "2026-04-23T08:00:01Z",
    )

    assert [chapter.uid for chapter in result.book.chapters] == ["ch-1", "ch-2"]
    assert [block.uid for block in result.book.chapters[0].blocks] == ["p-1"]
    assert [block.uid for block in result.book.chapters[1].blocks] == ["p-3", "p-2", "p-4"]
    assert result.memory is not None
    assert set(result.memory.chapter_status) == {"ch-1", "ch-2"}


# ---------------------------------------------------------------------------
# §1.5b SplitMergedTable apply tests
# ---------------------------------------------------------------------------


def _table_book(prov: Callable[..., Provenance]) -> Book:
    """Book with a multi_page Table that has two merged segments."""
    merged_html = (
        "<table>"
        "<thead><tr><th>Col</th></tr></thead>"
        "<tbody><tr><td>Row A</td></tr></tbody>"
        "<tbody><tr><td>Row B</td></tr></tbody>"
        "</table>"
    )
    return Book(
        version=0,
        initialized_at="2026-04-23T08:00:00Z",
        uid_seed="seed-tbl",
        title="Table Book",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter 1",
                blocks=[
                    Paragraph(uid="p-before", text="Before table.", provenance=prov(2)),
                    Table(
                        uid="tbl-merged",
                        html=merged_html,
                        multi_page=True,
                        provenance=prov(3),
                        merge_record=TableMergeRecord(
                            segment_html=[
                                "<tr><td>Row A</td></tr>",
                                "<tr><td>Row B</td></tr>",
                            ],
                            segment_pages=[3, 4],
                            segment_order=[0, 1],
                            column_widths=[1, 1],
                        ),
                    ),
                    Paragraph(uid="p-after", text="After table.", provenance=prov(4)),
                ],
            )
        ],
    )


def _split_merged_table_op(block_uid: str = "tbl-merged") -> dict[str, object]:
    return {
        "op": "split_merged_table",
        "block_uid": block_uid,
        "segment_html": [
            "<table><tbody><tr><td>Row A</td></tr></tbody></table>",
            "<table><tbody><tr><td>Row B</td></tr></tbody></table>",
        ],
        "segment_pages": [3, 4],
        "multi_page_was": True,
    }


class TestSplitMergedTableApply:
    """Apply-layer tests for SplitMergedTable op (PR-D §1.5b)."""

    def test_split_replaces_merged_block_with_two_segment_blocks(self, prov) -> None:
        book = _table_book(prov)
        result = apply_envelope(
            book,
            _env(_split_merged_table_op(), base_version=0),
            now=lambda: "2026-04-23T08:00:01Z",
        )
        blocks = result.book.chapters[0].blocks
        assert len(blocks) == 4  # p-before, seg0, seg1, p-after
        assert isinstance(blocks[0], Paragraph)
        assert isinstance(blocks[1], Table)
        assert isinstance(blocks[2], Table)
        assert isinstance(blocks[3], Paragraph)

    def test_split_segment_html_and_pages_are_correct(self, prov) -> None:
        book = _table_book(prov)
        result = apply_envelope(
            book,
            _env(_split_merged_table_op(), base_version=0),
            now=lambda: "2026-04-23T08:00:01Z",
        )
        seg0: Table = result.book.chapters[0].blocks[1]  # type: ignore[assignment]
        seg1: Table = result.book.chapters[0].blocks[2]  # type: ignore[assignment]
        assert seg0.html == "<table><tbody><tr><td>Row A</td></tr></tbody></table>"
        assert seg0.provenance.page == 3
        assert seg1.html == "<table><tbody><tr><td>Row B</td></tr></tbody></table>"
        assert seg1.provenance.page == 4

    def test_split_new_blocks_have_unique_runtime_uids(self, prov) -> None:
        """New block uids are generated at apply time; they must not equal the original."""
        book = _table_book(prov)
        result = apply_envelope(
            book,
            _env(_split_merged_table_op(), base_version=0),
            now=lambda: "2026-04-23T08:00:01Z",
        )
        seg0: Table = result.book.chapters[0].blocks[1]  # type: ignore[assignment]
        seg1: Table = result.book.chapters[0].blocks[2]  # type: ignore[assignment]
        assert seg0.uid != "tbl-merged"
        assert seg1.uid != "tbl-merged"
        assert seg0.uid != seg1.uid
        assert seg0.uid is not None
        assert seg1.uid is not None

    def test_split_does_not_assert_original_uid_restored(self, prov) -> None:
        """Regression: apply must not try to reuse pre-merge constituent uids."""
        book = _table_book(prov)
        result = apply_envelope(
            book,
            _env(_split_merged_table_op(), base_version=0),
            now=lambda: "2026-04-23T08:00:01Z",
        )
        seg0: Table = result.book.chapters[0].blocks[1]  # type: ignore[assignment]
        seg1: Table = result.book.chapters[0].blocks[2]  # type: ignore[assignment]
        # Verify neither segment carries the constituent original uid assumption:
        assert seg0.uid not in ("tbl-seg-0-pre-merge", "tbl-seg-1-pre-merge")
        assert seg1.uid not in ("tbl-seg-0-pre-merge", "tbl-seg-1-pre-merge")

    def test_split_first_segment_is_not_continuation(self, prov) -> None:
        book = _table_book(prov)
        result = apply_envelope(
            book,
            _env(_split_merged_table_op(), base_version=0),
            now=lambda: "2026-04-23T08:00:01Z",
        )
        seg0: Table = result.book.chapters[0].blocks[1]  # type: ignore[assignment]
        seg1: Table = result.book.chapters[0].blocks[2]  # type: ignore[assignment]
        assert seg0.continuation is False
        assert seg1.continuation is True

    def test_split_sets_multi_page_false_on_new_blocks(self, prov) -> None:
        book = _table_book(prov)
        result = apply_envelope(
            book,
            _env(_split_merged_table_op(), base_version=0),
            now=lambda: "2026-04-23T08:00:01Z",
        )
        seg0: Table = result.book.chapters[0].blocks[1]  # type: ignore[assignment]
        seg1: Table = result.book.chapters[0].blocks[2]  # type: ignore[assignment]
        assert seg0.multi_page is False
        assert seg1.multi_page is False

    def test_split_requires_target_block_to_be_multi_page(self, prov) -> None:
        """Applying to a non-multi_page Table must raise ApplyError."""
        book = _table_book(prov)
        # Override the merged table with a normal (non-multi_page) table.
        book.chapters[0].blocks[1] = Table(
            uid="tbl-merged",
            html="<table><tbody><tr><td>X</td></tr></tbody></table>",
            multi_page=False,
            provenance=prov(3),
        )
        with pytest.raises(ApplyError, match="not a multi_page Table"):
            apply_envelope(
                book,
                _env(_split_merged_table_op(), base_version=0),
                now=lambda: "2026-04-23T08:00:01Z",
            )

    def test_split_requires_target_block_to_be_a_table(self, prov) -> None:
        """Applying to a Paragraph must raise ApplyError."""
        book = _table_book(prov)
        with pytest.raises(ApplyError, match="requires a Table block"):
            apply_envelope(
                book,
                _env(_split_merged_table_op(block_uid="p-before"), base_version=0),
                now=lambda: "2026-04-23T08:00:01Z",
            )

    def test_split_increments_book_version(self, prov) -> None:
        book = _table_book(prov)
        result = apply_envelope(
            book,
            _env(_split_merged_table_op(), base_version=0),
            now=lambda: "2026-04-23T08:00:01Z",
        )
        assert result.book.op_log_version == 1

    def test_split_three_segments_produces_three_blocks(self, prov) -> None:
        book = _table_book(prov)
        op = {
            "op": "split_merged_table",
            "block_uid": "tbl-merged",
            "segment_html": [
                "<table><tbody><tr><td>A</td></tr></tbody></table>",
                "<table><tbody><tr><td>B</td></tr></tbody></table>",
                "<table><tbody><tr><td>C</td></tr></tbody></table>",
            ],
            "segment_pages": [3, 4, 5],
            "multi_page_was": True,
        }
        result = apply_envelope(
            book,
            _env(op, base_version=0),
            now=lambda: "2026-04-23T08:00:01Z",
        )
        blocks = result.book.chapters[0].blocks
        # p-before, seg0, seg1, seg2, p-after
        assert len(blocks) == 5
        for idx in range(1, 4):
            seg = blocks[idx]
            assert isinstance(seg, Table)
            assert seg.multi_page is False


# ---------------------------------------------------------------------------
# §1.6b/c memory_patches wiring tests (PR-F)
# ---------------------------------------------------------------------------


def test_apply_queue_merges_memory_patch_via_merge_edit_memory(prov) -> None:
    """Envelope with a valid memory_patches entry must fold the patch into working_memory."""
    book = _book(prov)
    memory = _memory_for(book)
    patch = MemoryPatch(
        chapter_status=[
            ChapterStatus(
                chapter_uid="ch-1",
                read_passes=3,
                last_reader="test-agent",
                issues_found=2,
                issues_fixed=1,
                notes="patched note",
            )
        ]
    )
    env = OpEnvelope.model_validate(
        {
            "op_id": str(uuid4()),
            "ts": "2026-04-23T08:00:00Z",
            "agent_id": "agent-1",
            "base_version": 0,
            "preconditions": [],
            "op": {"op": "set_text", "block_uid": "p-1", "field": "text", "value": "Patched"},
            "rationale": "test memory patch wiring",
            "memory_patches": [patch.model_dump(mode="json")],
        }
    )

    result = apply_envelope(
        book,
        env,
        memory=memory,
        now=lambda: "2026-04-23T08:00:01Z",
    )

    # Book mutation landed.
    block = result.book.chapters[0].blocks[0]
    assert isinstance(block, Paragraph)
    assert block.text == "Patched"
    assert result.book.op_log_version == 1

    # Accepted log grew by one.
    assert len(result.accepted_envelopes) == 1
    assert result.accepted_envelopes[0].applied_version == 1

    # Memory on the result reflects the patched chapter_status.
    assert result.memory is not None
    ch_status = result.memory.chapter_status.get("ch-1")
    assert ch_status is not None
    assert ch_status.read_passes == 3
    assert ch_status.last_reader == "test-agent"
    assert ch_status.notes == "patched note"


def test_apply_queue_rejects_envelope_when_memory_merge_fails(prov) -> None:
    """If merge_edit_memory raises, apply_envelope must raise ApplyError; book/memory unchanged."""
    book = _book(prov)
    memory = _memory_for(book)
    env = OpEnvelope.model_validate(
        {
            "op_id": str(uuid4()),
            "ts": "2026-04-23T08:00:00Z",
            "agent_id": "agent-1",
            "base_version": 0,
            "preconditions": [],
            "op": {"op": "set_text", "block_uid": "p-1", "field": "text", "value": "Should not land"},
            "rationale": "test memory merge failure",
            "memory_patches": [MemoryPatch().model_dump(mode="json")],
        }
    )

    with patch("epubforge.editor.apply.merge_edit_memory", side_effect=ValueError("simulated merge failure")):
        with pytest.raises(ApplyError) as exc_info:
            apply_envelope(
                book,
                env,
                memory=memory,
                now=lambda: "2026-04-23T08:00:01Z",
            )

    # The error reason must mention the memory failure.
    assert "memory merge failed" in exc_info.value.reason
    assert "simulated merge failure" in exc_info.value.reason

    # Book was not mutated (still version 0, text still "Alpha").
    assert book.op_log_version == 0
    block = book.chapters[0].blocks[0]
    assert isinstance(block, Paragraph)
    assert block.text == "Alpha"

    # Memory was not mutated (no chapter_status changes).
    assert memory.chapter_status.get("ch-1") is not None
    assert memory.chapter_status["ch-1"].read_passes == 0
