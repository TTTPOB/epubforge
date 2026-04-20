"""Stage 1 — Docling PDF parser. Implement in epubforge-5k2."""

from __future__ import annotations

from pathlib import Path


def parse_pdf(pdf_path: Path, out_path: Path, *, images_dir: Path) -> None:
    """Parse *pdf_path* with Docling and write DoclingDocument JSON to *out_path*.

    Figure crops are saved under *images_dir* as p{page}_{ref_id}.png.
    """
    raise NotImplementedError("TODO: implement in epubforge-5k2")
