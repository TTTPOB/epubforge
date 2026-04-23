"""Stage 3 — unified VLM extraction with cross-unit pending_tail and BookMemory."""

from __future__ import annotations

import base64
import io
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict, cast

import fitz
from docling_core.types.doc import DocItemLabel, DoclingDocument
from docling_core.types.doc.base import BoundingBox
from docling_core.types.doc.document import DocItem

from epubforge.config import Config
from epubforge.ir.book_memory import BookMemory
from epubforge.ir.semantic import VLMGroupOutput
from epubforge.llm.client import LLMClient, Message
from epubforge.llm.prompts import VLM_SYSTEM

log = logging.getLogger(__name__)

_HEADER_LABELS = frozenset({DocItemLabel.SECTION_HEADER, DocItemLabel.TITLE})
_SKIP_LABELS = frozenset({
    DocItemLabel.PAGE_HEADER,
    DocItemLabel.PAGE_FOOTER,
})
_TABLE_LIKE_LABELS = frozenset({DocItemLabel.TABLE, DocItemLabel.PICTURE})
_BOTTOM_NOISE_LABELS = frozenset({DocItemLabel.FOOTNOTE, DocItemLabel.LIST_ITEM})


class _AnchorItem(TypedDict):
    label: DocItemLabel
    text: str
    bbox: BoundingBox | None


@dataclass
class VLMGroupUnit:
    kind: str = "vlm_group"
    pages: list[int] = field(default_factory=list)
    page_kinds: list[str] = field(default_factory=list)  # "simple" or "complex" per page


Unit = VLMGroupUnit


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
    """Stage 3: all pages go through VLM with rolling BookMemory."""
    doc = DoclingDocument.load_from_json(raw_path)
    pages_data: list[dict[str, Any]] = json.loads(
        pages_path.read_text(encoding="utf-8")
    )["pages"]
    if page_filter is not None:
        pages_data = [p for p in pages_data if p["page"] in page_filter]

    pages_data = [p for p in pages_data if p["kind"] != "toc"]
    anchors = _build_anchors(doc)

    units = _build_units(
        pages_data,
        anchors,
        max_simple_batch=cfg.extract.max_simple_batch_pages,
        max_complex_batch=cfg.extract.max_complex_batch_pages,
    )

    vlm_client = LLMClient(cfg, use_vlm=True)
    fitz_doc = fitz.open(str(pdf_path))

    pending_tail: dict[str, Any] | None = None
    pending_footnote: dict[str, Any] | None = None

    book_memory = BookMemory()
    book_memory_path = out_dir / "book_memory.json"
    if book_memory_path.exists() and not force:
        try:
            book_memory = BookMemory.model_validate_json(
                book_memory_path.read_text(encoding="utf-8")
            )
        except Exception:
            log.warning("extract: failed to load book_memory.json — starting fresh")

    all_audit_notes: list[dict[str, Any]] = []

    all_pages = sorted({p for u in units for p in u.pages})
    log.info(
        "extract: %d units (all VLM), pages=%s",
        len(units),
        f"{all_pages[0]}-{all_pages[-1]}" if all_pages else "[]",
    )

    try:
        for idx, unit in enumerate(units):
            out_path = out_dir / f"unit_{idx:04d}.json"

            if out_path.exists() and not force:
                data = json.loads(out_path.read_text(encoding="utf-8"))
                blocks = data.get("blocks", [])
                pending_tail, pending_footnote = _extract_pending_context(blocks, unit)
                if "audit_notes" in data:
                    all_audit_notes.extend(data["audit_notes"])
                log.info(
                    "extract unit %d/%d pages=%s reused=Y",
                    idx + 1, len(units), unit.pages,
                )
                continue

            log.info(
                "extract unit %d/%d pages=%s reused=N",
                idx + 1, len(units), unit.pages,
            )

            memory_arg = book_memory if cfg.extract.enable_book_memory else None
            blocks, flag, fn_flag, unit_audit_notes, new_memory = _process_vlm_unit(
                unit, fitz_doc, anchors, pending_tail, pending_footnote,
                vlm_client, memory_arg, cfg.extract.vlm_dpi,
            )

            if cfg.extract.enable_book_memory and new_memory is not None:
                book_memory = new_memory
                book_memory_path.write_text(
                    book_memory.model_dump_json(indent=2), encoding="utf-8"
                )

            all_audit_notes.extend(unit_audit_notes)
            _write_unit(out_path, unit, blocks, flag, fn_flag, unit_audit_notes)
            pending_tail, pending_footnote = _extract_pending_context(blocks, unit)
    finally:
        fitz_doc.close()

    audit_path = out_dir / "audit_notes.json"
    audit_path.write_text(
        json.dumps(all_audit_notes, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _build_units(
    pages_data: list[dict[str, Any]],
    anchors: dict[int, list[_AnchorItem]],
    max_simple_batch: int = 8,
    max_complex_batch: int = 12,
) -> list[VLMGroupUnit]:
    units: list[VLMGroupUnit] = []
    unit_page_kinds: list[str] = []  # tracks dominant kind of each unit ("simple"/"complex")

    for page_info in pages_data:
        pno = page_info["page"]
        kind = page_info["kind"]  # "simple" or "complex"

        can_append = False
        if units:
            last = units[-1]
            last_kind = unit_page_kinds[-1]
            last_pno = last.pages[-1]

            if kind == "simple" and last_kind == "simple":
                if len(last.pages) < max_simple_batch and last_pno + 1 == pno:
                    can_append = True
            elif kind == "complex" and last_kind == "complex":
                if (
                    len(last.pages) < max_complex_batch
                    and last_pno + 1 == pno
                    and _page_trailing_element_label(anchors.get(last_pno, []))
                    in _TABLE_LIKE_LABELS
                ):
                    can_append = True

        if can_append:
            units[-1].pages.append(pno)
            units[-1].page_kinds.append(kind)
        else:
            units.append(VLMGroupUnit(pages=[pno], page_kinds=[kind]))
            unit_page_kinds.append(kind)

    return units


def _page_trailing_element_label(
    items: list[_AnchorItem],
) -> DocItemLabel | None:
    """Return the label of the last meaningful element on the page.

    'Bottom 25%' footnote/list_items are treated as noise and excluded
    so that a paragraph above them (the real trailing content) is visible.
    """
    valid = [a for a in items if a["label"] not in _SKIP_LABELS and a["bbox"] is not None]
    if not valid:
        return None

    # In docling PDF-native coords: t = top y (larger = higher on page)
    t_vals = [a["bbox"].t for a in valid]  # type: ignore[union-attr]
    min_t, max_t = min(t_vals), max(t_vals)
    span = max_t - min_t

    # Bottom of page = small t. Bottom 25% threshold from the minimum.
    bottom_thresh = min_t + 0.25 * span if span > 0 else min_t

    filtered = [
        a for a in valid
        if not (a["label"] in _BOTTOM_NOISE_LABELS and a["bbox"].t <= bottom_thresh)  # type: ignore[union-attr]
    ]
    if not filtered:
        return None

    # Last in reading order = smallest t (lowest on page in PDF y-up coords)
    last = min(filtered, key=lambda a: a["bbox"].t)  # type: ignore[union-attr]
    return last["label"]


def _process_vlm_unit(
    unit: VLMGroupUnit,
    fitz_doc: fitz.Document,
    anchors: dict[int, list[_AnchorItem]],
    pending_tail: dict[str, Any] | None,
    pending_footnote: dict[str, Any] | None,
    client: LLMClient,
    book_memory: BookMemory | None,
    dpi: int,
) -> tuple[list[dict[str, Any]], bool, bool, list[dict[str, Any]], BookMemory | None]:
    content: list[dict[str, Any]] = []

    if book_memory is not None:
        memory_text = (
            "[BOOK_MEMORY]\n"
            + json.dumps(book_memory.model_dump(), ensure_ascii=False, indent=2)
            + "\n[/BOOK_MEMORY]\n\n"
        )
        content.append({"type": "text", "text": memory_text})

    for i, pno in enumerate(unit.pages):
        anchor_text = _format_anchors(anchors.get(pno, []))
        page_text = f"=== Page {pno} ===\nDetected text anchors:\n{anchor_text}"
        if i == 0:
            page_text = _prepend_pending(page_text, pending_tail, pending_footnote)
        content.append({"type": "text", "text": page_text})
        img_b64, mime = _render_page(fitz_doc, pno, dpi)
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}})

    if len(unit.pages) > 1:
        pages_str = " and ".join(str(p) for p in unit.pages)
        final_instruction = (
            f"Analyse pages {pages_str}. These pages may share a continuing table — "
            f"if a table on page {unit.pages[1]} (or later) continues from the previous page, "
            f"set continuation=true and omit the column header row. "
            f"Return one VLMPageOutput per input page in order. "
            f"Update updated_book_memory with any new facts observed across these pages."
        )
    else:
        final_instruction = (
            f"Analyse page {unit.pages[0]} and return the structured JSON. "
            f"Update updated_book_memory with any new facts observed on this page."
        )
    content.append({"type": "text", "text": final_instruction})

    messages: list[Message] = cast(list[Message], [
        {"role": "system", "content": VLM_SYSTEM},
        {"role": "user", "content": content},
    ])

    try:
        result = client.chat_parsed(messages, response_format=VLMGroupOutput, temperature=0)
    except Exception as exc:
        log.warning("VLM call failed for pages %s: %s", unit.pages, exc)
        return [{"kind": "paragraph", "text": f"[VLM error: {exc}]"}], False, False, [], None

    if not result.pages:
        return [], False, False, [], result.updated_book_memory

    if len(result.pages) < len(unit.pages):
        log.warning(
            "VLM returned %d pages for %d-page unit %s",
            len(result.pages), len(unit.pages), unit.pages,
        )

    first_page_result = result.pages[0]
    flag = first_page_result.first_block_continues_prev_tail
    fn_flag = first_page_result.first_footnote_continues_prev_footnote

    blocks: list[dict[str, Any]] = []
    unit_audit_notes: list[dict[str, Any]] = []
    for i, page_result in enumerate(result.pages):
        pno = unit.pages[i] if i < len(unit.pages) else page_result.page
        for b in page_result.blocks:
            bd = b.model_dump(exclude_none=True)
            bd["page"] = pno
            blocks.append(bd)
        for note in page_result.audit_notes:
            unit_audit_notes.append({**note.model_dump(), "page": pno})

    new_memory = result.updated_book_memory if result.updated_book_memory.model_fields_set else None
    # Always accept the VLM's updated memory (even if it's the default empty object)
    new_memory = result.updated_book_memory

    return blocks, flag, fn_flag, unit_audit_notes, new_memory


def _extract_pending_context(
    blocks: list[dict[str, Any]],
    unit: VLMGroupUnit,
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
        break

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
    unit: VLMGroupUnit,
    blocks: list[dict[str, Any]],
    flag: bool,
    fn_flag: bool = False,
    audit_notes: list[dict[str, Any]] | None = None,
) -> None:
    data = {
        "unit": {"kind": unit.kind, "pages": unit.pages},
        "first_block_continues_prev_tail": flag,
        "first_footnote_continues_prev_footnote": fn_flag,
        "blocks": blocks,
        "audit_notes": audit_notes or [],
    }
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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


def _render_page(doc: fitz.Document, page_no: int, dpi: int) -> tuple[str, str]:
    """Render a 1-indexed PDF page; return (base64_data, mime_type)."""
    page = doc[page_no - 1]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    buf = io.BytesIO(pix.tobytes("jpg", jpg_quality=75))
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
