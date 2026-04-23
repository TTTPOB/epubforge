from __future__ import annotations

import logging
import os
from pathlib import Path

import typer
from rich.console import Console

from epubforge.config import Config, load_config
from epubforge import pipeline
from epubforge.observability import get_tracker, log_path_for, setup_logging

app = typer.Typer(
    name="epubforge",
    help="LLM/VLM-assisted PDF → EPUB converter for books and theses.",
    no_args_is_help=True,
)
console = Console()

_config_path: Path | None = None
_log_level: str = "INFO"
_log_file_override: Path | None = None

log = logging.getLogger(__name__)


@app.callback()
def _global_options(
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to TOML config file (overrides config.toml / config.local.toml)"
    ),
    log_level: str = typer.Option(
        None, "--log-level", "-L",
        help="Logging level (DEBUG/INFO/WARNING). Env: EPUBFORGE_LOG_LEVEL",
    ),
    log_file: Path | None = typer.Option(
        None, "--log-file", help="Override log file path (default: work/<book>/logs/run-<ts>.log)"
    ),
) -> None:
    global _config_path, _log_level, _log_file_override
    _config_path = config
    _log_level = log_level or os.environ.get("EPUBFORGE_LOG_LEVEL", "INFO")
    _log_file_override = log_file


def _init_logging(cfg: Config, pdf_path: Path) -> Path | None:
    work_dir = cfg.book_work_dir(pdf_path)
    log_path = _log_file_override or log_path_for(work_dir)
    setup_logging(_log_level, log_path)
    return log_path


def _log_startup_banner(cfg: Config, log_path: Path | None) -> None:
    log.info(
        "epubforge startup: model=%s/%s cache_dir=%s editor=ttl:%d/compact:%d/max_loops:%d log=%s",
        cfg.llm_model, cfg.vlm_model, cfg.cache_dir,
        cfg.editor_lease_ttl_seconds,
        cfg.editor_compact_threshold,
        cfg.editor_max_loops,
        log_path or "(stderr only)",
    )


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
    force: bool = typer.Option(False, "--force-rerun", "-f", help="Re-run stages even if outputs exist"),
    from_stage: int = typer.Option(1, "--from", min=1, max=4, help="Start from stage N (1–4); existing outputs are reused unless --force-rerun"),
    pages: str | None = typer.Option(None, "--pages", help="Limit extraction to pages, e.g. '1-26' or '5,10-12'"),
) -> None:
    """Run the ingestion pipeline (parse → classify → extract → assemble)."""
    cfg = load_config(_config_path)
    cfg.require_llm()
    cfg.require_vlm()
    log_path = _init_logging(cfg, pdf_path)
    _log_startup_banner(cfg, log_path)
    pipeline.run_all(pdf_path, cfg, force=force, from_stage=from_stage, pages=_parse_pages(pages))


@app.command()
def parse(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 1 — Docling parse → work/<name>/01_raw.json."""
    cfg = load_config(_config_path)
    log_path = _init_logging(cfg, pdf_path)
    _log_startup_banner(cfg, log_path)
    pipeline.run_parse(pdf_path, cfg, force=force)


@app.command()
def classify(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 2 — classify pages as simple/complex → work/<name>/02_pages.json."""
    cfg = load_config(_config_path)
    log_path = _init_logging(cfg, pdf_path)
    _log_startup_banner(cfg, log_path)
    pipeline.run_classify(pdf_path, cfg, force=force)


@app.command()
def extract(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 3 — LLM+VLM extraction (simple and complex pages) → work/<name>/03_extract/."""
    cfg = load_config(_config_path)
    cfg.require_llm()
    cfg.require_vlm()
    log_path = _init_logging(cfg, pdf_path)
    _log_startup_banner(cfg, log_path)
    pipeline.run_extract(pdf_path, cfg, force=force)


@app.command()
def assemble(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 4 — merge into Semantic IR → work/<name>/05_semantic_raw.json."""
    cfg = load_config(_config_path)
    log_path = _init_logging(cfg, pdf_path)
    _log_startup_banner(cfg, log_path)
    pipeline.run_assemble(pdf_path, cfg, force=force)


@app.command()
def build(
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 8 — generate EPUB from edit_state/book.json or 05_semantic.json."""
    cfg = load_config(_config_path)
    log_path = _init_logging(cfg, pdf_path)
    _log_startup_banner(cfg, log_path)
    pipeline.run_build(pdf_path, cfg, force=force)
