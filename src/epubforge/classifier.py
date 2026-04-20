"""Stage 2 — rule-based page complexity classifier."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

# Labels that immediately make a page "complex"
_COMPLEX_LABELS: frozenset[str] = frozenset(
    {"table", "picture", "footnote", "formula", "code", "chart"}
)

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
    raw: dict[str, Any] = json.loads(raw_path.read_text(encoding="utf-8"))

    page_info: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"kind": "simple", "element_refs": [], "text_bboxes": []}
    )

    pages_meta: dict[int, dict[str, float]] = {}
    for page_no, page_data in (raw.get("pages") or {}).items():
        w = (page_data.get("size") or {}).get("width", 595.0)
        pages_meta[int(page_no)] = {"width": w}

    def _process_items(items: list[dict[str, Any]]) -> None:
        for item in items:
            label: str = item.get("label", "")
            ref: str = item.get("self_ref", "")
            for prov in item.get("prov") or []:
                page_no: int = prov.get("page_no", 0)
                info = page_info[page_no]
                info["element_refs"].append(ref)
                if label in _COMPLEX_LABELS:
                    info["kind"] = "complex"
                if label == "text":
                    bbox = prov.get("bbox") or {}
                    l_val = bbox.get("l")
                    r_val = bbox.get("r")
                    if l_val is not None and r_val is not None:
                        info["text_bboxes"].append((l_val, r_val))

    for collection in ("texts", "tables", "pictures", "key_value_items", "form_items"):
        _process_items(raw.get(collection) or [])

    for page_no, info in page_info.items():
        if info["kind"] == "complex":
            continue
        page_width = (pages_meta.get(page_no) or {}).get("width", 595.0)
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
