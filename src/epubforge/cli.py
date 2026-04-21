from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from epubforge.config import load_config
from epubforge import pipeline

app = typer.Typer(
    name="epubforge",
    help="LLM/VLM-assisted PDF → EPUB converter for books and theses.",
    no_args_is_help=True,
)
console = Console()

_config_path: Path | None = None


@app.callback()
def _global_options(
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to TOML config file (overrides config.toml / config.local.toml)"
    ),
) -> None:
    global _config_path
    _config_path = config


def _parse_pages(pages_str: str | None) -> set[int] | None:
    """Parse '5,10-12,20' into {5, 10, 11, 12, 20}."""
    if not pages_str:
        return None
    result: set[int] = set()
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            result.update(range(int(lo), int(hi) + 1))
        else:
            result.add(int(part))
    return result


@app.command()
def run(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-run all stages even if outputs exist"),
    from_stage: int = typer.Option(1, "--from", min=1, max=7, help="Clear and re-run from stage N (1–7)"),
    pages: str | None = typer.Option(None, "--pages", help="Limit extraction to pages, e.g. '1-26' or '5,10-12'"),
) -> None:
    """Run the full pipeline (parse → classify → extract → assemble → refine-toc → proofread → build)."""
    cfg = load_config(_config_path)
    cfg.require_llm()
    cfg.require_vlm()
    pipeline.run_all(pdf_path, cfg, force=force, from_stage=from_stage, pages=_parse_pages(pages))


@app.command()
def parse(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 1 — Docling parse → work/<name>/01_raw.json."""
    cfg = load_config(_config_path)
    pipeline.run_parse(pdf_path, cfg, force=force)


@app.command()
def classify(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 2 — classify pages as simple/complex → work/<name>/02_pages.json."""
    cfg = load_config(_config_path)
    pipeline.run_classify(pdf_path, cfg, force=force)


@app.command()
def extract(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 3 — LLM+VLM extraction (simple and complex pages) → work/<name>/03_extract/."""
    cfg = load_config(_config_path)
    cfg.require_llm()
    cfg.require_vlm()
    pipeline.run_extract(pdf_path, cfg, force=force)


@app.command()
def assemble(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 4 — merge into Semantic IR → work/<name>/05_semantic_raw.json."""
    cfg = load_config(_config_path)
    pipeline.run_assemble(pdf_path, cfg, force=force)


@app.command()
def refine_toc(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 5 — refine heading hierarchy with LLM → work/<name>/05_semantic.json."""
    cfg = load_config(_config_path)
    cfg.require_llm()
    pipeline.run_refine_toc(pdf_path, cfg, force=force)


@app.command()
def proofread(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 6 — book-level proofread → work/<name>/06_proofread.json."""
    cfg = load_config(_config_path)
    cfg.require_llm()
    pipeline.run_proofread(pdf_path, cfg, force=force)


@app.command()
def build(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 7 — generate EPUB → out/<name>.epub."""
    cfg = load_config(_config_path)
    pipeline.run_build(pdf_path, cfg, force=force)
