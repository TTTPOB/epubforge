"""Pipeline orchestration: stages 1-6 with per-stage caching."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from epubforge.config import Config

console = Console()


def _stage_path(work: Path, name: str) -> Path:
    return work / name


def _skip(path: Path, force: bool, label: str) -> bool:
    if path.exists() and not force:
        console.print(f"[dim]skip {label} — already exists ({path})[/dim]")
        return True
    return False


def run_all(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    run_parse(pdf_path, cfg, force=force)
    run_classify(pdf_path, cfg, force=force)
    run_clean(pdf_path, cfg, force=force)
    run_vlm(pdf_path, cfg, force=force)
    run_assemble(pdf_path, cfg, force=force)
    run_build(pdf_path, cfg, force=force)


def run_parse(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.parser.docling_parser import parse_pdf

    work = cfg.book_work_dir(pdf_path)
    out = _stage_path(work, "01_raw.json")
    if _skip(out, force, "parse"):
        return
    work.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]Stage 1:[/bold] parsing {pdf_path.name}…")
    parse_pdf(pdf_path, out, images_dir=work / "images")
    console.print(f"  → {out}")


def run_classify(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.classifier import classify_pages

    work = cfg.book_work_dir(pdf_path)
    raw = _stage_path(work, "01_raw.json")
    out = _stage_path(work, "02_pages.json")
    if _skip(out, force, "classify"):
        return
    console.print("[bold]Stage 2:[/bold] classifying pages…")
    classify_pages(raw, out)
    console.print(f"  → {out}")


def run_clean(
    pdf_path: Path,
    cfg: Config,
    *,
    force: bool = False,
    page_nos: set[int] | None = None,
) -> None:
    from epubforge.cleaner import clean_simple_pages

    work = cfg.book_work_dir(pdf_path)
    raw = _stage_path(work, "01_raw.json")
    pages = _stage_path(work, "02_pages.json")
    out_dir = work / "03_simple"
    out_dir.mkdir(parents=True, exist_ok=True)
    console.print("[bold]Stage 3:[/bold] LLM text cleaning…")
    clean_simple_pages(raw, pages, out_dir, cfg, force=force, page_nos=page_nos)
    console.print(f"  → {out_dir}/")


def run_vlm(
    pdf_path: Path,
    cfg: Config,
    *,
    force: bool = False,
    page_nos: set[int] | None = None,
) -> None:
    from epubforge.vlm_reader import read_complex_pages

    work = cfg.book_work_dir(pdf_path)
    raw = _stage_path(work, "01_raw.json")
    pages = _stage_path(work, "02_pages.json")
    out_dir = work / "04_complex"
    out_dir.mkdir(parents=True, exist_ok=True)
    console.print("[bold]Stage 4:[/bold] VLM complex-page reading…")
    read_complex_pages(pdf_path, raw, pages, out_dir, cfg, force=force, page_nos=page_nos)
    console.print(f"  → {out_dir}/")


def run_assemble(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.assembler import assemble

    work = cfg.book_work_dir(pdf_path)
    out = _stage_path(work, "05_semantic.json")
    if _skip(out, force, "assemble"):
        return
    console.print("[bold]Stage 5:[/bold] assembling Semantic IR…")
    assemble(work, out)
    console.print(f"  → {out}")


def run_build(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.epub_builder import build_epub

    work = cfg.book_work_dir(pdf_path)
    semantic = _stage_path(work, "05_semantic.json")
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.book_out_path(pdf_path)
    if _skip(out, force, "build"):
        return
    console.print("[bold]Stage 6:[/bold] building EPUB…")
    build_epub(semantic, out)
    console.print(f"  → {out}")
