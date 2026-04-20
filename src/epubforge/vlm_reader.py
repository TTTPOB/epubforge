"""Stage 4 — VLM structured reading of complex pages."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any, TypedDict

from docling_core.types.doc import DocItemLabel, DoclingDocument
from docling_core.types.doc.base import BoundingBox


class _AnchorItem(TypedDict):
    label: DocItemLabel
    text: str
    bbox: BoundingBox | None

import fitz  # PyMuPDF

from epubforge.config import Config
from epubforge.ir.semantic import VLMPageOutput
from epubforge.llm.client import LLMClient, Message
from epubforge.llm.prompts import VLM_SYSTEM

_DPI = 150
_SKIP_LABELS = frozenset({DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER})


def read_complex_pages(
    pdf_path: Path,
    raw_path: Path,
    pages_path: Path,
    out_dir: Path,
    cfg: Config,
    *,
    force: bool = False,
    page_nos: set[int] | None = None,
) -> None:
    """Render each complex page and call VLM for structured JSON output.

    Consecutive complex pages that both contain tables are grouped into one VLM
    call so that cross-page table continuations are handled correctly.
    """
    doc = DoclingDocument.load_from_json(raw_path)
    pages_data: list[dict[str, Any]] = json.loads(pages_path.read_text(encoding="utf-8"))["pages"]

    complex_pages = [p["page"] for p in pages_data if p["kind"] == "complex"]
    if page_nos is not None:
        complex_pages = [p for p in complex_pages if p in page_nos]

    anchors = _build_anchors(doc)
    table_pages = _pages_with_tables(doc)
    groups = _group_pages(complex_pages, table_pages)

    client = LLMClient(cfg, use_vlm=True)
    fitz_doc = fitz.open(str(pdf_path))

    for group in groups:
        # Skip if all outputs already exist
        out_paths = [out_dir / f"p{pno:04d}.json" for pno in group]
        if all(p.exists() for p in out_paths) and not force:
            continue

        results = _call_vlm_for_group(fitz_doc, group, anchors, client)

        for pno, result in zip(group, results):
            out_path = out_dir / f"p{pno:04d}.json"
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    fitz_doc.close()


def _group_pages(complex_pages: list[int], table_pages: set[int]) -> list[list[int]]:
    """Merge consecutive complex pages that both have tables (cross-page table heuristic)."""
    if not complex_pages:
        return []
    groups: list[list[int]] = [[complex_pages[0]]]
    for prev, curr in zip(complex_pages, complex_pages[1:]):
        if curr == prev + 1 and prev in table_pages and curr in table_pages:
            groups[-1].append(curr)
        else:
            groups.append([curr])
    return groups


def _call_vlm_for_group(
    doc: fitz.Document,
    group: list[int],
    anchors: dict[int, list[_AnchorItem]],
    client: LLMClient,
) -> list[dict[str, Any]]:
    """Send one or more pages to the VLM and return one result dict per page."""
    content: list[dict[str, Any]] = []

    for pno in group:
        anchor_text = _format_anchors(anchors.get(pno, []))
        content.append({
            "type": "text",
            "text": (
                f"=== Page {pno} ===\n"
                f"Detected text anchors:\n{anchor_text}"
            ),
        })
        img_b64, mime = _render_page(doc, pno)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{img_b64}"},
        })

    if len(group) > 1:
        instruction = (
            f"The {len(group)} pages above may contain a table that continues across pages. "
            "Return a JSON array — one object per page — each matching the VLMPageOutput schema. "
            "For any table on page N+1 that is a direct continuation of a table from page N "
            "(same logical table, only data rows, no column header), set \"continuation\": true "
            "on that table block. A table that starts fresh on a page must NOT have continuation: true."
        )
    else:
        instruction = f"Analyse page {group[0]} and return the structured JSON."

    content.append({"type": "text", "text": instruction})

    messages: list[Message] = [
        {"role": "system", "content": VLM_SYSTEM},
        {"role": "user", "content": content},
    ]

    try:
        raw_reply = client.chat(messages, response_format={"type": "json_object"}, temperature=1)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("VLM call failed for pages %s: %s", group, exc)
        return [{"page": pno, "blocks": [{"kind": "paragraph", "text": f"[VLM error: {exc}]"}]} for pno in group]

    return _parse_vlm_reply(raw_reply, group)


def _parse_vlm_reply(raw_reply: str, group: list[int]) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw_reply)
    except json.JSONDecodeError:
        return [{"page": pno, "blocks": [{"kind": "paragraph", "text": raw_reply}]} for pno in group]

    # Single-page: expect {"page": N, "blocks": [...]}
    if len(group) == 1:
        parsed["page"] = group[0]
        try:
            VLMPageOutput.model_validate(parsed)
        except Exception:
            parsed = {"page": group[0], "blocks": [{"kind": "paragraph", "text": raw_reply}]}
        return [parsed]

    # Multi-page: expect {"pages": [...]} or a top-level array
    pages_list: list[dict[str, Any]] = []
    if isinstance(parsed, list):
        pages_list = parsed
    elif "pages" in parsed:
        pages_list = parsed["pages"]
    else:
        pages_list = [parsed]

    results: list[dict[str, Any]] = []
    for i, pno in enumerate(group):
        entry = pages_list[i] if i < len(pages_list) else {}
        entry["page"] = pno
        try:
            VLMPageOutput.model_validate(entry)
        except Exception:
            entry = {"page": pno, "blocks": [{"kind": "paragraph", "text": str(entry)}]}
        results.append(entry)
    return results


def _render_page(doc: fitz.Document, page_no: int) -> tuple[str, str]:
    """Render a 1-indexed PDF page; return (base64_data, mime_type)."""
    page = doc[page_no - 1]
    mat = fitz.Matrix(_DPI / 72, _DPI / 72)
    pix = page.get_pixmap(matrix=mat)
    buf = io.BytesIO(pix.tobytes("jpg", jpg_quality=75))
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


def _build_anchors(doc: DoclingDocument) -> dict[int, list[_AnchorItem]]:
    import itertools
    anchors: dict[int, list[_AnchorItem]] = {}
    for item in itertools.chain(
        doc.texts, doc.tables, doc.pictures, doc.key_value_items, doc.form_items
    ):
        text = getattr(item, "text", "")[:200]
        for prov in item.prov:
            pno = prov.page_no
            anchors.setdefault(pno, []).append({
                "label": item.label,
                "text": text,
                "bbox": prov.bbox,
            })
    return anchors


def _pages_with_tables(doc: DoclingDocument) -> set[int]:
    return {prov.page_no for tbl in doc.tables for prov in tbl.prov}


def _format_anchors(items: list[_AnchorItem]) -> str:
    lines: list[str] = []
    for it in items:
        if it["label"] in _SKIP_LABELS:
            continue
        bbox = it["bbox"]
        if bbox is not None:
            coord = f"({bbox.l:.0f},{bbox.t:.0f},{bbox.r:.0f},{bbox.b:.0f})"
        else:
            coord = "(?,?,?,?)"
        label_str = it["label"].value if isinstance(it["label"], DocItemLabel) else it["label"]
        lines.append(f"  [{label_str}] {it['text']!r} @{coord}")
    return "\n".join(lines) if lines else "(none)"
