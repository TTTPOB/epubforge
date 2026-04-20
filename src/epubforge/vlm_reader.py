"""Stage 4 — VLM structured reading of complex pages. Implement in epubforge-2om."""

from __future__ import annotations

from pathlib import Path

from epubforge.config import Config


def read_complex_pages(
    pdf_path: Path,
    raw_path: Path,
    pages_path: Path,
    out_dir: Path,
    cfg: Config,
    *,
    force: bool = False,
) -> None:
    """Render each complex page (200 dpi PNG) and call VLM for structured JSON output.

    Writes one ``p{page:04d}.json`` per complex page into *out_dir*.
    Each file matches the ``VLMPageOutput`` schema from ``epubforge.ir.semantic``.
    """
    raise NotImplementedError("TODO: implement in epubforge-2om")
