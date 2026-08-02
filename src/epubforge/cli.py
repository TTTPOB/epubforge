from __future__ import annotations

from collections.abc import Callable
import logging
from dataclasses import dataclass
from pathlib import Path

import typer

from epubforge.config import Config, load_config
from epubforge import pipeline
from epubforge.observability import log_path_for, setup_logging

app = typer.Typer(
    name="epubforge",
    help="MinerU-Luna PDF pipeline for corrected chapter HTML.",
    no_args_is_help=True,
)

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    config: Config
    log_file_override: Path | None


@app.callback()
def _global_options(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to TOML config file (no implicit scan; omit to use defaults + env)",
    ),
    log_level: str | None = typer.Option(
        None,
        "--log-level",
        "-L",
        help="Logging level (DEBUG/INFO/WARNING). Overrides config.runtime.log_level.",
    ),
    log_file: Path | None = typer.Option(
        None,
        "--log-file",
        help="Override log file path (default: work/<book>/logs/run-<ts>.log)",
    ),
) -> None:
    cfg = load_config(config_path=config)
    if log_level is not None:
        # CLI --log-level overrides config.runtime.log_level
        cfg = cfg.model_copy(
            update={"runtime": cfg.runtime.model_copy(update={"log_level": log_level})}
        )
    ctx.obj = AppContext(config=cfg, log_file_override=log_file)


def _get_config(ctx: typer.Context) -> Config:
    """Retrieve effective config from root AppContext."""
    root_obj = ctx.find_root().obj
    if isinstance(root_obj, AppContext):
        return root_obj.config
    # Fallback for direct invocation without root callback (e.g. CliRunner tests)
    return load_config(None)


def _init_logging(
    cfg: Config, pdf_path: Path, log_file_override: Path | None
) -> Path | None:
    work_dir = cfg.book_work_dir(pdf_path)
    log_path = log_file_override or log_path_for(work_dir)
    setup_logging(cfg.runtime.log_level, log_path)
    return log_path


def _log_startup_banner(cfg: Config, log_path: Path | None) -> None:
    log.info(
        "epubforge startup: model=%s cache_dir=%s log=%s",
        cfg.llm.model,
        cfg.runtime.cache_dir,
        log_path or "(stderr only)",
    )


def _run_logged_stage(
    ctx: typer.Context,
    pdf_path: Path,
    runner: Callable[..., None],
    *,
    force: bool,
    **kwargs: object,
) -> None:
    """Load command context, configure logging, and invoke one pipeline stage."""
    cfg = _get_config(ctx)
    root_obj = ctx.find_root().obj
    log_file_override = (
        root_obj.log_file_override if isinstance(root_obj, AppContext) else None
    )
    log_path = _init_logging(cfg, pdf_path, log_file_override)
    _log_startup_banner(cfg, log_path)
    runner(pdf_path, cfg, force=force, **kwargs)


@app.command()
def run(
    ctx: typer.Context,
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(
        False, "--force-rerun", "-f", help="Re-run stages even if outputs exist"
    ),
    from_stage: int = typer.Option(
        1,
        "--from",
        min=1,
        max=5,
        help="Start from stage N (1–5); existing outputs are reused unless --force-rerun",
    ),
    continue_on_error: bool = typer.Option(
        False,
        "--continue-on-error",
        help="Continue later chapter revisions after a chapter failure",
    ),
) -> None:
    """Run parse, normalize, segment, prepare, and revise through corrected HTML."""
    _run_logged_stage(
        ctx,
        pdf_path,
        pipeline.run_all,
        force=force,
        from_stage=from_stage,
        continue_on_error=continue_on_error,
    )


@app.command()
def parse(
    ctx: typer.Context,
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 1 — MinerU extraction → work/<name>/01_raw.zip."""
    _run_logged_stage(ctx, pdf_path, pipeline.run_parse, force=force)


@app.command()
def normalize(
    ctx: typer.Context,
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 2 — normalize 01_raw.zip → work/<name>/02_content/."""
    _run_logged_stage(ctx, pdf_path, pipeline.run_normalize, force=force)


@app.command()
def segment(
    ctx: typer.Context,
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 3 — detect chapter boundaries → work/<name>/03_chapters/."""
    _run_logged_stage(ctx, pdf_path, pipeline.run_segment, force=force)


@app.command()
def prepare(
    ctx: typer.Context,
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
) -> None:
    """Stage 4 — render chapter workspaces → work/<name>/04_edit/."""
    _run_logged_stage(ctx, pdf_path, pipeline.run_prepare, force=force)


@app.command()
def revise(
    ctx: typer.Context,
    pdf_path: Path = typer.Argument(..., help="Input PDF file"),
    force: bool = typer.Option(False, "--force-rerun", "-f"),
    continue_on_error: bool = typer.Option(
        False,
        "--continue-on-error",
        help="Continue later chapter revisions after a chapter failure",
    ),
) -> None:
    """Stage 5 — revise chapter workspaces → corrected.html."""
    _run_logged_stage(
        ctx,
        pdf_path,
        pipeline.run_revise,
        force=force,
        continue_on_error=continue_on_error,
    )
