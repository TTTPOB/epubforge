"""Stable CLI tool surface for editor orchestration commands."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from epubforge.editor.apply import ApplyError, apply_envelope
from epubforge.editor.cli_support import CommandError, JsonArgumentParser, emit_json, emit_text
from epubforge.editor.doctor import DoctorReport, build_doctor_report
from epubforge.editor.leases import LeaseState
from epubforge.editor.log import (
    append_accepted_log,
    append_rejected_log,
    append_revert_backref,
    compact_log,
    find_envelope,
    known_op_ids,
    reverted_target_op_ids,
)
from epubforge.editor.memory import EditMemory
from epubforge.editor.ops import NoopOp, OpEnvelope
from epubforge.editor.prompts import render_prompt
from epubforge.editor.scratch import allocate_script_path, run_script, write_script_stub
from epubforge.editor.state import (
    book_id_from_paths,
    chapter_uids,
    clear_staging,
    default_init_source,
    ensure_initialized,
    ensure_uninitialized,
    ensure_work_dir,
    load_editable_book,
    load_editor_memory,
    load_lease_state,
    read_staging,
    resolve_editor_paths,
    save_leases,
    save_memory,
    source_artifact_path,
    write_initial_state,
    initialize_book_state,
)
from epubforge.io import load_book, save_book


class DoctorContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book_version: int
    memory: EditMemory
    report: DoctorReport


def _parser(command: str, description: str) -> JsonArgumentParser:
    return JsonArgumentParser(prog=f"python -m epubforge.editor.{command}", description=description)


def _timestamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_doctor_context(path: Path) -> DoctorContext | None:
    if not path.exists():
        return None
    return DoctorContext.model_validate_json(path.read_text(encoding="utf-8"))


def _save_doctor_context(path: Path, *, book_version: int, memory: EditMemory, report: DoctorReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        DoctorContext(book_version=book_version, memory=memory, report=report).model_dump_json(indent=2),
        encoding="utf-8",
    )


def _chapter_uid_or_error(book, chapter_uid: str) -> str:
    for chapter in book.chapters:
        if chapter.uid == chapter_uid:
            return chapter_uid
    raise CommandError(f"chapter not found: {chapter_uid}")


def _resolve_issues(values: list[str] | None) -> list[str]:
    if not values:
        return []
    if len(values) == 1:
        candidate = values[0].strip()
        if candidate.startswith("["):
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise CommandError(f"--issues JSON must parse successfully: {exc.msg}") from exc
            if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
                raise CommandError("--issues JSON must be a list of strings")
            return payload
    return values


def run_init(argv: list[str] | None = None) -> int:
    parser = _parser("init", "Initialize edit_state from 05_semantic.json.")
    parser.add_argument("work")
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_uninitialized(paths)
    source = default_init_source(paths)
    if not source.exists():
        raise CommandError(f"missing init source: {source}")

    now = _timestamp()
    book = initialize_book_state(load_book(source), initialized_at=now)
    memory = EditMemory.create(
        book_id=book_id_from_paths(paths),
        updated_at=now,
        updated_by="editor.init",
        chapter_uids=chapter_uids(book),
    )
    write_initial_state(paths, book=book, memory=memory, leases=LeaseState())
    emit_json(
        {
            "initialized_at": book.initialized_at,
            "uid_seed": book.uid_seed,
            "book_version": book.version,
            "book_path": str(paths.book_path),
        }
    )
    return 0


def run_import_legacy(argv: list[str] | None = None) -> int:
    parser = _parser("import-legacy", "Initialize edit_state from a legacy artifact.")
    parser.add_argument("work")
    parser.add_argument("--from", dest="source", required=True)
    parser.add_argument("--assume-verified", action="store_true")
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_uninitialized(paths)
    source = source_artifact_path(paths, args.source)
    if not source.exists():
        raise CommandError(f"missing legacy artifact: {source}")

    now = _timestamp()
    book = initialize_book_state(load_book(source), initialized_at=now)
    memory = EditMemory.create(
        book_id=book_id_from_paths(paths),
        updated_at=now,
        updated_by="editor.import-legacy",
        chapter_uids=chapter_uids(book),
    ).with_legacy_import(
        imported_from=source.name,
        imported_at=now,
        updated_by="editor.import-legacy",
        chapter_uids=chapter_uids(book),
        assume_verified=args.assume_verified,
    )
    write_initial_state(paths, book=book, memory=memory, leases=LeaseState())

    noop_env = OpEnvelope(
        op_id=str(uuid4()),
        ts=now,
        agent_id="editor.import-legacy",
        base_version=0,
        op=NoopOp(op="noop", purpose="legacy_baseline"),
        rationale="legacy import baseline",
    )
    result = apply_envelope(book, noop_env, now=lambda: now)
    imported_book = result.book
    for accepted in result.accepted_envelopes:
        append_accepted_log(paths.edit_state_dir, accepted)
    save_book(imported_book, paths.work_dir)

    emit_json(
        {
            "initialized_at": imported_book.initialized_at,
            "uid_seed": imported_book.uid_seed,
            "book_version": imported_book.version,
            "assume_verified": memory.assume_verified,
            "book_path": str(paths.book_path),
        }
    )
    return 0


def run_doctor(argv: list[str] | None = None) -> int:
    parser = _parser("doctor", "Run doctor detectors and readiness evaluation.")
    parser.add_argument("work")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    book = load_editable_book(paths)
    memory = load_editor_memory(paths)
    previous = _load_doctor_context(paths.doctor_context_path)
    new_applied_op_count = 0
    if previous is not None:
        new_applied_op_count = max(0, book.version - previous.book_version)
    report = build_doctor_report(
        memory=memory,
        book=book,
        previous_memory=previous.memory if previous is not None else None,
        previous_report=previous.report if previous is not None else None,
        new_applied_op_count=new_applied_op_count,
    )
    paths.audit_dir.mkdir(parents=True, exist_ok=True)
    paths.doctor_report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    _save_doctor_context(paths.doctor_context_path, book_version=book.version, memory=memory, report=report)
    emit_json(report.model_dump(mode="json"))
    return 0


def run_propose_op(argv: list[str] | None = None) -> int:
    parser = _parser("propose-op", "Validate OpEnvelope[] from stdin and append to staging.jsonl.")
    parser.add_argument("work")
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CommandError(f"stdin must be JSON: {exc.msg}")
    if not isinstance(payload, list):
        raise CommandError("stdin JSON must be an array of OpEnvelope objects")

    accepted: list[OpEnvelope] = []
    errors: list[dict[str, object]] = []
    for index, item in enumerate(payload):
        try:
            accepted.append(OpEnvelope.model_validate(item))
        except Exception as exc:  # noqa: BLE001
            errors.append({"index": index, "error": str(exc)})

    if accepted:
        from epubforge.editor.state import append_staging

        append_staging(paths, accepted)
    emit_json({"accepted": len(accepted), "rejected": len(errors), "errors": errors})
    return 0 if not errors else 1


def run_apply_queue(argv: list[str] | None = None) -> int:
    parser = _parser("apply-queue", "Apply staged envelopes to book.json and edit log.")
    parser.add_argument("work")
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    book = load_editable_book(paths)
    memory = load_editor_memory(paths)
    lease_state = load_lease_state(paths)
    timestamp = _timestamp()
    lease_state.expire_stale(now=timestamp)
    staged = read_staging(paths)
    if not staged:
        save_leases(paths, lease_state)
        clear_staging(paths)
        emit_json({"applied": 0, "rejected": 0, "new_version": book.version})
        return 0

    known_ids = known_op_ids(paths.edit_state_dir)
    reverted_ids = reverted_target_op_ids(paths.edit_state_dir)
    applied_count = 0
    rejected_count = 0
    errors: list[dict[str, str]] = []

    for envelope in staged:
        try:
            result = apply_envelope(
                book,
                envelope,
                existing_op_ids=known_ids,
                reverted_target_op_ids=reverted_ids,
                resolve_target=lambda op_id: (located.envelope if (located := find_envelope(paths.edit_state_dir, op_id)) else None),
                now=lambda: timestamp,
                lease_state=lease_state,
                memory=memory,
            )
        except ApplyError as exc:
            rejected_count += 1
            append_rejected_log(paths.edit_state_dir, envelope, reason=exc.reason, rejected_at=timestamp)
            errors.append({"op_id": envelope.op_id, "error": exc.reason})
            continue

        for accepted in result.accepted_envelopes:
            append_accepted_log(paths.edit_state_dir, accepted)
            known_ids.add(accepted.op_id)
            applied_count += 1
        if result.revert_backref is not None:
            append_revert_backref(paths.edit_state_dir, result.revert_backref)
            reverted_ids.add(result.revert_backref.target_op_id)
        book = result.book
        memory = result.memory or memory
        save_book(book, paths.work_dir)
        save_memory(paths, memory)

    save_leases(paths, lease_state)
    clear_staging(paths)
    payload: dict[str, object] = {"applied": applied_count, "rejected": rejected_count, "new_version": book.version}
    if errors:
        payload["errors"] = errors
    emit_json(payload)
    return 0 if rejected_count == 0 else 1


def run_acquire_lease(argv: list[str] | None = None) -> int:
    parser = _parser("acquire-lease", "Acquire a chapter lease.")
    parser.add_argument("work")
    parser.add_argument("--chapter", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--ttl", type=int, default=1800)
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    book = load_editable_book(paths)
    chapter_uid = _chapter_uid_or_error(book, args.chapter)
    state = load_lease_state(paths)
    lease = state.acquire_chapter(chapter_uid, args.agent, args.task, ttl=args.ttl, now=_timestamp())
    save_leases(paths, state)
    if lease is None:
        raise CommandError("chapter lease unavailable", raw_stdout="null")
    emit_json(lease.model_dump(mode="json"))
    return 0


def run_release_lease(argv: list[str] | None = None) -> int:
    parser = _parser("release-lease", "Release a chapter lease.")
    parser.add_argument("work")
    parser.add_argument("--chapter", required=True)
    parser.add_argument("--agent", required=True)
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    state = load_lease_state(paths)
    released = state.release_chapter(args.chapter, args.agent, now=_timestamp())
    save_leases(paths, state)
    if released is None:
        raise CommandError("chapter lease not held by agent")
    emit_json({"released": True, "lease": released.model_dump(mode="json")})
    return 0


def run_acquire_book_lock(argv: list[str] | None = None) -> int:
    parser = _parser("acquire-book-lock", "Acquire the book-wide exclusive lease.")
    parser.add_argument("work")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--reason", required=True, choices=["topology_op", "compact", "init"])
    parser.add_argument("--ttl", type=int, default=300)
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    state = load_lease_state(paths)
    lease = state.acquire_book_exclusive(args.agent, args.reason, ttl=args.ttl, now=_timestamp())
    save_leases(paths, state)
    if lease is None:
        raise CommandError("book-exclusive lease unavailable", raw_stdout="null")
    emit_json(lease.model_dump(mode="json"))
    return 0


def run_release_book_lock(argv: list[str] | None = None) -> int:
    parser = _parser("release-book-lock", "Release the book-wide exclusive lease.")
    parser.add_argument("work")
    parser.add_argument("--agent", required=True)
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    state = load_lease_state(paths)
    released = state.release_book_exclusive(args.agent, now=_timestamp())
    save_leases(paths, state)
    if released is None:
        raise CommandError("book-exclusive lease not held by agent")
    emit_json({"released": True, "lease": released.model_dump(mode="json")})
    return 0


def run_run_script(argv: list[str] | None = None) -> int:
    parser = _parser("run-script", "Allocate or execute scratch scripts.")
    parser.add_argument("work")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write")
    mode.add_argument("--exec", dest="exec_path")
    parser.add_argument("--agent", default="agent")
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    if args.write is not None:
        path = write_script_stub(allocate_script_path(paths.work_dir, args.write, agent_id=args.agent))
        emit_json({"path": str(path), "scratch_dir": str(paths.scratch_dir)})
        return 0

    result = run_script(args.exec_path, work_dir=paths.work_dir)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


def run_compact(argv: list[str] | None = None) -> int:
    parser = _parser("compact", "Compact the accepted edit log into an archive snapshot.")
    parser.add_argument("work")
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    state = load_lease_state(paths)
    state.expire_stale(now=_timestamp())
    if state.book_exclusive is not None or state.chapter_leases:
        raise CommandError("cannot compact while leases are active")
    book = load_editable_book(paths)
    marker = compact_log(paths.edit_state_dir, book, ts=_timestamp())
    save_leases(paths, state)
    emit_json(marker.model_dump(mode="json"))
    return 0


def run_snapshot(argv: list[str] | None = None) -> int:
    parser = _parser("snapshot", "Copy current edit_state into snapshots/<tag>/.")
    parser.add_argument("work")
    parser.add_argument("--tag")
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    tag = args.tag or _timestamp().replace(":", "-")
    destination = paths.snapshots_dir / tag
    if destination.exists():
        raise CommandError(f"snapshot already exists: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    for entry in paths.edit_state_dir.iterdir():
        if entry.name == paths.snapshots_dir.name:
            continue
        target = destination / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copy2(entry, target)
    emit_json({"snapshot": str(destination)})
    return 0


def run_render_prompt(argv: list[str] | None = None) -> int:
    parser = _parser("render-prompt", "Render a subagent prompt with current book.version and memory snapshot.")
    parser.add_argument("work")
    parser.add_argument("--kind", required=True, choices=["scanner", "fixer", "reviewer"])
    parser.add_argument("--chapter", required=True)
    parser.add_argument("--issues", action="append")
    args = parser.parse_args(argv)

    paths = resolve_editor_paths(args.work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    book = load_editable_book(paths)
    _chapter_uid_or_error(book, args.chapter)
    memory = load_editor_memory(paths)
    prompt = render_prompt(
        kind=args.kind,
        book=book,
        memory=memory,
        work_dir=paths.work_dir,
        book_path=paths.book_path,
        chapter_uid=args.chapter,
        issues=_resolve_issues(args.issues),
    )
    emit_text(prompt)
    return 0


__all__ = [
    "run_acquire_book_lock",
    "run_acquire_lease",
    "run_apply_queue",
    "run_compact",
    "run_doctor",
    "run_import_legacy",
    "run_init",
    "run_propose_op",
    "run_release_book_lock",
    "run_release_lease",
    "run_render_prompt",
    "run_run_script",
    "run_snapshot",
]
