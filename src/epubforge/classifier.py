"""Stage 2 — rule-based page complexity classifier. Implement in epubforge-51n."""

from __future__ import annotations

from pathlib import Path


def classify_pages(raw_path: Path, out_path: Path) -> None:
    """Read *raw_path* (Docling JSON) and write page classification to *out_path*.

    Output schema::

        {
          "pages": [
            {"page": 1, "kind": "simple"|"complex", "element_refs": ["..."]}
          ]
        }

    A page is 'complex' if it contains Table, Figure, Footnote, Formula, Code,
    or has a multi-column text layout.
    """
    raise NotImplementedError("TODO: implement in epubforge-51n")
