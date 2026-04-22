from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from epubforge.editor.apply import ApplyError
from epubforge.editor.log import (
    append_rejected_log,
    apply_and_log,
    compact_log,
    find_envelope,
    known_op_ids,
    read_current_log,
    resolve_edit_log_paths,
)
from epubforge.editor.ops import InsertBlock, OpEnvelope, RevertOp, SetText
from epubforge.ir.semantic import Book, Chapter, Paragraph, Provenance


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
                blocks=[Paragraph(uid="p-1", text="Alpha", provenance=_prov())],
            )
        ],
    )


def _env(op, *, base_version: int, op_id: str | None = None) -> OpEnvelope:
    return OpEnvelope.model_validate(
        {
            "op_id": op_id or str(uuid4()),
            "ts": "2026-04-23T08:00:00Z",
            "agent_id": "agent-1",
            "base_version": base_version,
            "preconditions": [],
            "op": op if isinstance(op, dict) else op.model_dump(mode="json"),
            "rationale": "test",
        }
    )


def test_apply_and_log_writes_accepted_and_rejected_entries(tmp_path: Path) -> None:
    edit_dir = tmp_path / "edit_state"
    result = apply_and_log(
        _book(),
        edit_dir,
        _env(SetText(op="set_text", block_uid="p-1", field="text", value="Beta"), base_version=0),
        now="2026-04-23T08:00:01Z",
    )

    current_log = read_current_log(edit_dir)
    assert len(current_log) == 1
    assert current_log[0].applied_version == 1
    assert result.book.chapters[0].blocks[0].text == "Beta"  # type: ignore[union-attr]

    with pytest.raises(ApplyError, match="future-version rejection"):
        apply_and_log(
            result.book,
            edit_dir,
            _env(SetText(op="set_text", block_uid="p-1", field="text", value="Gamma"), base_version=99),
            now="2026-04-23T08:00:02Z",
        )

    rejected_path = resolve_edit_log_paths(edit_dir).rejected
    rejected = rejected_path.read_text(encoding="utf-8")
    assert "future-version rejection" in rejected
    assert len(read_current_log(edit_dir)) == 1


def test_compact_archives_log_builds_index_and_keeps_revertable_history(tmp_path: Path) -> None:
    edit_dir = tmp_path / "edit_state"
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
    inserted = apply_and_log(_book(), edit_dir, insert_env, now="2026-04-23T08:00:01Z")

    marker = compact_log(edit_dir, inserted.book, ts="2026-04-23T08:00:02Z")
    current_log = read_current_log(edit_dir)
    assert len(current_log) == 1
    assert current_log[0].op_id == marker.op_id

    paths = resolve_edit_log_paths(edit_dir)
    located = find_envelope(edit_dir, insert_env.op_id)
    assert located is not None
    assert located.archive_path is not None
    assert (located.archive_path / "book.json").exists()
    assert (located.archive_path / "edit_log.jsonl").exists()
    assert insert_env.op_id in known_op_ids(edit_dir)
    assert paths.index.read_text(encoding="utf-8")

    revert_env = _env(RevertOp(op="revert", target_op_id=insert_env.op_id), base_version=1)
    reverted = apply_and_log(inserted.book, edit_dir, revert_env, now="2026-04-23T08:00:03Z")

    assert reverted.book.version == 2
    assert [block.uid for block in reverted.book.chapters[0].blocks] == ["p-1"]
    current_log = read_current_log(edit_dir)
    assert [entry.op.op for entry in current_log] == ["compact_marker", "revert", "delete_block"]
    backrefs = paths.revert_backrefs.read_text(encoding="utf-8")
    assert insert_env.op_id in backrefs
    assert "reverted_by" not in paths.current.read_text(encoding="utf-8")
    assert "reverted_by" not in (located.archive_path / "edit_log.jsonl").read_text(encoding="utf-8")
