"""Stage 3 — unified LLM/VLM extraction with cross-unit pending_tail."""

from __future__ import annotations

import base64
import io
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict, cast

import fitz
from docling_core.types.doc import DocItemLabel, DoclingDocument
from docling_core.types.doc.base import BoundingBox
from docling_core.types.doc.document import DocItem

from epubforge.config import Config
from epubforge.ir.semantic import CleanOutput, VLMGroupOutput
from epubforge.llm.client import LLMClient, Message
from epubforge.llm.prompts import CLEAN_SYSTEM, VLM_SYSTEM

log = logging.getLogger(__name__)

_HEADER_LABELS = frozenset({DocItemLabel.SECTION_HEADER, DocItemLabel.TITLE})
_SKIP_LABELS = frozenset({
    DocItemLabel.PAGE_HEADER,
    DocItemLabel.PAGE_FOOTER,
})
_DPI = 150


class _AnchorItem(TypedDict):
    label: DocItemLabel
    text: str
    bbox: BoundingBox | None


@dataclass
class LLMGroupUnit:
    kind: str = "llm_group"
    pages: list[int] = field(default_factory=list)


@dataclass
class VLMPageUnit:
    kind: str = "vlm_page"
    pages: list[int] = field(default_factory=list)


Unit = LLMGroupUnit | VLMPageUnit


def extract(
    pdf_path: Path,
    raw_path: Path,
    pages_path: Path,
    out_dir: Path,
    cfg: Config,
    *,
    force: bool = False,
    page_filter: set[int] | None = None,
) -> None:
    """Unified Stage 3: process all units in PDF page order with cross-unit pending_tail."""
    doc = DoclingDocument.load_from_json(raw_path)
    pages_data: list[dict[str, Any]] = json.loads(
        pages_path.read_text(encoding="utf-8")
    )["pages"]
    if page_filter is not None:
        pages_data = [p for p in pages_data if p["page"] in page_filter]

    simple_set = {p["page"] for p in pages_data if p["kind"] == "simple"}
    pages_data = [p for p in pages_data if p["kind"] != "toc"]
    page_items = _build_page_items(doc, simple_set)
    anchors = _build_anchors(doc)

    pages_with_tables = _pages_with_tables(doc)
    units = _build_units(pages_data, page_items, pages_with_tables)

    llm_client = LLMClient(cfg, use_vlm=False)
    vlm_client = LLMClient(cfg, use_vlm=True)
    fitz_doc = fitz.open(str(pdf_path))

    pending_tail: dict[str, Any] | None = None
    pending_footnote: dict[str, Any] | None = None

    try:
        for idx, unit in enumerate(units):
            out_path = out_dir / f"unit_{idx:04d}.json"

            if out_path.exists() and not force:
                data = json.loads(out_path.read_text(encoding="utf-8"))
                blocks = data.get("blocks", [])
                pending_tail, pending_footnote = _extract_pending_context(blocks, unit)
                continue

            if isinstance(unit, LLMGroupUnit):
                blocks, flag, fn_flag = _process_llm_unit(unit, page_items, pending_tail, pending_footnote, llm_client)
            else:
                blocks, flag, fn_flag = _process_vlm_unit(unit, fitz_doc, anchors, pending_tail, pending_footnote, vlm_client)

            _write_unit(out_path, unit, blocks, flag, fn_flag)
            pending_tail, pending_footnote = _extract_pending_context(blocks, unit)
    finally:
        fitz_doc.close()


def _pages_with_tables(doc: DoclingDocument) -> set[int]:
    page_nos: set[int] = set()
    for table in doc.tables:
        for prov in table.prov:
            page_nos.add(prov.page_no)
    return page_nos


def _build_units(
    pages_data: list[dict[str, Any]],
    page_items: dict[int, list[dict[str, Any]]],
    pages_with_tables: set[int] | None = None,
) -> list[Unit]:
    if pages_with_tables is None:
        pages_with_tables = set()
    units: list[Unit] = []
    for page_info in pages_data:
        pno = page_info["page"]
        if page_info["kind"] == "complex":
            if (
                units
                and isinstance(units[-1], VLMPageUnit)
                and units[-1].pages[-1] + 1 == pno
                and units[-1].pages[-1] in pages_with_tables
                and pno in pages_with_tables
            ):
                units[-1].pages.append(pno)
            else:
                units.append(VLMPageUnit(pages=[pno]))
        else:
            units.append(LLMGroupUnit(pages=[pno]))
    return units


def _process_llm_unit(
    unit: LLMGroupUnit,
    page_items: dict[int, list[dict[str, Any]]],
    pending_tail: dict[str, Any] | None,
    pending_footnote: dict[str, Any] | None,
    client: LLMClient,
) -> tuple[list[dict[str, Any]], bool, bool]:
    items: list[dict[str, Any]] = []
    for pno in unit.pages:
        items.extend(page_items.get(pno, []))

    user_text = _format_blocks_for_llm(items)
    user_text = _prepend_pending(user_text, pending_tail, pending_footnote)

    messages: list[Message] = [
        {"role": "system", "content": CLEAN_SYSTEM},
        {"role": "user", "content": user_text},
    ]
    result = client.chat_parsed(messages, response_format=CleanOutput)
    blocks = [b.model_dump(exclude_none=True) for b in result.blocks]
    return blocks, result.first_block_continues_prev_tail, result.first_footnote_continues_prev_footnote


def _process_vlm_unit(
    unit: VLMPageUnit,
    fitz_doc: fitz.Document,
    anchors: dict[int, list[_AnchorItem]],
    pending_tail: dict[str, Any] | None,
    pending_footnote: dict[str, Any] | None,
    client: LLMClient,
) -> tuple[list[dict[str, Any]], bool, bool]:
    content: list[dict[str, Any]] = []

    for i, pno in enumerate(unit.pages):
        anchor_text = _format_anchors(anchors.get(pno, []))
        page_text = f"=== Page {pno} ===\nDetected text anchors:\n{anchor_text}"
        if i == 0:
            page_text = _prepend_pending(page_text, pending_tail, pending_footnote)
        content.append({"type": "text", "text": page_text})
        img_b64, mime = _render_page(fitz_doc, pno)
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}})

    if len(unit.pages) > 1:
        pages_str = " and ".join(str(p) for p in unit.pages)
        final_instruction = (
            f"Analyse pages {pages_str}. These pages may share a continuing table — "
            f"if a table on page {unit.pages[1]} (or later) continues from the previous page, "
            f"set continuation=true and omit the column header row. "
            f"Return one VLMPageOutput per input page in order."
        )
    else:
        final_instruction = f"Analyse page {unit.pages[0]} and return the structured JSON."
    content.append({"type": "text", "text": final_instruction})

    messages: list[Message] = cast(list[Message], [
        {"role": "system", "content": VLM_SYSTEM},
        {"role": "user", "content": content},
    ])

    try:
        result = client.chat_parsed(messages, response_format=VLMGroupOutput, temperature=0)
    except Exception as exc:
        log.warning("VLM call failed for pages %s: %s", unit.pages, exc)
        return [{"kind": "paragraph", "text": f"[VLM error: {exc}]"}], False, False

    if not result.pages:
        return [], False, False

    if len(result.pages) < len(unit.pages):
        log.warning(
            "VLM returned %d pages for %d-page unit %s",
            len(result.pages), len(unit.pages), unit.pages,
        )

    first_page_result = result.pages[0]
    flag = first_page_result.first_block_continues_prev_tail
    fn_flag = first_page_result.first_footnote_continues_prev_footnote

    blocks: list[dict[str, Any]] = []
    for i, page_result in enumerate(result.pages):
        pno = unit.pages[i] if i < len(unit.pages) else page_result.page
        for b in page_result.blocks:
            bd = b.model_dump(exclude_none=True)
            bd["page"] = pno
            blocks.append(bd)

    return blocks, flag, fn_flag


def _extract_pending_context(
    blocks: list[dict[str, Any]],
    unit: Unit,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (pending_tail, pending_footnote) for the next unit."""
    last_page = unit.pages[-1]
    pending_tail: dict[str, Any] | None = None
    pending_footnote: dict[str, Any] | None = None

    for i in range(len(blocks) - 1, -1, -1):
        b = blocks[i]
        kind = b.get("kind", "")
        if kind == "footnote":
            if pending_footnote is None:
                pending_footnote = {
                    "callout": b.get("callout", ""),
                    "text": b.get("text", ""),
                    "source_page": last_page,
                }
            continue
        if kind == "paragraph":
            pending_tail = {"text": b.get("text", ""), "source_page": last_page}
        break  # heading/table/figure/equation — stop scanning

    return pending_tail, pending_footnote


def _prepend_pending(
    user_text: str,
    pending_tail: dict[str, Any] | None,
    pending_footnote: dict[str, Any] | None,
) -> str:
    prefix = ""
    if pending_footnote:
        callout = pending_footnote["callout"]
        prefix += (
            f"[PENDING_FOOTNOTE callout={callout} page={pending_footnote['source_page']}]\n"
            f"{pending_footnote['text']}\n"
            f"[/PENDING_FOOTNOTE]\n\n"
        )
    if pending_tail:
        prefix += (
            f"[PENDING_TAIL page={pending_tail['source_page']}]\n"
            f"{pending_tail['text']}\n"
            f"[/PENDING_TAIL]\n\n"
        )
    return prefix + user_text if prefix else user_text


def _write_unit(
    out_path: Path,
    unit: Unit,
    blocks: list[dict[str, Any]],
    flag: bool,
    fn_flag: bool = False,
) -> None:
    data = {
        "unit": {"kind": unit.kind, "pages": unit.pages},
        "first_block_continues_prev_tail": flag,
        "first_footnote_continues_prev_footnote": fn_flag,
        "blocks": blocks,
    }
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_page_items(
    doc: DoclingDocument,
    page_set: set[int],
) -> dict[int, list[dict[str, Any]]]:
    page_items: dict[int, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, int]] = set()
    for item, _level in doc.iterate_items():
        if not isinstance(item, DocItem):
            continue
        text = getattr(item, "text", None)
        if not text:
            continue
        for prov in item.prov:
            pno = prov.page_no
            if pno not in page_set:
                continue
            key = (item.self_ref, pno)
            if key in seen:
                continue
            seen.add(key)
            page_items[pno].append({"label": item.label, "text": text, "page": pno})
    return page_items


def _build_anchors(doc: DoclingDocument) -> dict[int, list[_AnchorItem]]:
    import itertools
    anchors: dict[int, list[_AnchorItem]] = {}
    for item in itertools.chain(
        doc.texts, doc.tables, doc.pictures, doc.key_value_items, doc.form_items
    ):
        text = getattr(item, "text", "")
        for prov in item.prov:
            pno = prov.page_no
            anchors.setdefault(pno, []).append({
                "label": item.label,
                "text": text,
                "bbox": prov.bbox,
            })
    return anchors


def _format_blocks_for_llm(items: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for it in items:
        if it["label"] in _SKIP_LABELS:
            continue
        label_str = it["label"].value if isinstance(it["label"], DocItemLabel) else it["label"]
        prefix = f"[{label_str.upper()}] " if it["label"] in _HEADER_LABELS else ""
        pno = it.get("page", 0)
        chunks.append(f"[BLOCK p{pno}]\n{prefix}{it['text']}\n[/BLOCK]")
    return "\n".join(chunks)


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


def _render_page(doc: fitz.Document, page_no: int) -> tuple[str, str]:
    """Render a 1-indexed PDF page; return (base64_data, mime_type)."""
    page = doc[page_no - 1]
    mat = fitz.Matrix(_DPI / 72, _DPI / 72)
    pix = page.get_pixmap(matrix=mat)
    buf = io.BytesIO(pix.tobytes("jpg", jpg_quality=75))
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
