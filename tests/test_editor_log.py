from __future__ import annotations

from pathlib import Path

from epubforge.editor.log import (
    append_audit_event,
    compact_log,
    count_applied_log_events,
    count_current_log_events,
    read_current_log,
)
from epubforge.ir.semantic import Book, Chapter, Paragraph, Provenance


def _book() -> Book:
    return Book(
        initialized_at="2026-04-23T08:00:00Z",
        uid_seed="seed-1",
        title="Test Book",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter 1",
                blocks=[
                    Paragraph(uid="p-1", text="Alpha", provenance=Provenance(page=1))
                ],
            )
        ],
    )


def test_append_audit_event_round_trips_jsonl(tmp_path: Path) -> None:
    edit_dir = tmp_path / "edit_state"

    entry = append_audit_event(
        edit_dir,
        kind="agent_output_submitted",
        ts="2026-04-23T08:00:01Z",
        payload={"output_id": "out-1", "patches_applied": 2},
    )

    entries = read_current_log(edit_dir)
    assert entries == [entry]
    assert entries[0].payload["patches_applied"] == 2
    assert count_current_log_events(edit_dir) == 1


def test_count_applied_log_events_ignores_staged_outputs(tmp_path: Path) -> None:
    edit_dir = tmp_path / "edit_state"
    append_audit_event(edit_dir, kind="agent_output_staged", ts="2026-04-23T08:00:01Z")
    append_audit_event(
        edit_dir, kind="agent_output_submitted", ts="2026-04-23T08:00:02Z"
    )

    assert count_current_log_events(edit_dir) == 2
    assert count_applied_log_events(edit_dir) == 1


def test_compact_archives_current_audit_log_and_writes_marker(tmp_path: Path) -> None:
    edit_dir = tmp_path / "edit_state"
    append_audit_event(edit_dir, kind="agent_output_staged", ts="2026-04-23T08:00:01Z")
    append_audit_event(
        edit_dir, kind="agent_output_submitted", ts="2026-04-23T08:00:02Z"
    )

    marker = compact_log(edit_dir, _book(), ts="2026-04-23T08:00:03Z")

    current = read_current_log(edit_dir)
    assert current == [marker]
    assert marker.kind == "compact_marker"
    assert marker.payload["archived_event_count"] == 2
    assert marker.payload["applied_event_count"] == 1
    assert count_applied_log_events(edit_dir) == 1
    archive_path = edit_dir / marker.payload["archive_path"]
    assert (archive_path / "edit_log.jsonl").exists()
    assert (archive_path / "book.json").exists()


def test_count_applied_log_events_uses_compact_baseline(tmp_path: Path) -> None:
    edit_dir = tmp_path / "edit_state"
    append_audit_event(edit_dir, kind="agent_output_staged", ts="2026-04-23T08:00:01Z")
    append_audit_event(
        edit_dir, kind="agent_output_submitted", ts="2026-04-23T08:00:02Z"
    )
    compact_log(edit_dir, _book(), ts="2026-04-23T08:00:03Z")

    append_audit_event(edit_dir, kind="agent_output_staged", ts="2026-04-23T08:00:04Z")
    assert count_applied_log_events(edit_dir) == 1

    append_audit_event(
        edit_dir, kind="agent_output_submitted", ts="2026-04-23T08:00:05Z"
    )
    assert count_applied_log_events(edit_dir) == 2
