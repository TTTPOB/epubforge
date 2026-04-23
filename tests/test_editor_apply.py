from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from epubforge.editor.apply import ApplyError, apply_envelope, apply_log
from epubforge.editor.leases import LeaseState
from epubforge.editor.memory import ChapterStatus, EditMemory
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
)
from epubforge.ir.semantic import Book, Chapter, Footnote, Heading, Paragraph, Provenance


def _prov(page: int = 1) -> Provenance:
    return Provenance(page=page, source="passthrough")


def _book() -> Book:
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
                    Paragraph(uid="p-1", text="Alpha", provenance=_prov()),
                    Heading(uid="h-1", text="Heading", level=2, id="sec-1", provenance=_prov()),
                    Footnote(uid="fn-1", callout="①", text="Note", paired=False, orphan=False, provenance=_prov()),
                ],
            )
        ],
    )


def _topology_book() -> Book:
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
                    Paragraph(uid="p-1", text="Alpha", provenance=_prov(1)),
                    Paragraph(uid="p-2", text="Beta", provenance=_prov(1)),
                ],
            ),
            Chapter(
                uid="ch-2",
                title="Chapter 2",
                blocks=[
                    Paragraph(uid="p-3", text="Gamma", provenance=_prov(2)),
                    Paragraph(uid="p-4", text="Delta", provenance=_prov(2)),
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


def test_apply_basic_path_increments_version() -> None:
    result = apply_envelope(
        _book(),
        _env(SetText(op="set_text", block_uid="p-1", field="text", value="Beta"), base_version=0),
        now=lambda: "2026-04-23T08:00:01Z",
    )

    block = result.book.chapters[0].blocks[0]
    assert isinstance(block, Paragraph)
    assert block.text == "Beta"
    assert result.book.version == 1
    assert result.accepted_envelopes[0].applied_version == 1
    assert result.accepted_envelopes[0].applied_at == "2026-04-23T08:00:01Z"


def test_apply_rejects_duplicate_future_precondition_and_uid_collision() -> None:
    env = _env(SetText(op="set_text", block_uid="p-1", field="text", value="Beta"), base_version=0)
    with pytest.raises(ApplyError, match="duplicate op_id"):
        apply_envelope(_book(), env, existing_op_ids={env.op_id})

    with pytest.raises(ApplyError, match="future-version rejection"):
        apply_envelope(_book(), _env(SetText(op="set_text", block_uid="p-1", field="text", value="Beta"), base_version=1))

    with pytest.raises(ApplyError, match="precondition failed"):
        apply_envelope(
            _book(),
            _env(
                SetText(op="set_text", block_uid="p-1", field="text", value="Beta"),
                base_version=0,
                preconditions=[{"kind": "field_equals", "block_uid": "p-1", "field": "text", "expected": "Gamma"}],
            ),
        )

    with pytest.raises(ApplyError, match="new block uid collision"):
        apply_envelope(
            _book(),
            _env(
                InsertBlock(
                    op="insert_block",
                    chapter_uid="ch-1",
                    after_uid="p-1",
                    block_kind="paragraph",
                    new_block_uid="p-1",
                    block_data={"text": "Inserted", "role": "body", "provenance": _prov().model_dump(mode="json")},
                ),
                base_version=0,
            ),
        )


def test_noop_compact_marker_and_revert_semantics() -> None:
    noop_result = apply_envelope(
        _book(),
        _env(NoopOp(op="noop", purpose="milestone"), base_version=0),
        now=lambda: "2026-04-23T08:00:01Z",
    )
    assert noop_result.book.version == 1

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
    assert compact_result.book.version == 1
    assert compact_result.accepted_envelopes[0].applied_version == 1

    insert_env = _env(
        InsertBlock(
            op="insert_block",
            chapter_uid="ch-1",
            after_uid="p-1",
            block_kind="paragraph",
            new_block_uid="p-2",
            block_data={"text": "Inserted", "role": "body", "provenance": _prov().model_dump(mode="json")},
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
    assert revert_result.book.version == 3
    assert [block.uid for block in revert_result.book.chapters[0].blocks] == ["p-1", "h-1", "fn-1"]


def test_revert_target_effect_preconditions_block_later_edits() -> None:
    insert_env = _env(
        InsertBlock(
            op="insert_block",
            chapter_uid="ch-1",
            after_uid="p-1",
            block_kind="paragraph",
            new_block_uid="p-2",
            block_data={"text": "Inserted", "role": "body", "provenance": _prov().model_dump(mode="json")},
        ),
        base_version=0,
    )
    inserted = apply_envelope(_book(), insert_env, now=lambda: "2026-04-23T08:00:01Z")
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


def test_apply_log_replays_inverse_and_skips_revert_request(tmp_path: Path) -> None:
    baseline = _book()
    insert_env = _env(
        InsertBlock(
            op="insert_block",
            chapter_uid="ch-1",
            after_uid="p-1",
            block_kind="paragraph",
            new_block_uid="p-2",
            block_data={"text": "Inserted", "role": "body", "provenance": _prov().model_dump(mode="json")},
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

    replayed = apply_log(_book(), log_path)

    assert revert_result.accepted_envelopes[0].applied_version == 1
    assert revert_result.accepted_envelopes[1].applied_version == 2
    assert replayed.version == revert_result.accepted_envelopes[1].applied_version
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
def test_revert_supports_set_ops(op, preconditions, assertion) -> None:
    applied = apply_envelope(_book(), _env(op, base_version=0, preconditions=preconditions), now=lambda: "2026-04-23T08:00:01Z")
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


def test_revert_supports_split_block_and_at_sentence_max_splits() -> None:
    book = _book()
    book.chapters[0].blocks[0] = Paragraph(
        uid="p-1",
        text="Alpha. Beta. Gamma. Delta.",
        provenance=_prov(),
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


def test_revert_rejects_merge_blocks_even_with_original_blocks_snapshot() -> None:
    merge = MergeBlocks(
        op="merge_blocks",
        block_uids=["p-1", "p-2"],
        join="concat",
        original_blocks=[
            {"kind": "paragraph", "uid": "p-1", "text": "Alpha", "role": "body", "provenance": _prov().model_dump(mode="json")},
            {"kind": "paragraph", "uid": "p-2", "text": "Beta", "role": "body", "provenance": _prov().model_dump(mode="json")},
        ],
    )
    book = _book()
    book.chapters[0].blocks.insert(1, Paragraph(uid="p-2", text="Beta", provenance=_prov()))
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


def test_apply_rejects_intra_chapter_op_without_matching_lease() -> None:
    with pytest.raises(ApplyError, match="chapter lease"):
        apply_envelope(
            _book(),
            _env(SetText(op="set_text", block_uid="p-1", field="text", value="Beta"), base_version=0),
            lease_state=LeaseState(),
            now=lambda: "2026-04-23T08:00:01Z",
        )


def test_apply_rejects_topology_op_without_book_lock() -> None:
    book = _topology_book()
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


def test_merge_chapters_migrates_chapter_status_into_new_uid() -> None:
    book = _topology_book()
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


def test_split_chapter_preserves_old_status_and_creates_fresh_new_status() -> None:
    book = _topology_book()
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


def test_relocate_block_keeps_existing_chapter_uids_and_status_map() -> None:
    book = _topology_book()
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
