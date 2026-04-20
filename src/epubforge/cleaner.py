"""Stage 3 — LLM text cleaning of simple pages. Implement in epubforge-2u9."""

from __future__ import annotations

from pathlib import Path

from epubforge.config import Config


def clean_simple_pages(
    raw_path: Path,
    pages_path: Path,
    out_dir: Path,
    cfg: Config,
    *,
    force: bool = False,
) -> None:
    """Clean simple pages via LLM; write one JSON per section-group to *out_dir*."""
    raise NotImplementedError("TODO: implement in epubforge-2u9")
