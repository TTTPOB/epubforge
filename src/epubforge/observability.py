"""Logging setup, usage tracking, and stage timing for the epubforge pipeline."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
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
    root.addHandler(RichHandler(
        show_path=False,
        rich_tracebacks=True,
        log_time_format="[%X]",
        markup=False,
    ))
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s - %(message)s"
        ))
        root.addHandler(fh)
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("openai").setLevel("WARNING")
    _CONFIGURED = True
    return log_file


def log_path_for(book_work_dir: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return book_work_dir / "logs" / f"run-{ts}.log"


@dataclass
class UsageTracker:
    """Process-wide LLM/VLM call accounting. One global instance via get_tracker()."""
    requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    elapsed_s: float = 0.0

    def record_hit(self) -> None:
        self.requests += 1
        self.cache_hits += 1

    def record_miss(self, *, prompt: int, completion: int, elapsed: float) -> None:
        self.requests += 1
        self.cache_misses += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += prompt + completion
        self.elapsed_s += elapsed

    def snapshot(self) -> "UsageTracker":
        return UsageTracker(**self.__dict__)

    def delta(self, prev: "UsageTracker") -> "UsageTracker":
        return UsageTracker(**{k: getattr(self, k) - getattr(prev, k) for k in self.__dict__})

    def summary_line(self) -> str:
        hit_rate = (self.cache_hits / self.requests * 100) if self.requests else 0.0
        return (
            f"requests={self.requests} "
            f"cache_hit={self.cache_hits}/{self.requests} ({hit_rate:.0f}%) "
            f"tokens={self.prompt_tokens}p+{self.completion_tokens}c={self.total_tokens} "
            f"elapsed={self.elapsed_s:.1f}s"
        )


_tracker = UsageTracker()


def get_tracker() -> UsageTracker:
    return _tracker


@contextmanager
def stage_timer(log: logging.Logger, stage_name: str) -> Generator[None, None, None]:
    """Emit start/end INFO with elapsed time and per-stage LLM usage delta."""
    tr = get_tracker()
    before = tr.snapshot()
    t0 = time.perf_counter()
    log.info("▶ Stage %s started", stage_name)
    try:
        yield
    except Exception:
        log.exception("✖ Stage %s failed after %.1fs", stage_name, time.perf_counter() - t0)
        raise
    else:
        elapsed = time.perf_counter() - t0
        d = tr.delta(before)
        if d.requests:
            log.info("✔ Stage %s done in %.1fs — %s", stage_name, elapsed, d.summary_line())
        else:
            log.info("✔ Stage %s done in %.1fs", stage_name, elapsed)
