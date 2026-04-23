"""Append-only editor log helpers."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from epubforge.editor.apply import ApplyError, ApplyResult, RevertBackref, apply_envelope
from epubforge.editor.ops import CompactMarker, OpEnvelope
from epubforge.ir.semantic import Book


CURRENT_LOG = "edit_log.jsonl"
REJECTED_LOG = "edit_log.rejected.jsonl"
INDEX_LOG = "edit_log.index.jsonl"
REVERT_BACKREF_LOG = "edit_log.revert_backrefs.jsonl"
ARCHIVE_DIR = "log.archive"
BOOK_FILE = "book.json"


class RejectedLogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_id: str
    ts: str
    reason: str
    envelope: OpEnvelope


class IndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_id: str
    archive_path: str


class RevertBackrefEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_op_id: str
    revert_op_id: str
    inverse_op_id: str
    ts: str


@dataclass(frozen=True)
class EditLogPaths:
    root: Path
    current: Path
    rejected: Path
    index: Path
    revert_backrefs: Path
    archive_root: Path


@dataclass(frozen=True)
class LocatedEnvelope:
    envelope: OpEnvelope
    archive_path: Path | None = None


def resolve_edit_log_paths(path: str | Path) -> EditLogPaths:
    candidate = Path(path).expanduser()
    if candidate.name == CURRENT_LOG:
        current = candidate
        root = candidate.parent
    elif candidate.is_dir() or not candidate.exists():
        root = candidate
        current = root / CURRENT_LOG
    else:
        raise ValueError(f"expected edit-state dir or {CURRENT_LOG}, got {candidate}")
    return EditLogPaths(
        root=root,
        current=current,
        rejected=root / REJECTED_LOG,
        index=root / INDEX_LOG,
        revert_backrefs=root / REVERT_BACKREF_LOG,
        archive_root=root / ARCHIVE_DIR,
    )


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False))
        fh.write("\n")


def _iter_jsonl(path: Path) -> Iterable[dict[str, object]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def read_current_log(path: str | Path) -> list[OpEnvelope]:
    paths = resolve_edit_log_paths(path)
    return [OpEnvelope.model_validate(item) for item in _iter_jsonl(paths.current)]


def append_accepted_log(path: str | Path, envelope: OpEnvelope) -> None:
    if envelope.applied_version is None or envelope.applied_at is None:
        raise ValueError("accepted envelopes must have applied_version and applied_at")
    paths = resolve_edit_log_paths(path)
    _append_jsonl(paths.current, envelope.model_dump(mode="json"))


def append_rejected_log(path: str | Path, envelope: OpEnvelope, *, reason: str, rejected_at: str) -> None:
    paths = resolve_edit_log_paths(path)
    entry = RejectedLogEntry(op_id=envelope.op_id, ts=rejected_at, reason=reason, envelope=envelope)
    _append_jsonl(paths.rejected, entry.model_dump(mode="json"))


def append_revert_backref(path: str | Path, backref: RevertBackref) -> None:
    paths = resolve_edit_log_paths(path)
    entry = RevertBackrefEntry(**backref.__dict__)
    _append_jsonl(paths.revert_backrefs, entry.model_dump(mode="json"))


def known_op_ids(path: str | Path) -> set[str]:
    paths = resolve_edit_log_paths(path)
    op_ids = {env.op_id for env in read_current_log(paths.root)}
    op_ids.update(IndexEntry.model_validate(item).op_id for item in _iter_jsonl(paths.index))
    return op_ids


def reverted_target_op_ids(path: str | Path) -> set[str]:
    paths = resolve_edit_log_paths(path)
    return {RevertBackrefEntry.model_validate(item).target_op_id for item in _iter_jsonl(paths.revert_backrefs)}


def find_envelope(path: str | Path, op_id: str) -> LocatedEnvelope | None:
    paths = resolve_edit_log_paths(path)
    for envelope in read_current_log(paths.root):
        if envelope.op_id == op_id:
            return LocatedEnvelope(envelope=envelope)
    for item in _iter_jsonl(paths.index):
        entry = IndexEntry.model_validate(item)
        if entry.op_id != op_id:
            continue
        archive_path = paths.root / entry.archive_path
        for archived in _iter_jsonl(archive_path / CURRENT_LOG):
            envelope = OpEnvelope.model_validate(archived)
            if envelope.op_id == op_id:
                return LocatedEnvelope(envelope=envelope, archive_path=archive_path)
    return None


def apply_and_log(book: Book, path: str | Path, envelope: OpEnvelope, *, now: str | None = None) -> ApplyResult:
    paths = resolve_edit_log_paths(path)
    timestamp = now or envelope.applied_at or envelope.ts
    try:
        result = apply_envelope(
            book,
            envelope,
            existing_op_ids=known_op_ids(paths.root),
            reverted_target_op_ids=reverted_target_op_ids(paths.root),
            resolve_target=lambda op_id: (located.envelope if (located := find_envelope(paths.root, op_id)) else None),
            now=lambda: timestamp,
        )
    except ApplyError as exc:
        append_rejected_log(paths.root, envelope, reason=exc.reason, rejected_at=timestamp)
        raise

    for accepted in result.accepted_envelopes:
        append_accepted_log(paths.root, accepted)
    if result.revert_backref is not None:
        append_revert_backref(paths.root, result.revert_backref)
    return result


def compact_log(path: str | Path, book: Book, *, ts: str) -> OpEnvelope:
    paths = resolve_edit_log_paths(path)
    archive_name = ts.replace(":", "-")
    archive_path = paths.archive_root / archive_name
    archive_path.mkdir(parents=True, exist_ok=True)

    from epubforge.editor.state import atomic_write_text  # lazy: avoid circular import

    current_log = read_current_log(paths.root)
    if paths.current.exists():
        shutil.copyfile(paths.current, archive_path / CURRENT_LOG)
    else:
        atomic_write_text(archive_path / CURRENT_LOG, "")
    atomic_write_text(archive_path / BOOK_FILE, book.model_dump_json(indent=2))

    relative_archive = archive_path.relative_to(paths.root)
    for envelope in current_log:
        _append_jsonl(
            paths.index,
            IndexEntry(op_id=envelope.op_id, archive_path=str(relative_archive)).model_dump(mode="json"),
        )

    marker = OpEnvelope(
        op_id=str(uuid4()),
        ts=ts,
        agent_id="supervisor-compact",
        base_version=book.op_log_version,
        op=CompactMarker(
            op="compact_marker",
            compacted_at_version=book.op_log_version,
            archive_path=str(relative_archive),
            archived_op_count=len(current_log),
        ),
        rationale=f"compact log into {relative_archive}",
        applied_version=book.op_log_version,
        applied_at=ts,
    )

    paths.current.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.current, marker.model_dump_json() + "\n")  # noqa: F821  (imported above)
    return marker


__all__ = [
    "ARCHIVE_DIR",
    "BOOK_FILE",
    "CURRENT_LOG",
    "INDEX_LOG",
    "REJECTED_LOG",
    "REVERT_BACKREF_LOG",
    "EditLogPaths",
    "IndexEntry",
    "LocatedEnvelope",
    "RejectedLogEntry",
    "RevertBackrefEntry",
    "append_accepted_log",
    "append_rejected_log",
    "append_revert_backref",
    "apply_and_log",
    "compact_log",
    "find_envelope",
    "known_op_ids",
    "read_current_log",
    "resolve_edit_log_paths",
    "reverted_target_op_ids",
]
