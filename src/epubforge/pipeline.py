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


def run_all(
    pdf_path: Path,
    cfg: Config,
    *,
    force: bool = False,
    from_stage: int = 1,
    pages: set[int] | None = None,
) -> None:
    if from_stage > 1:
        work = cfg.book_work_dir(pdf_path)
        _clear_from(work, cfg.book_out_path(pdf_path), from_stage)
    # Only run stages >= from_stage (earlier stages are assumed complete).
    # Stage 3 (extract) is skipped entirely when from_stage > 3 to avoid partial API calls.
    def _f(stage: int) -> bool:
        return force if stage >= from_stage else False

    run_parse(pdf_path, cfg, force=_f(1))
    run_classify(pdf_path, cfg, force=_f(2))
    if from_stage <= 3:
        run_extract(pdf_path, cfg, force=_f(3))
    run_assemble(pdf_path, cfg, force=_f(4))
    run_refine_toc(pdf_path, cfg, force=_f(5))
    run_build(pdf_path, cfg, force=_f(6))


def _clear_from(work: Path, epub_out: Path, from_stage: int) -> None:
    """Delete stage outputs for stages >= from_stage so downstream is re-run."""
    stage_files: dict[int, list[Path]] = {
        1: [work / "01_raw.json"],
        2: [work / "02_pages.json"],
        3: list((work / "03_extract").glob("*.json")) if (work / "03_extract").exists() else [],
        4: [work / "05_semantic_raw.json"],
        5: [work / "05_semantic.json"],
        6: [epub_out],
    }
    for stage in range(from_stage, 7):
        for p in stage_files.get(stage, []):
            if p.exists():
                p.unlink()
                console.print(f"[dim]cleared stage {stage}: {p.name}[/dim]")


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


def run_extract(
    pdf_path: Path,
    cfg: Config,
    *,
    force: bool = False,
) -> None:
    from epubforge.extract import extract

    work = cfg.book_work_dir(pdf_path)
    raw = _stage_path(work, "01_raw.json")
    pages = _stage_path(work, "02_pages.json")
    out_dir = work / "03_extract"
    out_dir.mkdir(parents=True, exist_ok=True)
    console.print("[bold]Stage 3:[/bold] extracting (LLM + VLM)…")
    extract(pdf_path, raw, pages, out_dir, cfg, force=force)
    console.print(f"  → {out_dir}/")


def run_assemble(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.assembler import assemble

    work = cfg.book_work_dir(pdf_path)
    out = _stage_path(work, "05_semantic_raw.json")
    if _skip(out, force, "assemble"):
        return
    console.print("[bold]Stage 4:[/bold] assembling Semantic IR…")
    assemble(work, out)
    console.print(f"  → {out}")


def run_refine_toc(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.toc_refiner import refine_toc

    work = cfg.book_work_dir(pdf_path)
    raw = _stage_path(work, "05_semantic_raw.json")
    out = _stage_path(work, "05_semantic.json")
    if _skip(out, force, "refine-toc"):
        return
    console.print("[bold]Stage 5:[/bold] refining TOC hierarchy…")
    refine_toc(raw, out, cfg)
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
