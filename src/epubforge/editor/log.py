"""Append-only audit log helpers for the BookPatch/AgentOutput editor workflow."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from epubforge.ir.semantic import Book


CURRENT_LOG = "edit_log.jsonl"
ARCHIVE_DIR = "log.archive"
BOOK_FILE = "book.json"
APPLIED_EVENT_KINDS = frozenset({"agent_output_submitted"})
COMPACT_MARKER = "compact_marker"
COMPACT_APPLIED_EVENT_COUNT = "applied_event_count"


class AuditLogEntry(BaseModel):
    """Single audit-log event emitted by the current editor system."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    ts: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class EditLogPaths:
    root: Path
    current: Path
    archive_root: Path


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
    return EditLogPaths(root=root, current=current, archive_root=root / ARCHIVE_DIR)


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


def read_current_log(path: str | Path) -> list[AuditLogEntry]:
    paths = resolve_edit_log_paths(path)
    return [AuditLogEntry.model_validate(item) for item in _iter_jsonl(paths.current)]


def append_audit_event(
    path: str | Path,
    *,
    kind: str,
    ts: str,
    payload: dict[str, Any] | None = None,
) -> AuditLogEntry:
    """Append a current-system audit event and return the stored entry."""

    entry = AuditLogEntry(
        event_id=str(uuid4()),
        ts=ts,
        kind=kind,
        payload=payload or {},
    )
    paths = resolve_edit_log_paths(path)
    _append_jsonl(paths.current, entry.model_dump(mode="json"))
    return entry


def count_current_log_events(path: str | Path) -> int:
    return len(read_current_log(path))


def count_applied_log_events(path: str | Path) -> int:
    """Count accepted mutation events monotonically across log compaction.

    Compaction rewrites the current JSONL log to a compact marker plus any later
    events. The marker stores the cumulative accepted-event count through the
    archived log so doctor deltas remain stable after compaction.
    """

    total = 0
    for entry in read_current_log(path):
        if entry.kind == COMPACT_MARKER:
            total = int(entry.payload[COMPACT_APPLIED_EVENT_COUNT])
        elif entry.kind in APPLIED_EVENT_KINDS:
            total += 1
    return total


def compact_log(path: str | Path, book: Book, *, ts: str) -> AuditLogEntry:
    """Archive the current audit log and replace it with a compact marker event."""

    paths = resolve_edit_log_paths(path)
    archive_name = ts.replace(":", "-")
    archive_path = paths.archive_root / archive_name
    archive_path.mkdir(parents=True, exist_ok=True)

    from epubforge.editor.state import atomic_write_text  # lazy: avoid circular import

    archived_events = read_current_log(paths.root)
    applied_event_count = count_applied_log_events(paths.root)
    if paths.current.exists():
        shutil.copyfile(paths.current, archive_path / CURRENT_LOG)
    else:
        atomic_write_text(archive_path / CURRENT_LOG, "")
    atomic_write_text(archive_path / BOOK_FILE, book.model_dump_json(indent=2))

    relative_archive = archive_path.relative_to(paths.root)
    marker = AuditLogEntry(
        event_id=str(uuid4()),
        ts=ts,
        kind=COMPACT_MARKER,
        payload={
            "archive_path": str(relative_archive),
            "archived_event_count": len(archived_events),
            COMPACT_APPLIED_EVENT_COUNT: applied_event_count,
        },
    )
    atomic_write_text(paths.current, marker.model_dump_json() + "\n")
    return marker


__all__ = [
    "ARCHIVE_DIR",
    "BOOK_FILE",
    "COMPACT_APPLIED_EVENT_COUNT",
    "COMPACT_MARKER",
    "CURRENT_LOG",
    "APPLIED_EVENT_KINDS",
    "AuditLogEntry",
    "EditLogPaths",
    "append_audit_event",
    "count_applied_log_events",
    "compact_log",
    "count_current_log_events",
    "read_current_log",
    "resolve_edit_log_paths",
]
