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
_SKIP_LABELS = frozenset({DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER})
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
    page_items = _build_page_items(doc, simple_set)
    anchors = _build_anchors(doc)

    units = _build_units(pages_data, page_items)

    llm_client = LLMClient(cfg, use_vlm=False)
    vlm_client = LLMClient(cfg, use_vlm=True)
    fitz_doc = fitz.open(str(pdf_path))

    pending_tail: dict[str, Any] | None = None

    try:
        for idx, unit in enumerate(units):
            out_path = out_dir / f"unit_{idx:04d}.json"

            if out_path.exists() and not force:
                data = json.loads(out_path.read_text(encoding="utf-8"))
                blocks = data.get("blocks", [])
                pending_tail = _extract_pending_tail(blocks, unit)
                continue

            if isinstance(unit, LLMGroupUnit):
                blocks, flag = _process_llm_unit(unit, page_items, pending_tail, llm_client)
            else:
                blocks, flag = _process_vlm_unit(unit, fitz_doc, anchors, pending_tail, vlm_client)

            _write_unit(out_path, unit, blocks, flag)
            pending_tail = _extract_pending_tail(blocks, unit)
    finally:
        fitz_doc.close()


def _build_units(
    pages_data: list[dict[str, Any]],
    page_items: dict[int, list[dict[str, Any]]],
) -> list[Unit]:
    units: list[Unit] = []
    buf: list[int] = []

    def flush_buf() -> None:
        if buf:
            units.append(LLMGroupUnit(pages=list(buf)))
            buf.clear()

    for page_info in pages_data:
        pno = page_info["page"]
        kind = page_info["kind"]

        if kind == "complex":
            flush_buf()
            units.append(VLMPageUnit(pages=[pno]))
        else:
            items = page_items.get(pno, [])
            starts_with_header = bool(items) and items[0]["label"] in _HEADER_LABELS
            if starts_with_header and buf:
                flush_buf()
            buf.append(pno)

    flush_buf()
    return units


def _process_llm_unit(
    unit: LLMGroupUnit,
    page_items: dict[int, list[dict[str, Any]]],
    pending_tail: dict[str, Any] | None,
    client: LLMClient,
) -> tuple[list[dict[str, Any]], bool]:
    items: list[dict[str, Any]] = []
    for pno in unit.pages:
        items.extend(page_items.get(pno, []))

    user_text = _format_blocks_for_llm(items)

    if pending_tail:
        prefix = (
            f"[PENDING_TAIL page={pending_tail['source_page']}]\n"
            f"{pending_tail['text']}\n"
            f"[/PENDING_TAIL]\n\n"
        )
        user_text = prefix + user_text

    messages: list[Message] = [
        {"role": "system", "content": CLEAN_SYSTEM},
        {"role": "user", "content": user_text},
    ]
    result = client.chat_parsed(messages, response_format=CleanOutput)
    blocks = [b.model_dump(exclude_none=True) for b in result.blocks]
    return blocks, result.first_block_continues_prev_tail


def _process_vlm_unit(
    unit: VLMPageUnit,
    fitz_doc: fitz.Document,
    anchors: dict[int, list[_AnchorItem]],
    pending_tail: dict[str, Any] | None,
    client: LLMClient,
) -> tuple[list[dict[str, Any]], bool]:
    pno = unit.pages[0]
    anchor_text = _format_anchors(anchors.get(pno, []))

    page_text = f"=== Page {pno} ===\nDetected text anchors:\n{anchor_text}"
    if pending_tail:
        page_text = (
            f"[PENDING_TAIL page={pending_tail['source_page']}]\n"
            f"{pending_tail['text']}\n"
            f"[/PENDING_TAIL]\n\n"
        ) + page_text

    img_b64, mime = _render_page(fitz_doc, pno)
    content: list[dict[str, Any]] = [
        {"type": "text", "text": page_text},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
        {"type": "text", "text": f"Analyse page {pno} and return the structured JSON."},
    ]

    messages: list[Message] = cast(list[Message], [
        {"role": "system", "content": VLM_SYSTEM},
        {"role": "user", "content": content},
    ])

    try:
        result = client.chat_parsed(messages, response_format=VLMGroupOutput, temperature=0)
    except Exception as exc:
        log.warning("VLM call failed for page %d: %s", pno, exc)
        return [{"kind": "paragraph", "text": f"[VLM error: {exc}]"}], False

    if not result.pages:
        return [], False

    page_result = result.pages[0]
    blocks = [b.model_dump(exclude_none=True) for b in page_result.blocks]
    return blocks, page_result.first_block_continues_prev_tail


def _extract_pending_tail(
    blocks: list[dict[str, Any]],
    unit: Unit,
) -> dict[str, Any] | None:
    last_page = unit.pages[-1]
    for i in range(len(blocks) - 1, -1, -1):
        b = blocks[i]
        kind = b.get("kind", "")
        if kind == "footnote":
            continue
        if kind == "paragraph":
            return {"text": b.get("text", ""), "source_page": last_page}
        return None  # heading/table/figure/equation → not continuable
    return None


def _write_unit(
    out_path: Path,
    unit: Unit,
    blocks: list[dict[str, Any]],
    flag: bool,
) -> None:
    data = {
        "unit": {"kind": unit.kind, "pages": unit.pages},
        "first_block_continues_prev_tail": flag,
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
        text = getattr(item, "text", "")[:200]
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
