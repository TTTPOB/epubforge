"""Pipeline orchestration for stages 1-4 plus explicit build."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path

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
    source_meta.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
        parse_pdf(source_pdf, out, images_dir=work / "images")
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


def run_extract(
    pdf_path: Path,
    cfg: Config,
    *,
    force: bool = False,
    pages: set[int] | None = None,
) -> None:
    from epubforge.extract import extract

    work = cfg.book_work_dir(pdf_path)
    raw = _stage_path(work, "01_raw.json")
    pages_json = _stage_path(work, "02_pages.json")
    out_dir = work / "03_extract"
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("Stage 3: extracting (VLM)…")
    with stage_timer(log, "3 extract"):
        extract(pdf_path, raw, pages_json, out_dir, cfg, force=force, page_filter=pages)
    log.info("  -> %s/", out_dir)


def run_assemble(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.assembler import assemble

    work = cfg.book_work_dir(pdf_path)
    out = _stage_path(work, "05_semantic_raw.json")
    if _skip(out, force, "assemble"):
        return
    log.info("Stage 4: assembling Semantic IR…")
    with stage_timer(log, "4 assemble"):
        assemble(work, out)
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
    with stage_timer(log, "8 build"):
        build_epub(
            semantic,
            out,
            images_dir=work / "images",
            registry_path=registry if registry.exists() else None,
        )
    log.info("  -> %s", out)
