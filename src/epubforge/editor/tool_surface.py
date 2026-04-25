"""Stable business-logic surface for editor orchestration commands."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from epubforge.config import Config
from epubforge.editor.cli_support import CommandError, emit_json, emit_text
from epubforge.editor.doctor import DoctorReport, build_doctor_report
from epubforge.editor.log import compact_log, count_applied_log_events
from epubforge.editor.memory import EditMemory
from epubforge.editor.prompts import render_prompt
from epubforge.editor.scratch import allocate_script_path, run_script, write_script_stub
from epubforge.editor.state import (
    Stage3EditorMeta,
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
from epubforge.io import load_book, save_book


class DoctorContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied_event_count: int
    memory: EditMemory
    report: DoctorReport


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

        # Build VLM messages
        img_bytes = tmp_img_path.read_bytes()
        img_b64 = base64.b64encode(img_bytes).decode("ascii")

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

        resolved_out = out or (paths.audit_dir / "vlm_pages" / f"page_{page:04d}.json")
        resolved_out.parent.mkdir(parents=True, exist_ok=True)

        output: dict[str, object] = {
            "page": page,
            "dpi": dpi,
            "source_pdf": str(source_pdf),
            "vlm_result": vlm_result.model_dump(mode="json"),
        }
        if evidence_warning:
            output["evidence_warning"] = evidence_warning

        resolved_out.write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
    errors = validate_agent_output(output, book)
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
        errors = validate_agent_output(output, book)
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
    "run_render_page",
    "run_render_prompt",
    "run_run_script",
    "run_vlm_page",
]
