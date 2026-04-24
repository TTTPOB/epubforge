"""Stage 3 — unified VLM extraction with mechanical batching and evidence index."""

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

from epubforge.config import Config
from epubforge.ir.book_memory import BookMemory
from epubforge.ir.semantic import VLMGroupOutput
from epubforge.llm.client import LLMClient, Message
from epubforge.llm.prompts import VLM_SYSTEM
from epubforge.stage3_artifacts import EvidenceIndex, Stage3ExtractionResult

log = logging.getLogger(__name__)

_HEADER_LABELS = frozenset({DocItemLabel.SECTION_HEADER, DocItemLabel.TITLE})
_SKIP_LABELS = frozenset(
    {
        DocItemLabel.PAGE_HEADER,
        DocItemLabel.PAGE_FOOTER,
    }
)


class _AnchorItem(TypedDict):
    label: DocItemLabel
    text: str
    bbox: BoundingBox | None


@dataclass
class VLMGroupUnit:
    kind: str = "vlm_batch"
    contract_version: int = 3
    pages: list[int] = field(default_factory=list)
    page_kinds: list[str] = field(
        default_factory=list
    )  # "simple" or "complex" per page


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
) -> Stage3ExtractionResult:
    """Stage 3: all selected non-TOC pages go through VLM with rolling BookMemory."""
    doc = DoclingDocument.load_from_json(raw_path)
    pages_data: list[dict[str, Any]] = json.loads(
        pages_path.read_text(encoding="utf-8")
    )["pages"]

    # Collect page metadata before filtering for result
    all_toc_pages = [p["page"] for p in pages_data if p["kind"] == "toc"]
    all_complex_pages = [p["page"] for p in pages_data if p["kind"] == "complex"]

    if page_filter is not None:
        pages_data = [p for p in pages_data if p["page"] in page_filter]

    selected_pages_data = [p for p in pages_data if p["kind"] != "toc"]
    selected_pages = [p["page"] for p in selected_pages_data]

    anchors = _build_anchors(doc)

    units = _build_units(
        selected_pages_data,
        anchors,
        max_vlm_batch=cfg.extract.max_vlm_batch_pages,
    )

    vlm_client = LLMClient(cfg, use_vlm=True)
    fitz_doc = fitz.open(str(pdf_path))

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
    unit_files: list[Path] = []

    all_pages = sorted({p for u in units for p in u.pages})
    log.info(
        "extract: %d units (all VLM), pages=%s",
        len(units),
        f"{all_pages[0]}-{all_pages[-1]}" if all_pages else "[]",
    )

    try:
        for idx, unit in enumerate(units):
            out_path = out_dir / f"unit_{idx:04d}.json"
            unit_files.append(out_path)

            if out_path.exists() and not force:
                data = json.loads(out_path.read_text(encoding="utf-8"))
                if "audit_notes" in data:
                    all_audit_notes.extend(data["audit_notes"])
                log.info(
                    "extract unit %d/%d pages=%s reused=Y",
                    idx + 1,
                    len(units),
                    unit.pages,
                )
                continue

            log.info(
                "extract unit %d/%d pages=%s reused=N",
                idx + 1,
                len(units),
                unit.pages,
            )

            memory_arg = book_memory if cfg.extract.enable_book_memory else None
            blocks, unit_audit_notes, new_memory = _process_vlm_unit(
                unit,
                fitz_doc,
                anchors,
                vlm_client,
                memory_arg,
                cfg.extract.vlm_dpi,
            )

            if cfg.extract.enable_book_memory and new_memory is not None:
                book_memory = new_memory
                book_memory_path.write_text(
                    book_memory.model_dump_json(indent=2), encoding="utf-8"
                )

            all_audit_notes.extend(unit_audit_notes)
            _write_unit(out_path, unit, blocks, unit_audit_notes)
    finally:
        fitz_doc.close()

    # Always write sidecars
    audit_path = out_dir / "audit_notes.json"
    audit_path.write_text(
        json.dumps(all_audit_notes, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Write book_memory.json (ensure it always exists even if memory disabled)
    if not book_memory_path.exists():
        book_memory_path.write_text(
            book_memory.model_dump_json(indent=2), encoding="utf-8"
        )

    # Build and write evidence index from Docling data
    evidence_index_path = out_dir / "evidence_index.json"
    evidence_index = _build_evidence_index(
        doc=doc,
        pages_data=selected_pages_data,
        raw_path=raw_path,
        artifact_id=out_dir.name,
    )
    evidence_index_path.write_text(
        evidence_index.model_dump_json(indent=2), encoding="utf-8"
    )

    warnings_path = out_dir / "warnings.json"
    warnings_path.write_text("[]", encoding="utf-8")

    return Stage3ExtractionResult(
        mode="vlm",
        unit_files=unit_files,
        audit_notes_path=audit_path,
        book_memory_path=book_memory_path,
        evidence_index_path=evidence_index_path,
        warnings_path=warnings_path,
        selected_pages=sorted(selected_pages),
        toc_pages=sorted(all_toc_pages),
        complex_pages=sorted(all_complex_pages),
    )


def _build_units(
    pages_data: list[dict[str, Any]],
    anchors: dict[int, list[_AnchorItem]],
    max_vlm_batch: int = 4,
) -> list[VLMGroupUnit]:
    """Build VLM processing units by batching consecutive pages up to max_vlm_batch.

    Pages are split into new chunks when:
    - The batch reaches max_vlm_batch size.
    - There is a gap (non-adjacent pages) in the --pages filter.
    No heuristics about labels or page content affect batching.
    """
    units: list[VLMGroupUnit] = []

    for page_info in pages_data:
        pno = page_info["page"]
        kind = page_info["kind"]  # "simple" or "complex"

        can_append = False
        if units:
            last = units[-1]
            last_pno = last.pages[-1]

            if len(last.pages) < max_vlm_batch and last_pno + 1 == pno:
                can_append = True

        if can_append:
            units[-1].pages.append(pno)
            units[-1].page_kinds.append(kind)
        else:
            units.append(VLMGroupUnit(pages=[pno], page_kinds=[kind]))

    return units


def _process_vlm_unit(
    unit: VLMGroupUnit,
    fitz_doc: fitz.Document,
    anchors: dict[int, list[_AnchorItem]],
    client: LLMClient,
    book_memory: BookMemory | None,
    dpi: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], BookMemory | None]:
    content: list[dict[str, Any]] = []

    if book_memory is not None:
        memory_text = (
            "[BOOK_MEMORY]\n"
            + json.dumps(book_memory.model_dump(), ensure_ascii=False, indent=2)
            + "\n[/BOOK_MEMORY]\n\n"
        )
        content.append({"type": "text", "text": memory_text})

    for pno in unit.pages:
        anchor_text = _format_anchors(anchors.get(pno, []))
        page_text = f"=== Page {pno} ===\nDetected text anchors:\n{anchor_text}"
        content.append({"type": "text", "text": page_text})
        img_b64, mime = _render_page(fitz_doc, pno, dpi)
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}}
        )

    if len(unit.pages) > 1:
        pages_str = ", ".join(str(p) for p in unit.pages)
        final_instruction = (
            f"These are selected adjacent pages: {pages_str}. "
            f"Analyse each page from the images and Docling evidence anchors provided. "
            f"Do not assume any content continues from a previous page — judge continuations "
            f"(such as tables, paragraphs, or footnotes) solely from the visual content and "
            f"evidence on these pages. "
            f"Return one VLMPageOutput per input page in order. "
            f"Update updated_book_memory with any new facts observed across these pages."
        )
    else:
        final_instruction = (
            f"Analyse page {unit.pages[0]} from the image and Docling evidence anchors provided. "
            f"Do not assume any content continues from a previous page — judge from the visual "
            f"content and evidence on this page only. "
            f"Return the structured JSON. "
            f"Update updated_book_memory with any new facts observed on this page."
        )
    content.append({"type": "text", "text": final_instruction})

    messages: list[Message] = cast(
        list[Message],
        [
            {"role": "system", "content": VLM_SYSTEM},
            {"role": "user", "content": content},
        ],
    )

    try:
        result = client.chat_parsed(messages, response_format=VLMGroupOutput)
    except Exception as exc:
        log.warning("VLM call failed for pages %s: %s", unit.pages, exc)
        return [{"kind": "paragraph", "text": f"[VLM error: {exc}]"}], [], None

    if not result.pages:
        return [], [], result.updated_book_memory

    if len(result.pages) < len(unit.pages):
        log.warning(
            "VLM returned %d pages for %d-page unit %s",
            len(result.pages),
            len(unit.pages),
            unit.pages,
        )

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

    new_memory = result.updated_book_memory

    return blocks, unit_audit_notes, new_memory


def _write_unit(
    out_path: Path,
    unit: VLMGroupUnit,
    blocks: list[dict[str, Any]],
    audit_notes: list[dict[str, Any]] | None = None,
) -> None:
    data = {
        "unit": {
            "kind": unit.kind,
            "pages": unit.pages,
            "page_kinds": unit.page_kinds,
            "extractor": "vlm",
            "contract_version": unit.contract_version,
        },
        "blocks": blocks,
        "audit_notes": audit_notes or [],
    }
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _build_anchors(doc: DoclingDocument) -> dict[int, list[_AnchorItem]]:
    import itertools

    anchors: dict[int, list[_AnchorItem]] = {}
    for item in itertools.chain(
        doc.texts, doc.tables, doc.pictures, doc.key_value_items, doc.form_items
    ):
        text = getattr(item, "text", "")
        for prov in item.prov:
            pno = prov.page_no
            anchors.setdefault(pno, []).append(
                {
                    "label": item.label,
                    "text": text,
                    "bbox": prov.bbox,
                }
            )
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
        label_str = (
            it["label"].value if isinstance(it["label"], DocItemLabel) else it["label"]
        )
        lines.append(f"  [{label_str}] {it['text']!r} @{coord}")
    return "\n".join(lines) if lines else "(none)"


def _render_page(doc: fitz.Document, page_no: int, dpi: int) -> tuple[str, str]:
    """Render a 1-indexed PDF page; return (base64_data, mime_type)."""
    page = doc[page_no - 1]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    buf = io.BytesIO(pix.tobytes("jpg", jpg_quality=75))
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


def _build_evidence_index(
    doc: DoclingDocument,
    pages_data: list[dict[str, Any]],
    raw_path: Path,
    artifact_id: str,
) -> EvidenceIndex:
    """Build a unified evidence index from Docling data for selected non-TOC pages."""
    import itertools

    selected_page_nos = {p["page"] for p in pages_data}
    page_kind_map = {p["page"]: p["kind"] for p in pages_data}

    # Collect all items indexed by page
    page_items: dict[int, list[dict[str, Any]]] = {pno: [] for pno in selected_page_nos}
    refs: dict[str, dict[str, Any]] = {}

    for item in itertools.chain(
        doc.texts, doc.tables, doc.pictures, doc.key_value_items, doc.form_items
    ):
        ref = getattr(item, "self_ref", None) or getattr(item, "_ref", None)

        text = getattr(item, "text", "")
        label = item.label
        label_str = label.value if isinstance(label, DocItemLabel) else str(label)

        for prov in item.prov:
            pno = prov.page_no
            if pno not in selected_page_nos:
                continue

            bbox = prov.bbox
            bbox_list: list[float] | None = None
            if bbox is not None:
                bbox_list = [bbox.l, bbox.t, bbox.r, bbox.b]

            item_index = len(page_items[pno])
            entry: dict[str, Any] = {
                "ref": ref,
                "page": pno,
                "source": "docling",
                "label": label_str,
                "text": text,
                "html": None,
                "bbox": bbox_list,
                "image_ref": None,
                "marker": None,
                "caption_refs": [],
                "footnote_refs": [],
                "reference_refs": [],
                "resolved_refs": [],
            }
            page_items[pno].append(entry)

            if ref is not None:
                refs[ref] = {"page": pno, "item_index": item_index}

    pages_dict: dict[str, Any] = {}
    for pno in sorted(selected_page_nos):
        pages_dict[str(pno)] = {
            "page": pno,
            "page_kind": page_kind_map.get(pno, "unknown"),
            "items": page_items[pno],
            "vlm_blocks": [],
        }

    # Use artifact_id as-is; source_pdf is relative to work dir
    source_pdf_rel = "source/source.pdf"

    return EvidenceIndex(
        schema_version=3,
        artifact_id=artifact_id,
        mode="vlm",
        source_pdf=source_pdf_rel,
        pages=pages_dict,
        refs=refs,
    )
