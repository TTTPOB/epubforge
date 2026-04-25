"""Stable business-logic surface for editor orchestration commands."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from epubforge.config import Config
from epubforge.editor.cli_support import CommandError, emit_json, emit_text
from epubforge.editor.diff import DiffError, diff_books
from epubforge.editor.doctor import DoctorReport, build_doctor_report
from epubforge.editor.log import compact_log, count_applied_log_events
from epubforge.editor.memory import EditMemory
from epubforge.editor.patches import PatchError, apply_book_patch, validate_book_patch
from epubforge.editor.prompts import render_prompt
from epubforge.editor.projection import render_chapter_projection, render_index
from epubforge.editor.scratch import allocate_script_path, run_script, write_script_stub
from epubforge.editor.state import (
    Stage3EditorMeta,
    atomic_write_text,
    book_id_from_paths,
    chapter_uids,
    default_init_source,
    ensure_initialized,
    ensure_uninitialized,
    ensure_work_dir,
    load_editable_book,
    load_editor_meta,
    load_editor_memory,
    resolve_editor_paths,
    write_initial_state,
    initialize_book_state,
)
from epubforge.editor.workspace import GitError, find_repo_root, resolve_book_path_at_ref
from epubforge.io import load_book, save_book
from epubforge.ir.semantic import Book


class DoctorContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied_event_count: int
    memory: EditMemory
    report: DoctorReport


class DiffBooksResult(BaseModel):
    """Machine-readable result for the editor diff-books tool surface."""

    model_config = ConfigDict(extra="forbid")

    diff_applies: bool
    round_trip_verified: bool
    change_count: int
    base_sha256: str
    proposed_sha256: str
    patch: dict[str, Any]
    unsupported_diffs: list[dict[str, str]] = Field(default_factory=list)
    review_groups: list[dict[str, Any]] = Field(default_factory=list)
    base_ref: str | None = None
    proposed_ref: str | None = None


def _timestamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_doctor_context(path: Path) -> DoctorContext | None:
    if not path.exists():
        return None
    return DoctorContext.model_validate_json(path.read_text(encoding="utf-8"))


def _save_doctor_context(
    path: Path, *, applied_event_count: int, memory: EditMemory, report: DoctorReport
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        DoctorContext(
            applied_event_count=applied_event_count, memory=memory, report=report
        ).model_dump_json(indent=2),
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
                raise CommandError(
                    f"--issues JSON must parse successfully: {exc.msg}"
                ) from exc
            if not isinstance(payload, list) or not all(
                isinstance(item, str) for item in payload
            ):
                raise CommandError("--issues JSON must be a list of strings")
            return payload
    return values


def _build_stage3_meta(work_dir: Path, book) -> Stage3EditorMeta | None:
    """Build Stage3EditorMeta from the active manifest and source book extraction info.

    Returns None if no active manifest exists (legacy workflow).
    Raises CommandError on mismatch between book extraction metadata and active manifest.
    """
    from epubforge.stage3_artifacts import (
        Stage3ContractError,
        load_active_stage3_manifest,
    )

    try:
        pointer, manifest = load_active_stage3_manifest(work_dir)
    except Stage3ContractError:
        return None

    # Validate source book's extraction metadata matches active manifest.
    ex = book.extraction
    if ex.artifact_id is not None and ex.artifact_id != pointer.active_artifact_id:
        raise CommandError(
            f"source book artifact_id={ex.artifact_id!r} does not match "
            f"active manifest artifact_id={pointer.active_artifact_id!r}. "
            "Run `epubforge assemble` to regenerate 05_semantic_raw.json."
        )
    if (
        ex.stage3_manifest_sha256 is not None
        and ex.stage3_manifest_sha256 != pointer.manifest_sha256
    ):
        raise CommandError(
            "source book stage3_manifest_sha256 does not match active manifest. "
            "Run `epubforge assemble` to regenerate 05_semantic_raw.json."
        )

    # Determine evidence_index_path (workdir-relative from manifest sidecars)
    evidence_index_rel = manifest.sidecars.get("evidence_index", "")
    evidence_index_abs = (
        str(work_dir / evidence_index_rel) if evidence_index_rel else ""
    )

    # extraction_warnings_path: artifact dir / "warnings.json" (may not exist yet)
    from epubforge.stage3_artifacts import resolve_manifest_paths

    mpaths = resolve_manifest_paths(work_dir, manifest)
    artifact_dir = mpaths["artifact_dir"]
    extraction_warnings_path = str(artifact_dir / "warnings.json")

    return Stage3EditorMeta(
        mode=manifest.mode,
        manifest_path=str(work_dir / manifest.artifact_dir / "manifest.json"),
        manifest_sha256=pointer.manifest_sha256,
        artifact_id=pointer.active_artifact_id,
        selected_pages=manifest.selected_pages,
        complex_pages=manifest.complex_pages,
        source_pdf=manifest.source_pdf,
        evidence_index_path=evidence_index_abs,
        extraction_warnings_path=extraction_warnings_path,
    )


def run_init(work: Path, cfg: Config) -> int:
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_uninitialized(paths)

    try:
        source = default_init_source(paths)
    except FileNotFoundError as exc:
        raise CommandError(str(exc)) from exc

    now = _timestamp()
    book = initialize_book_state(load_book(source), initialized_at=now)
    stage3 = _build_stage3_meta(paths.work_dir, book)
    memory = EditMemory.create(
        book_id=book_id_from_paths(paths),
        updated_at=now,
        updated_by="editor.init",
        chapter_uids=chapter_uids(book),
    )
    write_initial_state(paths, book=book, memory=memory, stage3=stage3)
    save_book(book, paths.work_dir)

    # Copy artifact audit_notes.json to edit_state/audit/extraction_notes.json
    if stage3 is not None:
        from epubforge.stage3_artifacts import (
            Stage3ContractError,
            load_active_stage3_manifest,
        )

        try:
            _pointer, manifest = load_active_stage3_manifest(paths.work_dir)
            audit_notes_rel = manifest.sidecars.get("audit_notes", "")
            if audit_notes_rel:
                audit_notes_src = paths.work_dir / audit_notes_rel
                if audit_notes_src.exists():
                    extraction_notes_dst = paths.audit_dir / "extraction_notes.json"
                    extraction_notes_dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(audit_notes_src, extraction_notes_dst)
        except Stage3ContractError:
            pass

    result: dict[str, object] = {
        "initialized_at": book.initialized_at,
        "uid_seed": book.uid_seed,
        "book_path": str(paths.book_path),
    }
    if stage3 is not None:
        result["stage3"] = stage3.model_dump(mode="json")
    emit_json(result)
    return 0


def run_doctor(work: Path, output_json: bool, cfg: Config) -> int:
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    book = load_editable_book(paths)
    memory = load_editor_memory(paths)
    previous = _load_doctor_context(paths.doctor_context_path)
    applied_event_count = count_applied_log_events(paths.edit_state_dir)
    new_applied_op_count = applied_event_count
    if previous is not None:
        new_applied_op_count = max(
            0, applied_event_count - previous.applied_event_count
        )
    report = build_doctor_report(
        memory=memory,
        book=book,
        previous_memory=previous.memory if previous is not None else None,
        previous_report=previous.report if previous is not None else None,
        new_applied_op_count=new_applied_op_count,
    )
    paths.audit_dir.mkdir(parents=True, exist_ok=True)
    paths.doctor_report_path.write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )
    _save_doctor_context(
        paths.doctor_context_path,
        applied_event_count=applied_event_count,
        memory=memory,
        report=report,
    )
    emit_json(report.model_dump(mode="json"))
    return 0


def run_run_script(
    work: Path,
    write: str | None,
    exec_path: str | None,
    agent: str,
    cfg: Config,
) -> int:
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    if write is not None:
        path = write_script_stub(
            allocate_script_path(paths.work_dir, write, agent_id=agent)
        )
        emit_json({"path": str(path), "scratch_dir": str(paths.scratch_dir)})
        return 0

    if exec_path is None:
        raise CommandError("either --write or --exec must be provided")

    try:
        result = run_script(exec_path, work_dir=paths.work_dir)
    except (ValueError, FileNotFoundError) as exc:
        raise CommandError(str(exc)) from exc
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


def run_compact(work: Path, cfg: Config) -> int:
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    book = load_editable_book(paths)
    marker = compact_log(paths.edit_state_dir, book, ts=_timestamp())
    emit_json(marker.model_dump(mode="json"))
    return 0


def _read_book_snapshot(path: Path, *, label: str) -> tuple[Book, bytes]:
    resolved = path.expanduser()
    if not resolved.exists():
        raise CommandError(
            f"{label} file not found: {resolved}",
            exit_code=2,
            payload={
                "error": f"{label} file not found: {resolved}",
                "kind": "file_not_found",
                "path": str(resolved),
            },
        )
    if not resolved.is_file():
        raise CommandError(
            f"{label} path is not a file: {resolved}",
            exit_code=2,
            payload={
                "error": f"{label} path is not a file: {resolved}",
                "kind": "not_a_file",
                "path": str(resolved),
            },
        )

    raw = resolved.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CommandError(
            f"invalid UTF-8 in {label} Book JSON {resolved}: {exc}",
            exit_code=1,
            payload={
                "error": f"invalid UTF-8 in {label} Book JSON: {exc}",
                "kind": "invalid_json",
                "path": str(resolved),
            },
        ) from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CommandError(
            f"invalid JSON in {label} Book file {resolved}: {exc.msg}",
            exit_code=1,
            payload={
                "error": f"invalid JSON in {label} Book file: {exc.msg}",
                "kind": "invalid_json",
                "path": str(resolved),
                "line": exc.lineno,
                "column": exc.colno,
            },
        ) from exc

    try:
        return Book.model_validate(payload), raw
    except ValidationError as exc:
        raise CommandError(
            f"invalid Book schema in {label} file {resolved}: {exc}",
            exit_code=1,
            payload={
                "error": f"invalid Book schema in {label} file: {exc}",
                "kind": "invalid_book_schema",
                "path": str(resolved),
            },
        ) from exc


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _classify_diff_error(exc: DiffError) -> tuple[str, int]:
    message = str(exc)
    if any(
        marker in message
        for marker in (
            "duplicate uid",
            "uid=None",
            "empty uid",
            "non-string uid",
        )
    ):
        return "uid_error", 1
    if any(
        marker in message
        for marker in (
            "unsupported",
            "unclassified",
            "Book-level",
            "immutable",
        )
    ):
        return "unsupported_diff", 2
    return "diff_error", 1


def build_diff_books_result(
    work: Path,
    *,
    proposed_file: Path | None = None,
    base_file: Path | None = None,
    proposed_ref: str | None = None,
    base_ref: str | None = None,
    verify_round_trip: bool = True,
) -> DiffBooksResult:
    """Build a machine-readable diff result from two Book JSON snapshots.

    If *base_file* is omitted and *base_ref* is also omitted, the base snapshot
    defaults to ``<work>/edit_state/book.json``. This helper is read-only: it
    validates and applies the generated patch in memory for round-trip
    verification, but never writes to ``edit_state/book.json`` or any other
    editor state file.

    Either *proposed_file* or *proposed_ref* must be provided (but not both).
    Similarly, *base_file* and *base_ref* are mutually exclusive.

    When a ref is provided, the Book is resolved via ``git show`` using
    ``resolve_book_path_at_ref``.
    """
    # Validate mutual exclusivity
    if base_file is not None and base_ref is not None:
        raise CommandError(
            "--base-file and --base-ref are mutually exclusive",
            exit_code=2,
            payload={
                "error": "--base-file and --base-ref are mutually exclusive",
                "kind": "invalid_args",
            },
        )
    if proposed_file is not None and proposed_ref is not None:
        raise CommandError(
            "--proposed-file and --proposed-ref are mutually exclusive",
            exit_code=2,
            payload={
                "error": "--proposed-file and --proposed-ref are mutually exclusive",
                "kind": "invalid_args",
            },
        )
    if proposed_file is None and proposed_ref is None:
        raise CommandError(
            "one of --proposed-file or --proposed-ref is required",
            exit_code=2,
            payload={
                "error": "one of --proposed-file or --proposed-ref is required",
                "kind": "invalid_args",
            },
        )

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)

    # Resolve base Book
    if base_ref is not None:
        try:
            repo_root = find_repo_root(work.resolve())
        except GitError as exc:
            raise CommandError(
                f"work directory is not inside a Git repo: {exc}",
                exit_code=1,
                payload={
                    "error": f"work directory is not inside a Git repo: {exc}",
                    "kind": "not_a_repo",
                },
            ) from exc
        work_dir_rel = str(work.resolve().relative_to(repo_root))
        try:
            base, base_raw = resolve_book_path_at_ref(repo_root, base_ref, work_dir_rel)
        except GitError as exc:
            raise CommandError(
                f"failed to resolve base ref {base_ref!r}: {exc}",
                exit_code=1,
                payload={
                    "error": f"failed to resolve base ref {base_ref!r}: {exc}",
                    "kind": "git_ref_error",
                    "ref": base_ref,
                },
            ) from exc
        except ValidationError as exc:
            raise CommandError(
                f"base ref {base_ref!r} does not contain a valid Book schema: {exc}",
                exit_code=1,
                payload={
                    "error": f"base ref {base_ref!r} does not contain a valid Book schema: {exc}",
                    "kind": "invalid_book_schema",
                    "ref": base_ref,
                },
            ) from exc
    else:
        resolved_base_file = base_file or paths.book_path
        base, base_raw = _read_book_snapshot(resolved_base_file, label="base")

    # Resolve proposed Book
    if proposed_ref is not None:
        try:
            repo_root = find_repo_root(work.resolve())
        except GitError as exc:
            raise CommandError(
                f"work directory is not inside a Git repo: {exc}",
                exit_code=1,
                payload={
                    "error": f"work directory is not inside a Git repo: {exc}",
                    "kind": "not_a_repo",
                },
            ) from exc
        work_dir_rel = str(work.resolve().relative_to(repo_root))
        try:
            proposed, proposed_raw = resolve_book_path_at_ref(
                repo_root, proposed_ref, work_dir_rel
            )
        except GitError as exc:
            raise CommandError(
                f"failed to resolve proposed ref {proposed_ref!r}: {exc}",
                exit_code=1,
                payload={
                    "error": f"failed to resolve proposed ref {proposed_ref!r}: {exc}",
                    "kind": "git_ref_error",
                    "ref": proposed_ref,
                },
            ) from exc
        except ValidationError as exc:
            raise CommandError(
                f"proposed ref {proposed_ref!r} does not contain a valid Book schema: {exc}",
                exit_code=1,
                payload={
                    "error": f"proposed ref {proposed_ref!r} does not contain a valid Book schema: {exc}",
                    "kind": "invalid_book_schema",
                    "ref": proposed_ref,
                },
            ) from exc
    else:
        assert proposed_file is not None  # guaranteed by mutual-exclusivity check above
        proposed, proposed_raw = _read_book_snapshot(proposed_file, label="proposed")
    base_sha256 = _sha256_bytes(base_raw)
    proposed_sha256 = _sha256_bytes(proposed_raw)

    try:
        patch = diff_books(base, proposed)
    except DiffError as exc:
        kind, exit_code = _classify_diff_error(exc)
        unsupported_diffs = [{"message": str(exc)}] if kind == "unsupported_diff" else []
        raise CommandError(
            str(exc),
            exit_code=exit_code,
            payload={
                "error": str(exc),
                "kind": kind,
                "base_sha256": base_sha256,
                "proposed_sha256": proposed_sha256,
                "unsupported_diffs": unsupported_diffs,
            },
        ) from exc

    try:
        validate_book_patch(base, patch)
        applied = apply_book_patch(base, patch)
    except PatchError as exc:
        raise CommandError(
            f"generated patch did not apply: {exc.reason}",
            exit_code=2,
            payload={
                "error": f"generated patch did not apply: {exc.reason}",
                "kind": "patch_apply_failed",
                "patch_id": exc.patch_id,
                "base_sha256": base_sha256,
                "proposed_sha256": proposed_sha256,
                "change_count": len(patch.changes),
                "diff_applies": False,
                "round_trip_verified": False,
            },
        ) from exc

    round_trip_verified = False
    if verify_round_trip:
        round_trip_verified = applied.model_dump(mode="json") == proposed.model_dump(
            mode="json"
        )
        if not round_trip_verified:
            raise CommandError(
                "generated patch applied but did not reproduce the proposed Book snapshot",
                exit_code=2,
                payload={
                    "error": "generated patch applied but did not reproduce the proposed Book snapshot",
                    "kind": "round_trip_mismatch",
                    "base_sha256": base_sha256,
                    "proposed_sha256": proposed_sha256,
                    "change_count": len(patch.changes),
                    "diff_applies": True,
                    "round_trip_verified": False,
                },
            )

    return DiffBooksResult(
        diff_applies=True,
        round_trip_verified=round_trip_verified,
        change_count=len(patch.changes),
        base_sha256=base_sha256,
        proposed_sha256=proposed_sha256,
        patch=patch.model_dump(mode="json"),
        unsupported_diffs=[],
        review_groups=[],
        base_ref=base_ref,
        proposed_ref=proposed_ref,
    )


def run_diff_books(
    work: Path,
    proposed_file: Path | None,
    base_file: Path | None,
    cfg: Config,
    *,
    proposed_ref: str | None = None,
    base_ref: str | None = None,
) -> int:
    _ = cfg
    result = build_diff_books_result(
        work,
        proposed_file=proposed_file,
        base_file=base_file,
        proposed_ref=proposed_ref,
        base_ref=base_ref,
    )
    emit_json(result.model_dump(mode="json"))
    return 0


def _render_pdf_page_image(
    pdf_path: Path,
    page: int,
    dpi: int,
    out_path: Path,
) -> None:
    """Render a single page of a PDF to JPEG using pypdfium2 (0-based page index internally).

    *page* is 1-based (as displayed in PDFs).
    """
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        page_count = len(doc)
        zero_based = page - 1
        if zero_based < 0 or zero_based >= page_count:
            raise CommandError(
                f"page {page} out of range for PDF with {page_count} pages: {pdf_path}"
            )
        pdf_page = doc[zero_based]
        scale = dpi / 72.0
        bitmap = pdf_page.render(scale=scale)
        pil_image = bitmap.to_pil()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pil_image.save(str(out_path), format="JPEG", quality=92)
    finally:
        doc.close()


def run_render_page(
    work: Path,
    page: int,
    dpi: int,
    out: Path | None,
    cfg: Config,
) -> int:
    """Render a single page of the source PDF to a JPEG image without LLM/VLM calls."""
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    meta = load_editor_meta(paths)
    if meta.stage3 is None:
        raise CommandError(
            "edit_state/meta.json has no stage3 section. "
            "Re-initialize with `epubforge editor init` after running Stage 3."
        )

    source_pdf = paths.work_dir / meta.stage3.source_pdf
    if not source_pdf.exists():
        raise CommandError(
            f"source PDF not found: {source_pdf}. "
            "Rerun parse with --force-rerun to restore source/source.pdf."
        )

    resolved_out = out or (paths.audit_dir / "page_images" / f"page_{page:04d}.jpg")

    _render_pdf_page_image(source_pdf, page, dpi, resolved_out)

    emit_json(
        {
            "image_path": str(resolved_out),
            "page": page,
            "dpi": dpi,
            "source_pdf": str(source_pdf),
        }
    )
    return 0


def _run_vlm_page_core(
    *,
    paths,
    page: int,
    dpi: int,
    cfg: Config,
    chapter: str | None = None,
    blocks: list[str] | None = None,
) -> "tuple[VLMObservation, str | None]":
    """Core VLM page analysis logic — returns (VLMObservation, evidence_warning).

    Importable by vlm-range (phase 8C) which calls this in a loop.
    Never mutates book.json or produces CLI output.
    """
    import base64
    import tempfile

    from epubforge.editor.vlm_evidence import (
        VLMObservation,
        VLMPageAnalysis,
        _compute_sha256_bytes,
        _compute_sha256_str,
        _generate_observation_id,
        save_vlm_observation,
    )

    # Step 1: Load meta and validate page
    meta = load_editor_meta(paths)
    if meta.stage3 is None:
        raise CommandError(
            "edit_state/meta.json has no stage3 section. "
            "Re-initialize with `epubforge editor init` after running Stage 3."
        )
    stage3 = meta.stage3

    if page not in stage3.selected_pages:
        raise CommandError(
            f"page {page} is not in selected pages {stage3.selected_pages}. "
            "Only selected pages have evidence and are eligible for VLM re-analysis."
        )

    # Step 2: Load Book IR and scope validation
    book = load_editable_book(paths)

    if chapter is not None:
        # Validate chapter exists
        matching_chapters = [ch for ch in book.chapters if ch.uid == chapter]
        if not matching_chapters:
            raise CommandError(f"chapter not found: {chapter}")
        # Scope to blocks from that chapter on this page
        scope_blocks = [
            b
            for ch in matching_chapters
            for b in ch.blocks
            if b.provenance.page == page
        ]
    elif blocks is not None:
        # Validate each block_uid exists in the book
        all_blocks_by_uid = {
            b.uid: b
            for ch in book.chapters
            for b in ch.blocks
            if b.uid is not None
        }
        missing = [uid for uid in blocks if uid not in all_blocks_by_uid]
        if missing:
            raise CommandError(f"block UIDs not found in book: {missing}")
        scope_blocks = [all_blocks_by_uid[uid] for uid in blocks]
    else:
        # Default: all blocks on this page
        scope_blocks = [
            b
            for ch in book.chapters
            for b in ch.blocks
            if b.provenance.page == page
        ]

    scope_block_uids: set[str] = {b.uid for b in scope_blocks if b.uid is not None}

    # Step 3: Render PDF page
    source_pdf = paths.work_dir / stage3.source_pdf
    if not source_pdf.exists():
        raise CommandError(
            f"source PDF not found: {source_pdf}. "
            "Rerun parse with --force-rerun to restore source/source.pdf."
        )

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_f:
        tmp_img_path = Path(tmp_f.name)

    try:
        _render_pdf_page_image(source_pdf, page, dpi, tmp_img_path)
        img_bytes = tmp_img_path.read_bytes()
        image_sha256 = _compute_sha256_bytes(img_bytes)
        img_b64 = base64.b64encode(img_bytes).decode("ascii")
    finally:
        try:
            tmp_img_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    # Step 4: Load evidence
    evidence_items: list[object] = []
    evidence_warning: str | None = None
    if stage3.evidence_index_path:
        ev_path = Path(stage3.evidence_index_path)
        if ev_path.exists():
            from epubforge.stage3_artifacts import EvidenceIndex

            ev_index = EvidenceIndex.model_validate_json(
                ev_path.read_text(encoding="utf-8")
            )
            page_evidence = ev_index.pages.get(str(page), {})
            evidence_items = (
                page_evidence.get("items", [])
                if isinstance(page_evidence, dict)
                else []
            )
        else:
            evidence_warning = f"evidence_index not found: {ev_path}"
    else:
        evidence_warning = "no evidence_index_path in stage3 meta"

    if not evidence_items:
        evidence_warning = (
            evidence_warning or ""
        ) + f" (no evidence items for page {page})"

    # Step 5: Build blocks context
    blocks_context = [
        {
            "uid": b.uid,
            "kind": b.kind,
            "text": (b.text[:200] if hasattr(b, "text") else ""),
            "role": getattr(b, "role", None),
            "page": b.provenance.page,
        }
        for b in scope_blocks
    ]

    # Step 6: Build VLM prompt and compute hash
    evidence_text = (
        json.dumps(evidence_items, ensure_ascii=False, indent=2)
        if evidence_items
        else "[]"
    )
    system_prompt = (
        "You are a PDF extraction quality reviewer. "
        "Analyze the provided page image and the extracted evidence items for accuracy."
    )
    user_content: list[object] = [
        {
            "type": "text",
            "text": (
                f"Page {page} evidence extracted by Stage 3 ({stage3.mode}):\n\n"
                f"{evidence_text}\n\n"
                "Blocks in scope (from book IR):\n\n"
                f"{json.dumps(blocks_context, ensure_ascii=False, indent=2)}\n\n"
                "Review the image and identify any extraction issues, missing elements, "
                "or items requiring semantic correction."
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
        },
    ]

    from typing import cast as _cast

    from openai.types.chat import ChatCompletionMessageParam as _Msg

    messages = _cast(
        "list[_Msg]",
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    prompt_sha256 = _compute_sha256_str(json.dumps(messages, ensure_ascii=False))

    # Step 7: Call VLM
    from epubforge.llm.client import LLMClient

    vlm_client = LLMClient(cfg, use_vlm=True)
    vlm_result: VLMPageAnalysis = vlm_client.chat_parsed(
        messages, response_format=VLMPageAnalysis
    )

    # Step 8: Build VLMObservation — filter hallucinated block_uids
    filtered_findings = []
    for finding in vlm_result.findings:
        filtered_uids = [
            uid for uid in finding.block_uids if uid in scope_block_uids
        ]
        filtered_findings.append(finding.model_copy(update={"block_uids": filtered_uids}))

    obs = VLMObservation(
        observation_id=_generate_observation_id(),
        page=page,
        chapter_uid=chapter,
        related_block_uids=sorted(scope_block_uids),
        model=vlm_client.model,
        image_sha256=image_sha256,
        prompt_sha256=prompt_sha256,
        findings=filtered_findings,
        raw_text=vlm_result.summary or None,
        created_at=_timestamp(),
        dpi=dpi,
        source_pdf=str(source_pdf.relative_to(paths.work_dir)),
    )

    # Step 9: Save
    save_vlm_observation(paths, obs)

    # Step 10: Return
    return obs, evidence_warning


def run_vlm_page(
    work: Path,
    page: int,
    dpi: int,
    out: Path | None,
    cfg: Config,
    *,
    chapter: str | None = None,
    blocks: list[str] | None = None,
) -> int:
    """Render a page, load its evidence, call VLM, and write result — never mutates book.json."""
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    obs, evidence_warning = _run_vlm_page_core(
        paths=paths,
        page=page,
        dpi=dpi,
        cfg=cfg,
        chapter=chapter,
        blocks=blocks,
    )

    # Backward compat: if out provided, also write legacy-format file
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        legacy_output: dict[str, object] = {
            "page": obs.page,
            "dpi": obs.dpi,
            "source_pdf": obs.source_pdf,
            "observation_id": obs.observation_id,
            "findings": [f.model_dump(mode="json") for f in obs.findings],
        }
        if evidence_warning:
            legacy_output["evidence_warning"] = evidence_warning
        out.write_text(
            json.dumps(legacy_output, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    findings_summary = [
        {"type": f.finding_type, "severity": f.severity}
        for f in obs.findings
    ]
    json_payload: dict[str, object] = {
        "observation_id": obs.observation_id,
        "page": obs.page,
        "output_path": str(out) if out is not None else None,
        "findings_count": len(obs.findings),
        "findings_summary": findings_summary,
        "model": obs.model,
    }
    if evidence_warning:
        json_payload["evidence_warning"] = evidence_warning
    emit_json(json_payload)
    return 0


def run_vlm_range(
    work: Path,
    start_page: int,
    end_page: int,
    dpi: int,
    cfg: Config,
    *,
    chapter: str | None = None,
    blocks: list[str] | None = None,
) -> int:
    """Analyze a range of pages with VLM, creating one observation per page."""
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    meta = load_editor_meta(paths)

    if meta.stage3 is None:
        raise CommandError(
            "edit_state/meta.json has no stage3 section. "
            "Re-initialize with `epubforge editor init` after running Stage 3."
        )

    if start_page > end_page:
        raise CommandError(f"start_page ({start_page}) > end_page ({end_page})")

    selected_set = set(meta.stage3.selected_pages)
    pages_in_range = [
        p for p in range(start_page, end_page + 1)
        if p in selected_set
    ]

    if not pages_in_range:
        raise CommandError(
            f"no selected pages in range [{start_page}, {end_page}]"
        )

    observation_ids: list[str] = []
    results: list[dict] = []

    for page in pages_in_range:
        obs, _warn = _run_vlm_page_core(
            paths=paths,
            page=page,
            dpi=dpi,
            cfg=cfg,
            chapter=chapter,
            blocks=blocks,
        )
        observation_ids.append(obs.observation_id)
        results.append({
            "observation_id": obs.observation_id,
            "page": page,
            "findings_count": len(obs.findings),
        })

    emit_json({
        "observation_ids": observation_ids,
        "pages_analyzed": len(pages_in_range),
        "total_findings": sum(r["findings_count"] for r in results),
        "per_page": results,
    })
    return 0


def run_render_prompt(
    work: Path,
    kind: Literal["scanner", "fixer", "reviewer"],
    chapter: str,
    issues: list[str] | None,
    cfg: Config,
) -> int:
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    book = load_editable_book(paths)
    _chapter_uid_or_error(book, chapter)
    memory = load_editor_memory(paths)
    meta = load_editor_meta(paths)
    prompt = render_prompt(
        kind=kind,
        book=book,
        memory=memory,
        work_dir=paths.work_dir,
        book_path=paths.book_path,
        chapter_uid=chapter,
        issues=_resolve_issues(issues),
        stage3=meta.stage3,
    )
    emit_text(prompt)
    return 0


def _chapter_projection_path(chapters_dir: Path, chapter_uid: str) -> Path:
    """Return a safe chapter projection path for a chapter UID."""
    if not chapter_uid:
        raise CommandError("chapter is missing uid")
    if "/" in chapter_uid or "\\" in chapter_uid or ".." in chapter_uid:
        raise CommandError(
            f"unsafe chapter uid for projection path: {chapter_uid}",
            payload={"error": f"unsafe chapter uid for projection path: {chapter_uid}"},
        )

    candidate = chapters_dir / f"{chapter_uid}.md"
    chapters_root = chapters_dir.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if not resolved_candidate.is_relative_to(chapters_root):
        raise CommandError(
            f"unsafe chapter uid for projection path: {chapter_uid}",
            payload={"error": f"unsafe chapter uid for projection path: {chapter_uid}"},
        )
    return candidate


def run_projection_export(
    work: Path,
    cfg: Config,
    *,
    chapter_uid: str | None = None,
) -> int:
    """Export the initialized editable Book to read-only projection files."""
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    book = load_editable_book(paths)
    if chapter_uid is None:
        chapters = book.chapters
    else:
        chapters = [chapter for chapter in book.chapters if chapter.uid == chapter_uid]
        if not chapters:
            available = [chapter.uid for chapter in book.chapters if chapter.uid]
            raise CommandError(
                f"chapter not found: {chapter_uid}",
                payload={
                    "error": f"chapter not found: {chapter_uid}",
                    "available_chapters": available,
                },
            )

    projection_dir = paths.edit_state_dir / "projections"
    chapters_dir = projection_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    chapter_targets = [
        (chapter, _chapter_projection_path(chapters_dir, chapter.uid or ""))
        for chapter in chapters
    ]

    if chapter_uid is not None:
        target_paths = {
            chapter_path.resolve(strict=False) for _chapter, chapter_path in chapter_targets
        }
        for stale_path in chapters_dir.glob("*.md"):
            if stale_path.resolve(strict=False) not in target_paths and stale_path.is_file():
                stale_path.unlink()

    exported_at = _timestamp()
    chapter_paths: list[str] = []
    blocks_written = 0
    for chapter, chapter_path in chapter_targets:
        atomic_write_text(chapter_path, render_chapter_projection(chapter))
        chapter_paths.append(str(chapter_path))
        blocks_written += len(chapter.blocks)

    index_book = book.model_copy(update={"chapters": list(chapters)})
    index_path = projection_dir / "index.md"
    atomic_write_text(
        index_path,
        render_index(
            index_book,
            source="edit_state/book.json",
            exported_at=exported_at,
        ),
    )

    emit_json(
        {
            "exported_at": exported_at,
            "projection_dir": str(projection_dir),
            "index_path": str(index_path),
            "chapters_written": len(chapters),
            "blocks_written": blocks_written,
            "chapter_paths": chapter_paths,
        }
    )
    return 0


def run_agent_output_begin(
    work: Path,
    kind: str,
    agent: str,
    chapter: str | None,
    cfg: Config,
) -> int:
    import typing
    from uuid import uuid4

    from epubforge.editor.agent_output import AgentKind, AgentOutput, save_agent_output

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    # Validate kind
    valid_kinds = list(typing.get_args(AgentKind))
    if kind not in valid_kinds:
        raise CommandError(
            f"--kind must be one of: {', '.join(valid_kinds)}",
            exit_code=2,
            payload={"error": f"--kind must be one of: {', '.join(valid_kinds)}"},
        )

    # Validate agent non-empty
    if not agent or not agent.strip():
        raise CommandError(
            "--agent must not be empty",
            exit_code=2,
            payload={"error": "--agent must not be empty"},
        )

    # scanner must specify --chapter
    if kind == "scanner" and chapter is None:
        raise CommandError(
            "scanner must specify --chapter",
            exit_code=2,
            payload={"error": "scanner must specify --chapter"},
        )

    # If chapter specified, verify it exists
    if chapter is not None:
        book = load_editable_book(paths)
        chapter_uid_set = {ch.uid for ch in book.chapters}
        if chapter not in chapter_uid_set:
            raise CommandError(
                f"chapter not found: {chapter}",
                exit_code=1,
                payload={"error": f"chapter not found: {chapter}"},
            )

    output_id = str(uuid4())
    now = _timestamp()
    output = AgentOutput(
        output_id=output_id,
        kind=kind,  # type: ignore[arg-type]
        agent_id=agent.strip(),
        chapter_uid=chapter,
        created_at=now,
        updated_at=now,
    )
    paths.agent_outputs_dir.mkdir(parents=True, exist_ok=True)
    save_agent_output(paths, output)
    path = paths.agent_outputs_dir / f"{output_id}.json"
    emit_json({"output_id": output_id, "path": str(path)})
    return 0


def run_agent_output_add_note(
    work: Path,
    output_id: str,
    text: str,
    cfg: Config,
) -> int:
    from epubforge.editor.agent_output import load_agent_output, save_agent_output

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    if not text or not text.strip():
        raise CommandError(
            "--text must not be empty",
            exit_code=2,
            payload={"error": "--text must not be empty"},
        )

    output = load_agent_output(paths, output_id)
    output.notes.append(text.strip())
    output.updated_at = _timestamp()
    save_agent_output(paths, output)
    emit_json({"output_id": output_id, "notes_count": len(output.notes)})
    return 0


def run_agent_output_add_question(
    work: Path,
    output_id: str,
    question: str,
    context_uids: list[str],
    options: list[str],
    cfg: Config,
) -> int:
    from uuid import uuid4

    from epubforge.editor.agent_output import load_agent_output, save_agent_output
    from epubforge.editor.memory import OpenQuestion

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    if not question or not question.strip():
        raise CommandError(
            "--question must not be empty",
            exit_code=2,
            payload={"error": "--question must not be empty"},
        )

    output = load_agent_output(paths, output_id)

    # Validate context_uids exist in book
    if context_uids:
        book = load_editable_book(paths)
        for uid in context_uids:
            found = False
            for chapter in book.chapters:
                if chapter.uid == uid:
                    found = True
                    break
                for block in chapter.blocks:
                    if block.uid == uid:
                        found = True
                        break
                if found:
                    break
            if not found:
                raise CommandError(
                    f"uid not found: {uid}",
                    exit_code=1,
                    payload={"error": f"uid not found: {uid}"},
                )

    q_id = str(uuid4())
    question_obj = OpenQuestion(
        q_id=q_id,
        question=question.strip(),
        context_uids=context_uids or [],
        options=options or [],
        asked_by=output.agent_id,
    )
    output.open_questions.append(question_obj)
    output.updated_at = _timestamp()
    save_agent_output(paths, output)
    emit_json(
        {
            "output_id": output_id,
            "q_id": q_id,
            "questions_count": len(output.open_questions),
        }
    )
    return 0


def run_agent_output_add_command(
    work: Path,
    output_id: str,
    command_file: Path,
    cfg: Config,
) -> int:
    from epubforge.editor.agent_output import load_agent_output, save_agent_output
    from epubforge.editor.patch_commands import PatchCommand

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    if not command_file.exists():
        raise CommandError(
            f"command file not found: {command_file}",
            exit_code=2,
            payload={"error": f"command file not found: {command_file}"},
        )

    try:
        parsed = json.loads(command_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommandError(
            f"invalid JSON: {exc.msg}",
            exit_code=1,
            payload={"error": f"invalid JSON: {exc.msg}"},
        ) from exc

    try:
        command = PatchCommand.model_validate(parsed)
    except Exception as exc:
        raise CommandError(
            f"PatchCommand validation failed: {exc}",
            exit_code=1,
            payload={"error": f"PatchCommand validation failed: {exc}"},
        ) from exc

    output = load_agent_output(paths, output_id)

    # Phase 2: warn (not error) if agent_id mismatch
    if command.agent_id != output.agent_id:
        import sys as _sys

        _sys.stderr.write(
            f"warning: command.agent_id {command.agent_id!r} != output.agent_id {output.agent_id!r}\n"
        )

    output.commands.append(command)
    output.updated_at = _timestamp()
    save_agent_output(paths, output)
    emit_json(
        {
            "output_id": output_id,
            "command_id": command.command_id,
            "commands_count": len(output.commands),
        }
    )
    return 0


def run_agent_output_add_patch(
    work: Path,
    output_id: str,
    patch_file: Path,
    cfg: Config,
) -> int:
    from epubforge.editor.agent_output import load_agent_output, save_agent_output
    from epubforge.editor.patches import BookPatch

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    if not patch_file.exists():
        raise CommandError(
            f"patch file not found: {patch_file}",
            exit_code=2,
            payload={"error": f"patch file not found: {patch_file}"},
        )

    try:
        parsed = json.loads(patch_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommandError(
            f"invalid JSON: {exc.msg}",
            exit_code=1,
            payload={"error": f"invalid JSON: {exc.msg}"},
        ) from exc

    try:
        patch = BookPatch.model_validate(parsed)
    except Exception as exc:
        raise CommandError(
            f"BookPatch validation failed: {exc}",
            exit_code=1,
            payload={"error": f"BookPatch validation failed: {exc}"},
        ) from exc

    output = load_agent_output(paths, output_id)

    # Check scope consistency
    if output.chapter_uid is not None and patch.scope.chapter_uid != output.chapter_uid:
        raise CommandError(
            f"patch scope mismatch: output.chapter_uid={output.chapter_uid!r}, "
            f"patch.scope.chapter_uid={patch.scope.chapter_uid!r}",
            exit_code=1,
            payload={
                "error": f"patch scope mismatch: output.chapter_uid={output.chapter_uid!r}, "
                f"patch.scope.chapter_uid={patch.scope.chapter_uid!r}"
            },
        )

    output.patches.append(patch)
    output.updated_at = _timestamp()
    save_agent_output(paths, output)
    emit_json(
        {
            "output_id": output_id,
            "patch_id": patch.patch_id,
            "patches_count": len(output.patches),
        }
    )
    return 0


def run_agent_output_add_memory_patch(
    work: Path,
    output_id: str,
    patch_file: Path,
    cfg: Config,
) -> int:
    from epubforge.editor.agent_output import load_agent_output, save_agent_output
    from epubforge.editor.memory import MemoryPatch

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    if not patch_file.exists():
        raise CommandError(
            f"patch file not found: {patch_file}",
            exit_code=2,
            payload={"error": f"patch file not found: {patch_file}"},
        )

    try:
        parsed = json.loads(patch_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommandError(
            f"invalid JSON: {exc.msg}",
            exit_code=1,
            payload={"error": f"invalid JSON: {exc.msg}"},
        ) from exc

    try:
        mp = MemoryPatch.model_validate(parsed)
    except Exception as exc:
        raise CommandError(
            f"MemoryPatch validation failed: {exc}",
            exit_code=1,
            payload={"error": f"MemoryPatch validation failed: {exc}"},
        ) from exc

    output = load_agent_output(paths, output_id)
    output.memory_patches.append(mp)
    output.updated_at = _timestamp()
    save_agent_output(paths, output)
    emit_json(
        {
            "output_id": output_id,
            "memory_patches_count": len(output.memory_patches),
        }
    )
    return 0


def run_agent_output_validate(
    work: Path,
    output_id: str,
    cfg: Config,
) -> int:
    from epubforge.editor.agent_output import load_agent_output, validate_agent_output

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    output = load_agent_output(paths, output_id)
    book = load_editable_book(paths)
    errors = validate_agent_output(output, book, paths=paths)
    emit_json({"valid": not errors, "output_id": output_id, "errors": errors})
    return 0 if not errors else 1


def run_agent_output_submit(
    work: Path,
    output_id: str,
    apply: bool,
    stage: bool,
    cfg: Config,
) -> int:
    if apply and stage:
        emit_json({"error": "--apply and --stage are mutually exclusive"})
        return 1

    from epubforge.editor.agent_output import (
        load_agent_output,
        stage_agent_output,
        submit_agent_output,
        validate_agent_output,
    )

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    output = load_agent_output(paths, output_id)
    book = load_editable_book(paths)

    if stage:
        result = stage_agent_output(output, book, paths, now=_timestamp())
        if not result.staged:
            emit_json(
                {"staged": False, "output_id": output_id, "errors": result.errors}
            )
            return 1
        emit_json(
            {
                "staged": True,
                "output_id": result.output_id,
                "patches_validated": result.patches_validated,
                "archive_path": result.archive_path,
            }
        )
        return 0

    # Dry-run mode (no --apply flag)
    if not apply:
        errors = validate_agent_output(output, book, paths=paths)
        emit_json({"valid": not errors, "output_id": output_id, "errors": errors})
        return 0 if not errors else 1

    # Apply mode
    memory = load_editor_memory(paths)
    now = _timestamp()
    result = submit_agent_output(output, book, memory, paths, now=now)

    if not result.submitted:
        emit_json({"submitted": False, "output_id": output_id, "errors": result.errors})
        return 1

    emit_json(
        {
            "submitted": True,
            "output_id": result.output_id,
            "patches_applied": result.patches_applied,
            "memory_patches_applied": result.memory_patches_applied,
            "archive_path": result.archive_path,
            "memory_decisions": result.memory_decisions,
        }
    )
    return 0


def run_workspace_create(work: Path, branch: str, base_ref: str = "HEAD") -> int:
    """Create a new Git worktree for agent use."""
    from epubforge.editor.workspace import GitError, create_worktree, find_repo_root

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)

    try:
        repo_root = find_repo_root(work.resolve())
    except GitError as exc:
        raise CommandError(
            str(exc),
            exit_code=1,
            payload={"error": str(exc), "kind": "not_a_repo"},
        ) from exc

    work_dir_rel = str(work.resolve().relative_to(repo_root))

    try:
        _validate_branch_name_for_cli(branch)
    except ValueError as exc:
        raise CommandError(
            str(exc),
            exit_code=2,
            payload={"error": str(exc), "kind": "invalid_branch"},
        ) from exc

    try:
        result = create_worktree(repo_root, branch, base_ref=base_ref)
    except ValueError as exc:
        raise CommandError(
            str(exc),
            exit_code=2,
            payload={"error": str(exc), "kind": "invalid_branch"},
        ) from exc
    except GitError as exc:
        raise CommandError(
            str(exc),
            exit_code=1,
            payload={"error": str(exc), "kind": "git_error"},
        ) from exc

    work_dir = result.worktree_path / work_dir_rel
    emit_json({
        "created": True,
        "worktree_path": str(result.worktree_path),
        "branch": result.branch,
        "work_dir": str(work_dir),
        "commit": result.commit,
        "base_ref": base_ref,
    })
    return 0


def _validate_branch_name_for_cli(branch: str) -> None:
    """Thin wrapper so we can call _validate_branch_name without importing workspace at module level."""
    from epubforge.editor.workspace import _validate_branch_name

    _validate_branch_name(branch)


def run_workspace_list(work: Path, agent_only: bool = False) -> int:
    """List Git worktrees, optionally filtered to agent/* branches."""
    from epubforge.editor.workspace import GitError, find_repo_root, list_worktrees

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)

    try:
        repo_root = find_repo_root(work.resolve())
    except GitError as exc:
        raise CommandError(
            str(exc),
            exit_code=1,
            payload={"error": str(exc), "kind": "not_a_repo"},
        ) from exc

    try:
        worktrees = list_worktrees(repo_root, agent_only=agent_only)
    except GitError as exc:
        raise CommandError(
            str(exc),
            exit_code=1,
            payload={"error": str(exc), "kind": "git_error"},
        ) from exc

    items = [
        {
            "path": str(wt.path),
            "branch": wt.branch,
            "commit": wt.commit,
            "is_main": wt.is_main,
            "is_bare": wt.is_bare,
            "prunable": wt.prunable,
        }
        for wt in worktrees
    ]
    emit_json({"worktrees": items, "count": len(items)})
    return 0


def run_workspace_merge(work: Path, branch: str, timeout: int = 60) -> int:
    """Merge an agent branch and validate the result semantically."""
    from epubforge.editor.workspace import GitError, find_repo_root, merge_and_validate

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)

    try:
        repo_root = find_repo_root(work.resolve())
    except GitError as exc:
        raise CommandError(
            str(exc),
            exit_code=1,
            payload={"error": str(exc), "kind": "not_a_repo"},
        ) from exc

    work_dir_rel = str(work.resolve().relative_to(repo_root))

    try:
        result = merge_and_validate(repo_root, work_dir_rel, branch, timeout=timeout)
    except GitError as exc:
        raise CommandError(
            str(exc),
            exit_code=1,
            payload={"error": str(exc), "kind": "git_error"},
        ) from exc

    payload: dict[str, object] = {
        "outcome": result.outcome.status,
        "message": result.outcome.message,
        "branch": result.branch,
        "merge_commit": result.merge_commit,
        "pre_merge_sha": result.pre_merge_sha,
        "base_sha256": result.base_sha256,
        "merged_sha256": result.merged_sha256,
        "change_count": result.change_count,
        "patch": result.patch_json,
        "conflict_files": result.conflict_files,
    }

    status = result.outcome.status
    if status == "accepted":
        emit_json(payload)
        return 0
    elif status == "git_conflict":
        emit_json(payload)
        return 1
    else:
        # semantic_conflict or parse_error
        emit_json(payload)
        return 2


def run_workspace_remove(work: Path, branch: str, force: bool = False) -> int:
    """Remove a Git worktree and optionally its branch."""
    from epubforge.editor.workspace import GitError, find_repo_root, remove_worktree

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)

    try:
        repo_root = find_repo_root(work.resolve())
    except GitError as exc:
        raise CommandError(
            str(exc),
            exit_code=1,
            payload={"error": str(exc), "kind": "not_a_repo"},
        ) from exc

    try:
        result = remove_worktree(repo_root, branch, force=force)
    except GitError as exc:
        exit_code = 2 if "main worktree" in str(exc) else 1
        raise CommandError(
            str(exc),
            exit_code=exit_code,
            payload={"error": str(exc), "kind": "git_error"},
        ) from exc

    emit_json({
        "removed": True,
        "worktree_path": str(result.worktree_path),
        "branch": result.branch,
        "branch_deleted": result.branch_deleted,
        "force_used": result.force_used,
    })
    return 0


def run_workspace_gc(work: Path, max_age_days: int = 7, dry_run: bool = False) -> int:
    """Garbage-collect orphaned agent worktrees older than max_age_days."""
    from epubforge.editor.workspace import GitError, find_repo_root, gc_worktrees

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)

    try:
        repo_root = find_repo_root(work.resolve())
    except GitError as exc:
        raise CommandError(
            str(exc),
            exit_code=1,
            payload={"error": str(exc), "kind": "not_a_repo"},
        ) from exc

    try:
        result = gc_worktrees(repo_root, max_age_days=max_age_days, dry_run=dry_run)
    except GitError as exc:
        raise CommandError(
            str(exc),
            exit_code=1,
            payload={"error": str(exc), "kind": "git_error"},
        ) from exc

    emit_json({
        "removed": [
            {
                "worktree_path": str(r.worktree_path),
                "branch": r.branch,
                "branch_deleted": r.branch_deleted,
                "force_used": r.force_used,
            }
            for r in result.removed
        ],
        "skipped": result.skipped,
        "pruned": result.pruned,
        "dry_run": dry_run,
    })
    return 0


__all__ = [
    "run_agent_output_add_command",
    "run_agent_output_add_memory_patch",
    "run_agent_output_add_note",
    "run_agent_output_add_patch",
    "run_agent_output_add_question",
    "run_agent_output_begin",
    "run_agent_output_submit",
    "run_agent_output_validate",
    "run_compact",
    "run_doctor",
    "run_init",
    "run_projection_export",
    "run_render_page",
    "run_render_prompt",
    "run_run_script",
    "run_vlm_page",
    "run_workspace_create",
    "run_workspace_gc",
    "run_workspace_list",
    "run_workspace_merge",
    "run_workspace_remove",
]
