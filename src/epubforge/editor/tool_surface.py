"""Stable business-logic surface for editor orchestration commands."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from epubforge.config import Config
from epubforge.editor.apply import ApplyError, apply_envelope
from epubforge.editor.cli_support import CommandError, emit_json, emit_text
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
from epubforge.editor.ops import OpEnvelope
from epubforge.editor.prompts import render_prompt
from epubforge.editor.scratch import allocate_script_path, run_script, write_script_stub
from epubforge.editor.state import (
    Stage3EditorMeta,
    book_id_from_paths,
    chapter_uids,
    clear_staging,
    default_init_source,
    ensure_initialized,
    ensure_uninitialized,
    ensure_work_dir,
    load_editable_book,
    load_editor_meta,
    load_editor_memory,
    load_lease_state,
    read_staging,
    resolve_editor_paths,
    save_leases,
    save_memory,
    write_initial_state,
    initialize_book_state,
)
from epubforge.io import load_book, save_book


class DoctorContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book_version: int
    memory: EditMemory
    report: DoctorReport


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


def _build_stage3_meta(work_dir: Path, book) -> Stage3EditorMeta | None:
    """Build Stage3EditorMeta from the active manifest and source book extraction info.

    Returns None if no active manifest exists (legacy workflow).
    Raises CommandError on mismatch between book extraction metadata and active manifest.
    """
    from epubforge.stage3_artifacts import Stage3ContractError, load_active_stage3_manifest

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
    if ex.stage3_manifest_sha256 is not None and ex.stage3_manifest_sha256 != pointer.manifest_sha256:
        raise CommandError(
            f"source book stage3_manifest_sha256 does not match active manifest. "
            "Run `epubforge assemble` to regenerate 05_semantic_raw.json."
        )

    # Determine evidence_index_path (workdir-relative from manifest sidecars)
    evidence_index_rel = manifest.sidecars.get("evidence_index", "")
    evidence_index_abs = str(work_dir / evidence_index_rel) if evidence_index_rel else ""

    # extraction_warnings_path: artifact dir / "warnings.json" (may not exist yet)
    from epubforge.stage3_artifacts import resolve_manifest_paths
    mpaths = resolve_manifest_paths(work_dir, manifest)
    artifact_dir = mpaths["artifact_dir"]
    extraction_warnings_path = str(artifact_dir / "warnings.json")

    return Stage3EditorMeta(
        mode=manifest.mode,
        skipped_vlm=(manifest.mode == "skip_vlm"),
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
    write_initial_state(paths, book=book, memory=memory, leases=LeaseState(), stage3=stage3)
    save_book(book, paths.work_dir)

    # Copy artifact audit_notes.json to edit_state/audit/extraction_notes.json
    if stage3 is not None:
        from epubforge.stage3_artifacts import Stage3ContractError, load_active_stage3_manifest
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
        "book_version": book.op_log_version,
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
    new_applied_op_count = 0
    if previous is not None:
        new_applied_op_count = max(0, book.op_log_version - previous.book_version)
    report = build_doctor_report(
        memory=memory,
        book=book,
        previous_memory=previous.memory if previous is not None else None,
        previous_report=previous.report if previous is not None else None,
        new_applied_op_count=new_applied_op_count,
    )
    paths.audit_dir.mkdir(parents=True, exist_ok=True)
    paths.doctor_report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    _save_doctor_context(paths.doctor_context_path, book_version=book.op_log_version, memory=memory, report=report)
    emit_json(report.model_dump(mode="json"))
    return 0


def run_propose_op(work: Path, payload_json: str, cfg: Config) -> int:
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise CommandError(f"stdin must be JSON: {exc.msg}")
    if not isinstance(payload, list):
        raise CommandError("stdin JSON must be an array of OpEnvelope objects")

    # Validate all envelopes first; only write if every one is valid (all-or-nothing).
    validated: list[OpEnvelope] = []
    errors: list[dict[str, object]] = []
    for index, item in enumerate(payload):
        try:
            validated.append(OpEnvelope.model_validate(item))
        except Exception as exc:  # noqa: BLE001
            errors.append({"index": index, "error": str(exc)})

    if errors:
        # Any validation failure → reject the entire batch; do not touch staging.jsonl.
        emit_json({"accepted": 0, "rejected": len(payload), "errors": errors})
        return 1

    if validated:
        from epubforge.editor.state import append_staging

        append_staging(paths, validated)
    emit_json({"accepted": len(validated), "rejected": 0, "errors": []})
    return 0


def run_apply_queue(work: Path, cfg: Config) -> int:
    paths = resolve_editor_paths(work)
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
        emit_json({"applied": 0, "rejected": 0, "new_version": book.op_log_version})
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
    result_payload: dict[str, object] = {"applied": applied_count, "rejected": rejected_count, "new_version": book.op_log_version}
    if errors:
        result_payload["errors"] = errors
    emit_json(result_payload)
    return 0 if rejected_count == 0 else 1


def run_acquire_lease(work: Path, chapter: str, agent: str, task: str, ttl: int | None, cfg: Config) -> int:
    resolved_ttl = ttl if ttl is not None else cfg.editor.lease_ttl_seconds

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    book = load_editable_book(paths)
    chapter_uid = _chapter_uid_or_error(book, chapter)
    state = load_lease_state(paths)
    lease = state.acquire_chapter(chapter_uid, agent, task, ttl=resolved_ttl, now=_timestamp())
    save_leases(paths, state)
    if lease is None:
        raise CommandError("chapter lease unavailable", raw_stdout="null")
    emit_json(lease.model_dump(mode="json"))
    return 0


def run_release_lease(work: Path, chapter: str, agent: str, cfg: Config) -> int:
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    state = load_lease_state(paths)
    released = state.release_chapter(chapter, agent, now=_timestamp())
    save_leases(paths, state)
    if released is None:
        raise CommandError("chapter lease not held by agent")
    emit_json({"released": True, "lease": released.model_dump(mode="json")})
    return 0


def run_acquire_book_lock(
    work: Path,
    agent: str,
    reason: Literal["topology_op", "compact", "init"],
    ttl: int | None,
    cfg: Config,
) -> int:
    resolved_ttl = ttl if ttl is not None else cfg.editor.book_exclusive_ttl_seconds

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    state = load_lease_state(paths)
    lease = state.acquire_book_exclusive(agent, reason, ttl=resolved_ttl, now=_timestamp())
    save_leases(paths, state)
    if lease is None:
        raise CommandError("book-exclusive lease unavailable", raw_stdout="null")
    emit_json(lease.model_dump(mode="json"))
    return 0


def run_release_book_lock(work: Path, agent: str, cfg: Config) -> int:
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    state = load_lease_state(paths)
    released = state.release_book_exclusive(agent, now=_timestamp())
    save_leases(paths, state)
    if released is None:
        raise CommandError("book-exclusive lease not held by agent")
    emit_json({"released": True, "lease": released.model_dump(mode="json")})
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
        path = write_script_stub(allocate_script_path(paths.work_dir, write, agent_id=agent))
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
    state = load_lease_state(paths)
    state.expire_stale(now=_timestamp())
    if state.book_exclusive is not None or state.chapter_leases:
        raise CommandError("cannot compact while leases are active")
    book = load_editable_book(paths)
    marker = compact_log(paths.edit_state_dir, book, ts=_timestamp())
    save_leases(paths, state)
    emit_json(marker.model_dump(mode="json"))
    return 0


def run_snapshot(work: Path, tag: str | None, cfg: Config) -> int:
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    resolved_tag = tag or _timestamp().replace(":", "-")
    destination = paths.snapshots_dir / resolved_tag
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

    resolved_out = out or (
        paths.audit_dir / "page_images" / f"page_{page:04d}.jpg"
    )

    _render_pdf_page_image(source_pdf, page, dpi, resolved_out)

    emit_json({
        "image_path": str(resolved_out),
        "page": page,
        "dpi": dpi,
        "source_pdf": str(source_pdf),
    })
    return 0


def run_vlm_page(
    work: Path,
    page: int,
    dpi: int,
    out: Path | None,
    cfg: Config,
) -> int:
    """Render a page, load its evidence, call VLM, and write result — never mutates book.json."""
    import base64

    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)

    meta = load_editor_meta(paths)
    if meta.stage3 is None:
        raise CommandError(
            "edit_state/meta.json has no stage3 section. "
            "Re-initialize with `epubforge editor init` after running Stage 3."
        )

    stage3 = meta.stage3

    # Validate page is in selected_pages
    if page not in stage3.selected_pages:
        raise CommandError(
            f"page {page} is not in selected pages {stage3.selected_pages}. "
            "Only selected pages have evidence and are eligible for VLM re-analysis."
        )

    source_pdf = paths.work_dir / stage3.source_pdf
    if not source_pdf.exists():
        raise CommandError(
            f"source PDF not found: {source_pdf}. "
            "Rerun parse with --force-rerun to restore source/source.pdf."
        )

    # Render page to a temp image
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_f:
        tmp_img_path = Path(tmp_f.name)

    try:
        _render_pdf_page_image(source_pdf, page, dpi, tmp_img_path)

        # Load evidence for this page
        evidence_items: list[object] = []
        evidence_warning: str | None = None
        if stage3.evidence_index_path:
            ev_path = Path(stage3.evidence_index_path)
            if ev_path.exists():
                from epubforge.stage3_artifacts import EvidenceIndex
                ev_index = EvidenceIndex.model_validate_json(ev_path.read_text(encoding="utf-8"))
                page_evidence = ev_index.pages.get(str(page), {})
                evidence_items = page_evidence.get("items", []) if isinstance(page_evidence, dict) else []
            else:
                evidence_warning = f"evidence_index not found: {ev_path}"
        else:
            evidence_warning = "no evidence_index_path in stage3 meta"

        if not evidence_items:
            evidence_warning = (evidence_warning or "") + f" (no evidence items for page {page})"

        # Build VLM messages
        img_bytes = tmp_img_path.read_bytes()
        img_b64 = base64.b64encode(img_bytes).decode("ascii")

        evidence_text = json.dumps(evidence_items, ensure_ascii=False, indent=2) if evidence_items else "[]"
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
                    "Review the image and identify any extraction issues, missing elements, "
                    "or items requiring semantic correction."
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
            },
        ]

        from epubforge.llm.client import LLMClient
        from pydantic import BaseModel as _BaseModel

        class _VLMPageResult(_BaseModel):
            page: int
            issues: list[str]
            suggestions: list[str]
            notes: str = ""

        vlm_client = LLMClient(cfg, use_vlm=True)
        from typing import cast as _cast
        from openai.types.chat import ChatCompletionMessageParam as _Msg
        messages = _cast(
            "list[_Msg]",
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        vlm_result = vlm_client.chat_parsed(messages, response_format=_VLMPageResult)

        resolved_out = out or (
            paths.audit_dir / "vlm_pages" / f"page_{page:04d}.json"
        )
        resolved_out.parent.mkdir(parents=True, exist_ok=True)

        output: dict[str, object] = {
            "page": page,
            "dpi": dpi,
            "source_pdf": str(source_pdf),
            "vlm_result": vlm_result.model_dump(mode="json"),
        }
        if evidence_warning:
            output["evidence_warning"] = evidence_warning

        resolved_out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        emit_json({"output_path": str(resolved_out), "page": page})
    finally:
        try:
            tmp_img_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

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


__all__ = [
    "run_acquire_book_lock",
    "run_acquire_lease",
    "run_apply_queue",
    "run_compact",
    "run_doctor",
    "run_init",
    "run_propose_op",
    "run_release_book_lock",
    "run_release_lease",
    "run_render_page",
    "run_render_prompt",
    "run_run_script",
    "run_snapshot",
    "run_vlm_page",
]
