"""Pipeline orchestration: stages 1-7 with per-stage caching."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from epubforge.config import Config

console = Console()


def _stage_path(work: Path, name: str) -> Path:
    return work / name


def _skip(path: Path, force: bool, label: str) -> bool:
    if path.exists() and not force:
        console.print(f"[dim]skip {label} — reusing {path} (pass --force-rerun to re-run)[/dim]")
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
    # stages < from_stage use normal skip; stages >= from_stage are controlled by --force-rerun
    def _f(stage: int) -> bool:
        return force if stage >= from_stage else False

    run_parse(pdf_path, cfg, force=_f(1))
    run_classify(pdf_path, cfg, force=_f(2))
    run_extract(pdf_path, cfg, force=_f(3), pages=pages)
    run_assemble(pdf_path, cfg, force=_f(4))
    run_refine_toc(pdf_path, cfg, force=_f(5))
    run_proofread(pdf_path, cfg, force=_f(6), pages=pages)
    run_build(pdf_path, cfg, force=_f(7))



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
    pages: set[int] | None = None,
) -> None:
    from epubforge.extract import extract

    work = cfg.book_work_dir(pdf_path)
    raw = _stage_path(work, "01_raw.json")
    pages_json = _stage_path(work, "02_pages.json")
    out_dir = work / "03_extract"
    out_dir.mkdir(parents=True, exist_ok=True)
    console.print("[bold]Stage 3:[/bold] extracting (LLM + VLM)…")
    extract(pdf_path, raw, pages_json, out_dir, cfg, force=force, page_filter=pages)
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


def run_proofread(
    pdf_path: Path,
    cfg: Config,
    *,
    force: bool = False,
    pages: set[int] | None = None,
) -> None:
    from epubforge.proofreader import proofread

    work = cfg.book_work_dir(pdf_path)
    src = _stage_path(work, "05_semantic.json")
    out = _stage_path(work, "06_proofread.json")
    registry = _stage_path(work, "style_registry.json")
    if _skip(out, force, "proofread"):
        return
    console.print("[bold]Stage 6:[/bold] book-level proofreading…")
    proofread(src, out, registry, cfg, pages=pages)
    console.print(f"  → {out}")


def run_build(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.epub_builder import build_epub

    work = cfg.book_work_dir(pdf_path)
    proofread_out = _stage_path(work, "06_proofread.json")
    refined = _stage_path(work, "05_semantic.json")
    semantic = proofread_out if proofread_out.exists() else refined
    registry = _stage_path(work, "style_registry.json")
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.book_out_path(pdf_path)
    if _skip(out, force, "build"):
        return
    console.print("[bold]Stage 7:[/bold] building EPUB…")
    build_epub(
        semantic,
        out,
        images_dir=work / "images",
        registry_path=registry if registry.exists() else None,
    )
    console.print(f"  → {out}")
