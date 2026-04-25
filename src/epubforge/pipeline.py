"""Pipeline orchestration for stages 1-4 plus explicit build."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from epubforge.config import Config
from epubforge.observability import get_tracker, stage_timer

log = logging.getLogger(__name__)


def _stage_path(work: Path, name: str) -> Path:
    return work / name


def _skip(path: Path, force: bool, label: str) -> bool:
    if path.exists() and not force:
        log.info("skip %s — reusing %s (pass --force-rerun to re-run)", label, path)
        return True
    return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _source_paths(work: Path) -> tuple[Path, Path]:
    source_dir = work / "source"
    return source_dir / "source.pdf", source_dir / "source_meta.json"


def _ensure_existing_parse_source(work: Path) -> None:
    source_pdf, source_meta = _source_paths(work)
    missing = [
        str(path.relative_to(work))
        for path in (source_pdf, source_meta)
        if not path.is_file()
    ]
    if missing:
        raise RuntimeError(
            "Existing parse output is missing stable source artifact(s): "
            f"{', '.join(missing)}. Rerun parse with --force-rerun."
        )


def _persist_source_pdf(pdf_path: Path, work: Path) -> tuple[Path, dict[str, object]]:
    source_pdf, source_meta = _source_paths(work)
    source_pdf.parent.mkdir(parents=True, exist_ok=True)

    original = pdf_path.resolve()
    if not original.is_file():
        raise FileNotFoundError(f"PDF not found: {original}")

    if source_pdf.exists():
        try:
            if source_pdf.samefile(original):
                pass
            else:
                source_pdf.unlink()
        except FileNotFoundError:
            pass

    if not source_pdf.exists():
        try:
            os.link(original, source_pdf)
            copy_method = "hardlink"
        except OSError:
            shutil.copy2(original, source_pdf)
            copy_method = "copy2"
    else:
        copy_method = "existing"

    if not os.access(source_pdf, os.R_OK):
        raise RuntimeError(f"Persisted source PDF is not readable: {source_pdf}")

    sha256 = _sha256_file(source_pdf)
    meta: dict[str, object] = {
        "source_pdf": "source/source.pdf",
        "original_pdf_abs": str(original),
        "sha256": sha256,
        "size_bytes": source_pdf.stat().st_size,
        "copied_at": datetime.now(timezone.utc).isoformat(),
    }
    source_meta.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    log.info(
        "parse source: original=%s target=%s sha256=%s size_bytes=%s method=%s",
        original,
        source_pdf,
        sha256,
        meta["size_bytes"],
        copy_method,
    )
    return source_pdf, meta


def run_all(
    pdf_path: Path,
    cfg: Config,
    *,
    force: bool = False,
    from_stage: int = 1,
    pages: set[int] | None = None,
) -> None:
    # stages < from_stage use normal skip; stages >= from_stage are controlled by --force-rerun
    def _f(stage: int) -> bool:
        return force if stage >= from_stage else False

    with stage_timer(log, "pipeline"):
        run_parse(pdf_path, cfg, force=_f(1))
        run_classify(pdf_path, cfg, force=_f(2))
        if from_stage >= 4:
            # run --from 4: only validate active artifact exists, never create a new one
            run_extract(pdf_path, cfg, force=False, pages=pages, reuse_only=True)
        else:
            run_extract(pdf_path, cfg, force=_f(3), pages=pages)
        run_assemble(pdf_path, cfg, force=_f(4))

    log.info("pipeline total: %s", get_tracker().summary_line())


def run_parse(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.parser.docling_parser import parse_pdf

    work = cfg.book_work_dir(pdf_path)
    out = _stage_path(work, "01_raw.json")
    if _skip(out, force, "parse"):
        _ensure_existing_parse_source(work)
        return
    work.mkdir(parents=True, exist_ok=True)
    source_pdf, _source_meta = _persist_source_pdf(pdf_path, work)
    log.info("Stage 1: parsing %s...", pdf_path.name)
    with stage_timer(log, "1 parse"):
        parse_pdf(
            source_pdf, out, images_dir=work / "images", ocr_settings=cfg.extract.ocr
        )
    log.info("  -> %s", out)


def run_classify(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.classifier import classify_pages

    work = cfg.book_work_dir(pdf_path)
    raw = _stage_path(work, "01_raw.json")
    out = _stage_path(work, "02_pages.json")
    if _skip(out, force, "classify"):
        return
    log.info("Stage 2: classifying pages…")
    with stage_timer(log, "2 classify"):
        classify_pages(raw, out)
    log.info("  -> %s", out)


def _parse_pages_json(pages_json: Path) -> tuple[list[int], list[int], list[int]]:
    """Read 02_pages.json and return (selected_pages, toc_pages, complex_pages).

    selected_pages: pages with kind != "toc", sorted.
    toc_pages: pages with kind == "toc", sorted.
    complex_pages: pages with kind == "complex", sorted.
    """
    data: dict[str, Any] = json.loads(pages_json.read_text(encoding="utf-8"))
    pages_data: list[dict[str, Any]] = data["pages"]
    selected_pages = sorted(p["page"] for p in pages_data if p["kind"] != "toc")
    toc_pages = sorted(p["page"] for p in pages_data if p["kind"] == "toc")
    complex_pages = sorted(p["page"] for p in pages_data if p["kind"] == "complex")
    return selected_pages, toc_pages, complex_pages


def _settings_for_artifact(cfg: Config) -> dict[str, Any]:
    """Build the settings snapshot used for artifact_id computation."""
    return {
        "skip_vlm": True,
        "contract_version": 3,
        "vlm_dpi": None,
        "max_vlm_batch_pages": None,
        "enable_book_memory": False,
        "vlm_model": None,
        "vlm_base_url": None,
    }


def run_extract(
    pdf_path: Path,
    cfg: Config,
    *,
    force: bool = False,
    pages: set[int] | None = None,
    reuse_only: bool = False,
) -> None:
    from epubforge.stage3_artifacts import (
        Stage3Manifest,
        active_manifest_matches_desired,
        activate_manifest_atomic,
        build_desired_stage3_manifest,
        load_active_stage3_manifest,
        validate_stage3_artifact,
        write_artifact_manifest_atomic,
    )

    work = cfg.book_work_dir(pdf_path)
    source_pdf = work / "source" / "source.pdf"
    raw = _stage_path(work, "01_raw.json")
    pages_json = _stage_path(work, "02_pages.json")

    # Validate prerequisite files exist
    for label, path in [
        ("source/source.pdf", source_pdf),
        ("01_raw.json", raw),
        ("02_pages.json", pages_json),
    ]:
        if not path.is_file():
            raise RuntimeError(
                f"Stage 3 requires {label} to exist in {work}. "
                "Run earlier pipeline stages first."
            )

    # Read SHAs for artifact_id computation
    source_pdf_sha256 = _sha256_file(source_pdf)
    raw_sha256 = _sha256_file(raw)
    pages_sha256 = _sha256_file(pages_json)

    # Parse pages classification
    selected_pages, toc_pages, complex_pages = _parse_pages_json(pages_json)

    # Apply pages filter
    page_filter: list[int] | None = None
    if pages is not None:
        page_filter = sorted(pages)
        selected_pages = sorted(p for p in selected_pages if p in pages)
        toc_pages = sorted(p for p in toc_pages if p in pages)
        complex_pages = sorted(p for p in complex_pages if p in pages)

    mode = "docling"
    settings = _settings_for_artifact(cfg)

    desired_artifact_id = build_desired_stage3_manifest(
        mode=mode,
        source_pdf_rel="source/source.pdf",
        source_pdf_sha256=source_pdf_sha256,
        raw_sha256=raw_sha256,
        pages_sha256=pages_sha256,
        selected_pages=selected_pages,
        toc_pages=toc_pages,
        complex_pages=complex_pages,
        page_filter=page_filter,
        settings=settings,
    )

    # Check for reusable active artifact (unless force=True)
    if not force:
        if active_manifest_matches_desired(work, desired_artifact_id):
            try:
                pointer, manifest = load_active_stage3_manifest(work)
                validate_stage3_artifact(work, manifest)
                log.info(
                    "Stage 3: reusing active artifact mode=%s artifact_id=%s manifest_sha256=%s",
                    manifest.mode,
                    manifest.artifact_id,
                    pointer.manifest_sha256,
                )
                log.info("Stage 3: provider_required=%s", False)
                return
            except Exception as exc:
                log.warning(
                    "Stage 3: active artifact validation failed (%s), will re-extract",
                    exc,
                )

    # Handle reuse_only mode: fail if we can't reuse
    if reuse_only:
        raise RuntimeError(
            f"Stage 3: no valid active artifact matching desired configuration "
            f"(artifact_id={desired_artifact_id}). "
            "Run `epubforge extract <pdf>` or `epubforge run <pdf> --from 3` first."
        )

    # Read old active artifact_id for logging
    old_artifact_id: str | None = None
    try:
        old_pointer, _ = load_active_stage3_manifest(work)
        old_artifact_id = old_pointer.active_artifact_id
    except Exception:
        log.debug("No prior artifact found, starting fresh")

    # Create artifact directory
    artifact_dir = work / "03_extract" / "artifacts" / desired_artifact_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    from epubforge.extract_skip_vlm import extract_skip_vlm

    log.info("Stage 3: extracting (Docling evidence draft)...")
    log.info("Stage 3: provider_required=%s", False)
    with stage_timer(log, "3 extract"):
        result = extract_skip_vlm(
            raw,
            pages_json,
            artifact_dir,
            force=force,
            page_filter=pages,
            images_dir=work / "images",
        )

    # Build and write manifest
    artifact_dir_rel = artifact_dir.relative_to(work).as_posix()
    unit_files_rel = [f.relative_to(work).as_posix() for f in result.unit_files]
    sidecars_rel = {
        "audit_notes": result.audit_notes_path.relative_to(work).as_posix(),
        "book_memory": result.book_memory_path.relative_to(work).as_posix(),
        "evidence_index": result.evidence_index_path.relative_to(work).as_posix(),
        "warnings": (
            result.warnings_path.relative_to(work).as_posix()
            if result.warnings_path is not None
            else (artifact_dir / "warnings.json").relative_to(work).as_posix()
        ),
    }

    from epubforge.stage3_artifacts import _now_utc_iso  # type: ignore[attr-defined]

    manifest = Stage3Manifest(
        mode=result.mode,
        artifact_id=desired_artifact_id,
        artifact_dir=artifact_dir_rel,
        created_at=_now_utc_iso(),
        raw_sha256=raw_sha256,
        pages_sha256=pages_sha256,
        source_pdf="source/source.pdf",
        source_pdf_sha256=source_pdf_sha256,
        selected_pages=selected_pages,
        toc_pages=toc_pages,
        complex_pages=complex_pages,
        page_filter=page_filter,
        unit_files=unit_files_rel,
        sidecars=sidecars_rel,
        settings=settings,
    )

    write_artifact_manifest_atomic(work, manifest)
    activate_manifest_atomic(work, manifest)

    log.info(
        "Stage 3: activated artifact_id=%s (previous=%s)",
        desired_artifact_id,
        old_artifact_id,
    )


def run_assemble(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.ir.semantic import Book, ExtractionMetadata
    from epubforge.stage3_artifacts import load_active_stage3_manifest

    work = cfg.book_work_dir(pdf_path)
    out = _stage_path(work, "05_semantic_raw.json")

    # 1. Load active Stage 3 manifest (fail if missing)
    pointer, manifest = load_active_stage3_manifest(work)

    # 2. Check freshness
    if not force and out.exists():
        try:
            book = Book.model_validate_json(out.read_text(encoding="utf-8"))
            if (
                book.extraction.artifact_id == pointer.active_artifact_id
                and book.extraction.stage3_manifest_sha256 == pointer.manifest_sha256
            ):
                log.info(
                    "Stage 4: skipping assemble (fresh: artifact_id=%s)",
                    pointer.active_artifact_id,
                )
                return
        except Exception:
            pass  # damaged/old format → rerun

    # 3. Assemble from manifest
    log.info(
        "Stage 4: assembling from manifest artifact_id=%s mode=%s...",
        manifest.artifact_id,
        manifest.mode,
    )
    from epubforge.assembler import assemble_from_manifest

    with stage_timer(log, "4 assemble"):
        book = assemble_from_manifest(work, manifest)

    # 4. Write Book.extraction metadata
    from pathlib import PurePosixPath as _PurePosix

    manifest_path_abs = work / _PurePosix(pointer.manifest_path)
    book.extraction = ExtractionMetadata(
        stage3_mode=manifest.mode,
        stage3_manifest_path=str(manifest_path_abs),
        stage3_manifest_sha256=pointer.manifest_sha256,
        artifact_id=manifest.artifact_id,
        selected_pages=manifest.selected_pages,
        complex_pages=manifest.complex_pages,
        source_pdf=manifest.source_pdf,
        evidence_index_path=manifest.sidecars.get("evidence_index", ""),
    )

    out.write_text(book.model_dump_json(indent=2), encoding="utf-8")
    log.info("  -> %s", out)


def run_build(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.epub_builder import build_epub, resolve_build_source

    work = cfg.book_work_dir(pdf_path)
    semantic = resolve_build_source(work)
    registry = _stage_path(work, "style_registry.json")
    cfg.runtime.out_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.book_out_path(pdf_path)
    if _skip(out, force, "build"):
        return
    log.info("Stage 5: building EPUB...")
    with stage_timer(log, "5 build"):
        build_epub(
            semantic,
            out,
            images_dir=work / "images",
            registry_path=registry if registry.exists() else None,
            work_dir=work,
        )
    log.info("  -> %s", out)
