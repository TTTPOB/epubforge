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


def _pdf_arg() -> Path:
    """Dummy sentinel — replaced by Click parameter at runtime."""
    ...


@app.command()
def run(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-run all stages even if outputs exist"),
) -> None:
    """Run the full six-stage pipeline."""
    cfg = load_config()
    cfg.require_llm()
    cfg.require_vlm()
    pipeline.run_all(pdf_path, cfg, force=force)


@app.command()
def parse(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 1 — Docling parse → work/<name>/01_raw.json."""
    cfg = load_config()
    pipeline.run_parse(pdf_path, cfg, force=force)


@app.command()
def classify(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 2 — classify pages as simple/complex → work/<name>/02_pages.json."""
    cfg = load_config()
    pipeline.run_classify(pdf_path, cfg, force=force)


@app.command()
def clean(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 3 — LLM text cleaning of simple pages → work/<name>/03_simple/."""
    cfg = load_config()
    cfg.require_llm()
    pipeline.run_clean(pdf_path, cfg, force=force)


@app.command()
def vlm(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 4 — VLM structured reading of complex pages → work/<name>/04_complex/."""
    cfg = load_config()
    cfg.require_vlm()
    pipeline.run_vlm(pdf_path, cfg, force=force)


@app.command()
def assemble(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 5 — merge into Semantic IR → work/<name>/05_semantic.json."""
    cfg = load_config()
    pipeline.run_assemble(pdf_path, cfg, force=force)


@app.command()
def build(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Stage 6 — generate EPUB → out/<name>.epub."""
    cfg = load_config()
    pipeline.run_build(pdf_path, cfg, force=force)
