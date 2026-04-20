"""Stage 5 — merge cleaned + VLM outputs into Semantic IR. Implement in epubforge-d81."""

from __future__ import annotations

from pathlib import Path


def assemble(work_dir: Path, out_path: Path) -> None:
    """Read stages 2-4 from *work_dir* and write Semantic IR JSON to *out_path*.

    Traverses pages in reading order, stitches simple-page LLM blocks and
    complex-page VLM blocks into ``Book`` → ``Chapter`` → ``Block`` hierarchy.
    Pairs footnote callouts ↔ bodies; unmatched footnotes are preserved as inline notes.
    """
    raise NotImplementedError("TODO: implement in epubforge-d81")
