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
    pages: str | None = typer.Option(None, "--pages", "-p", help="Page filter for clean/vlm e.g. '1-44'"),
) -> None:
    """Run the full pipeline (parse → classify → clean → vlm → assemble → refine-toc → build)."""
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
def clean(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
    pages: str | None = typer.Option(None, "--pages", "-p", help="Page filter e.g. '5,10-12'"),
) -> None:
    """Stage 3 — LLM text cleaning of simple pages → work/<name>/03_simple/."""
    cfg = load_config(_config_path)
    cfg.require_llm()
    pipeline.run_clean(pdf_path, cfg, force=force, page_nos=_parse_pages(pages))


@app.command()
def vlm(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
    pages: str | None = typer.Option(None, "--pages", "-p", help="Page filter e.g. '10,11,12'"),
) -> None:
    """Stage 4 — VLM structured reading of complex pages → work/<name>/04_complex/."""
    cfg = load_config(_config_path)
    cfg.require_vlm()
    pipeline.run_vlm(pdf_path, cfg, force=force, page_nos=_parse_pages(pages))


@app.command()
def assemble(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 5 — merge into Semantic IR → work/<name>/05_semantic.json."""
    cfg = load_config(_config_path)
    pipeline.run_assemble(pdf_path, cfg, force=force)


@app.command()
def refine_toc(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 5.5 — refine heading hierarchy with LLM → work/<name>/05_semantic.json."""
    cfg = load_config(_config_path)
    cfg.require_llm()
    pipeline.run_refine_toc(pdf_path, cfg, force=force)


@app.command()
def build(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 6 — generate EPUB → out/<name>.epub."""
    cfg = load_config(_config_path)
    pipeline.run_build(pdf_path, cfg, force=force)
