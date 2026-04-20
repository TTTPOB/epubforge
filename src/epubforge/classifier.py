"""Stage 2 — rule-based page complexity classifier."""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from pathlib import Path

from docling_core.types.doc import (
    DocItemLabel,
    DoclingDocument,
)

_COMPLEX_LABELS: frozenset[DocItemLabel] = frozenset({
    DocItemLabel.TABLE,
    DocItemLabel.PICTURE,
    DocItemLabel.FOOTNOTE,
    DocItemLabel.FORMULA,
    DocItemLabel.CODE,
    DocItemLabel.CHART,
})

_MULTICOLUMN_GAP_RATIO = 0.05  # x-gap > 5% of page width → multi-column


def classify_pages(raw_path: Path, out_path: Path) -> None:
    """Read *raw_path* (Docling JSON) and write page classification to *out_path*.

    Output schema::

        {
          "pages": [
            {"page": 1, "kind": "simple"|"complex", "element_refs": ["..."]}
          ]
        }
    """
    doc = DoclingDocument.load_from_json(raw_path)

    page_info: dict[int, dict] = defaultdict(
        lambda: {"kind": "simple", "element_refs": [], "text_bboxes": []}
    )

    pages_meta: dict[int, float] = {
        pno: page.size.width for pno, page in doc.pages.items()
    }

    for item in itertools.chain(
        doc.texts, doc.tables, doc.pictures, doc.key_value_items, doc.form_items
    ):
        for prov in item.prov:
            pno = prov.page_no
            info = page_info[pno]
            info["element_refs"].append(item.self_ref)
            if item.label in _COMPLEX_LABELS:
                info["kind"] = "complex"
            if item.label == DocItemLabel.TEXT:
                info["text_bboxes"].append((prov.bbox.l, prov.bbox.r))

    for pno, info in page_info.items():
        if info["kind"] == "complex":
            continue
        page_width = pages_meta.get(pno, 595.0)
        if _is_multicolumn(info["text_bboxes"], page_width):
            info["kind"] = "complex"

    all_page_nos = sorted(page_info.keys())
    pages_out = [
        {
            "page": pno,
            "kind": page_info[pno]["kind"],
            "element_refs": page_info[pno]["element_refs"],
        }
        for pno in all_page_nos
    ]

    out_path.write_text(
        json.dumps({"pages": pages_out}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _is_multicolumn(bboxes: list[tuple[float, float]], page_width: float) -> bool:
    """Return True if text blocks form two or more distinct x-columns."""
    if len(bboxes) < 6:
        return False

    x_mids = sorted((l + r) / 2 for l, r in bboxes)
    gap_threshold = page_width * _MULTICOLUMN_GAP_RATIO

    # Look for a gap in x-midpoints that splits the page into ≥2 columns
    columns: list[list[float]] = [[x_mids[0]]]
    for x in x_mids[1:]:
        if x - columns[-1][-1] > gap_threshold:
            columns.append([])
        columns[-1].append(x)

    # Require columns to be meaningfully separated (gap > 5% page width)
    if len(columns) < 2:
        return False

    # Check that columns have distinct x ranges (not just one outlier)
    significant = [c for c in columns if len(c) >= 3]
    return len(significant) >= 2
