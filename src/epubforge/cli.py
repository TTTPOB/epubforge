from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from epubforge.config import Config, load_config
from epubforge import pipeline
from epubforge.editor.app import editor_app
from epubforge.observability import get_tracker, log_path_for, setup_logging

app = typer.Typer(
    name="epubforge",
    help="LLM/VLM-assisted PDF → EPUB converter for books and theses.",
    no_args_is_help=True,
)
console = Console()

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    config: Config
    log_file_override: Path | None


@app.callback()
def _global_options(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to TOML config file (no implicit scan; omit to use defaults + env)"
    ),
    log_level: str | None = typer.Option(
        None, "--log-level", "-L",
        help="Logging level (DEBUG/INFO/WARNING). Overrides config.runtime.log_level.",
    ),
    log_file: Path | None = typer.Option(
        None, "--log-file", help="Override log file path (default: work/<book>/logs/run-<ts>.log)"
    ),
) -> None:
    cfg = load_config(config_path=config)
    if log_level is not None:
        # CLI --log-level overrides config.runtime.log_level
        cfg = cfg.model_copy(update={"runtime": cfg.runtime.model_copy(update={"log_level": log_level})})
    ctx.obj = AppContext(config=cfg, log_file_override=log_file)


app.add_typer(editor_app, name="editor")


def _get_config(ctx: typer.Context) -> Config:
    """Retrieve effective config from root AppContext."""
    root_obj = ctx.find_root().obj
    if isinstance(root_obj, AppContext):
        return root_obj.config
    # Fallback for direct invocation without root callback (e.g. CliRunner tests)
    return load_config(None)


def _init_logging(cfg: Config, pdf_path: Path, log_file_override: Path | None) -> Path | None:
    work_dir = cfg.book_work_dir(pdf_path)
    log_path = log_file_override or log_path_for(work_dir)
    setup_logging(cfg.runtime.log_level, log_path)
    return log_path


def _log_startup_banner(cfg: Config, log_path: Path | None) -> None:
    log.info(
        "epubforge startup: model=%s/%s cache_dir=%s editor=ttl:%d/compact:%d/max_loops:%d"
        " skip_vlm=%s stage3_mode=%s max_vlm_batch_pages=%d log=%s",
        cfg.llm.model, cfg.vlm.model, cfg.runtime.cache_dir,
        cfg.editor.lease_ttl_seconds,
        cfg.editor.compact_threshold,
        cfg.editor.max_loops,
        cfg.extract.skip_vlm,
        "skip_vlm" if cfg.extract.skip_vlm else "vlm",
        cfg.extract.max_vlm_batch_pages,
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
            lo_str, hi_str = part.split("-", 1)
            lo_str = lo_str.strip()
            hi_str = hi_str.strip()
            try:
                lo = int(lo_str)
                hi = int(hi_str)
            except ValueError:
                raise typer.BadParameter(f"invalid page spec '{part}'", param_hint="--pages")
            if lo <= 0 or hi <= 0:
                raise typer.BadParameter(f"page number must be positive in range '{part}'", param_hint="--pages")
            if lo > hi:
                raise typer.BadParameter(f"reversed page range '{part}': start ({lo}) must be <= end ({hi})", param_hint="--pages")
            result.update(range(lo, hi + 1))
        else:
            try:
                page = int(part)
            except ValueError:
                raise typer.BadParameter(f"invalid page spec '{part}'", param_hint="--pages")
            if page <= 0:
                raise typer.BadParameter(f"page number must be positive, got {part}", param_hint="--pages")
            result.add(page)
    return result


@app.command()
def run(
    ctx: typer.Context,
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f", help="Re-run stages even if outputs exist"),
    from_stage: int = typer.Option(1, "--from", min=1, max=4, help="Start from stage N (1–4); existing outputs are reused unless --force-rerun"),
    pages: str | None = typer.Option(None, "--pages", help="Limit extraction to pages, e.g. '1-26' or '5,10-12'"),
    skip_vlm: bool | None = typer.Option(
        None,
        "--skip-vlm/--no-skip-vlm",
        help="Skip Stage 3 pipeline VLM and use a Docling-derived evidence draft",
    ),
) -> None:
    """Run the ingestion pipeline (parse → classify → extract → assemble)."""
    cfg = _get_config(ctx)
    if skip_vlm is not None:
        cfg = cfg.model_copy(update={"extract": cfg.extract.model_copy(update={"skip_vlm": skip_vlm})})
    app_ctx = ctx.find_root().obj
    log_file_override = app_ctx.log_file_override if isinstance(app_ctx, AppContext) else None
    log_path = _init_logging(cfg, pdf_path, log_file_override)
    _log_startup_banner(cfg, log_path)
    pipeline.run_all(pdf_path, cfg, force=force, from_stage=from_stage, pages=_parse_pages(pages))


@app.command()
def parse(
    ctx: typer.Context,
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 1 — Docling parse → work/<name>/01_raw.json."""
    cfg = _get_config(ctx)
    app_ctx = ctx.find_root().obj
    log_file_override = app_ctx.log_file_override if isinstance(app_ctx, AppContext) else None
    log_path = _init_logging(cfg, pdf_path, log_file_override)
    _log_startup_banner(cfg, log_path)
    pipeline.run_parse(pdf_path, cfg, force=force)


@app.command()
def classify(
    ctx: typer.Context,
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 2 — classify pages as simple/complex → work/<name>/02_pages.json."""
    cfg = _get_config(ctx)
    app_ctx = ctx.find_root().obj
    log_file_override = app_ctx.log_file_override if isinstance(app_ctx, AppContext) else None
    log_path = _init_logging(cfg, pdf_path, log_file_override)
    _log_startup_banner(cfg, log_path)
    pipeline.run_classify(pdf_path, cfg, force=force)


@app.command()
def extract(
    ctx: typer.Context,
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
    pages: str | None = typer.Option(None, "--pages", help="Limit extraction to pages, e.g. '1-26' or '5,10-12'"),
    skip_vlm: bool | None = typer.Option(
        None,
        "--skip-vlm/--no-skip-vlm",
        help="Skip Stage 3 pipeline VLM and use a Docling-derived evidence draft",
    ),
) -> None:
    """Stage 3 — VLM extraction → work/<name>/03_extract/."""
    cfg = _get_config(ctx)
    if skip_vlm is not None:
        cfg = cfg.model_copy(update={"extract": cfg.extract.model_copy(update={"skip_vlm": skip_vlm})})
    app_ctx = ctx.find_root().obj
    log_file_override = app_ctx.log_file_override if isinstance(app_ctx, AppContext) else None
    log_path = _init_logging(cfg, pdf_path, log_file_override)
    _log_startup_banner(cfg, log_path)
    pipeline.run_extract(pdf_path, cfg, force=force, pages=_parse_pages(pages))


@app.command()
def assemble(
    ctx: typer.Context,
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 4 — merge into Semantic IR → work/<name>/05_semantic_raw.json."""
    cfg = _get_config(ctx)
    app_ctx = ctx.find_root().obj
    log_file_override = app_ctx.log_file_override if isinstance(app_ctx, AppContext) else None
    log_path = _init_logging(cfg, pdf_path, log_file_override)
    _log_startup_banner(cfg, log_path)
    pipeline.run_assemble(pdf_path, cfg, force=force)


@app.command()
def build(
    ctx: typer.Context,
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 5 — generate EPUB from edit_state/book.json or 05_semantic.json."""
    cfg = _get_config(ctx)
    app_ctx = ctx.find_root().obj
    log_file_override = app_ctx.log_file_override if isinstance(app_ctx, AppContext) else None
    log_path = _init_logging(cfg, pdf_path, log_file_override)
    _log_startup_banner(cfg, log_path)
    pipeline.run_build(pdf_path, cfg, force=force)
