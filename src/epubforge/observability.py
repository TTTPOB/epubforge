"""Logging setup and stage timing for the epubforge pipeline."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from rich.logging import RichHandler

_CONFIGURED = False


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> Path | None:
    """Idempotent. Attaches RichHandler to root logger + optional plain FileHandler."""
    global _CONFIGURED
    if _CONFIGURED:
        return log_file
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.addHandler(
        RichHandler(
            show_path=False,
            rich_tracebacks=True,
            log_time_format="[%X]",
            markup=False,
        )
    )
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
        )
        root.addHandler(fh)
    logging.getLogger("httpx").setLevel("WARNING")
    _CONFIGURED = True
    return log_file


def log_path_for(book_work_dir: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return book_work_dir / "logs" / f"run-{ts}.log"


@contextmanager
def stage_timer(log: logging.Logger, stage_name: str) -> Generator[None, None, None]:
    """Emit start/end INFO records with elapsed wall-clock time."""
    t0 = time.perf_counter()
    log.info("▶ Stage %s started", stage_name)
    try:
        yield
    except Exception:
        log.exception(
            "✖ Stage %s failed after %.1fs", stage_name, time.perf_counter() - t0
        )
        raise
    else:
        elapsed = time.perf_counter() - t0
        log.info("✔ Stage %s done in %.1fs", stage_name, elapsed)
