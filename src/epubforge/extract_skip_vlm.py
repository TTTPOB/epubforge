"""Stage 3 — skip-VLM Docling evidence-draft extractor.

Produces one ``docling_page`` unit per selected non-TOC page with mechanical
draft blocks derived from Docling labels.  No semantic decisions are made here:
no headings are inferred, no footnotes are structured, no continuation is set.
"""

from __future__ import annotations

import itertools
import json
import logging
from pathlib import Path
from typing import Any

from docling_core.types.doc import DocItemLabel, DoclingDocument, TableItem
from docling_core.types.doc.document import DocItem, PictureItem

from epubforge.ir.book_memory import BookMemory
from epubforge.ir.semantic import (
    Equation,
    Figure,
    Paragraph,
    Provenance,
    Table,
)
from epubforge.stage3_artifacts import (
    EvidenceIndex,
    Stage3ExtractionResult,
    Stage3Warning,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Label families
# ---------------------------------------------------------------------------

# Labels that map to Paragraph with a specific role
_BODY_LABELS = frozenset(
    {
        DocItemLabel.TEXT,
        DocItemLabel.PARAGRAPH,
        DocItemLabel.REFERENCE,
    }
)

_EVIDENCE_ONLY_LABELS = frozenset(
    {
        DocItemLabel.PAGE_HEADER,
        DocItemLabel.PAGE_FOOTER,
        DocItemLabel.DOCUMENT_INDEX,
        DocItemLabel.FORM,
        DocItemLabel.KEY_VALUE_REGION,
        DocItemLabel.FIELD_REGION,
        DocItemLabel.EMPTY_VALUE,
    }
)

_FIELD_LABELS = frozenset(
    {
        DocItemLabel.FIELD_HEADING,
        DocItemLabel.FIELD_ITEM,
        DocItemLabel.FIELD_KEY,
        DocItemLabel.FIELD_VALUE,
        DocItemLabel.FIELD_HINT,
    }
)

_CHECKBOX_LABELS = frozenset(
    {
        DocItemLabel.CHECKBOX_SELECTED,
        DocItemLabel.CHECKBOX_UNSELECTED,
    }
)

_PICTURE_LABELS = frozenset(
    {
        DocItemLabel.PICTURE,
        DocItemLabel.CHART,
    }
)

# Max items to record as leading / trailing per page
_EDGE_ITEM_COUNT = 3


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def extract_skip_vlm(
    raw_path: Path,
    pages_path: Path,
    out_dir: Path,
    *,
    force: bool = False,
    page_filter: set[int] | None = None,
    images_dir: Path | None = None,
) -> Stage3ExtractionResult:
    """Stage 3 skip-VLM: produce one unit per selected non-TOC page.

    Parameters
    ----------
    raw_path:
        Path to ``01_raw.json`` (DoclingDocument JSON).
    pages_path:
        Path to ``02_pages.json`` (page classification JSON).
    out_dir:
        Output artifact directory where units and sidecars are written.
    force:
        If *True*, overwrite existing unit files.
    page_filter:
        If given, only process pages in this set (still excludes TOC pages).
    """
    doc = DoclingDocument.load_from_json(raw_path)
    pages_data: list[dict[str, Any]] = json.loads(
        pages_path.read_text(encoding="utf-8")
    )["pages"]

    # Collect page metadata before filtering
    all_toc_pages = [p["page"] for p in pages_data if p["kind"] == "toc"]
    all_complex_pages = [p["page"] for p in pages_data if p["kind"] == "complex"]

    # Apply page_filter before selecting non-TOC
    if page_filter is not None:
        pages_data = [p for p in pages_data if p["page"] in page_filter]

    selected_pages_data = [p for p in pages_data if p["kind"] != "toc"]
    selected_page_nos: list[int] = [p["page"] for p in selected_pages_data]
    selected_set = set(selected_page_nos)

    out_dir.mkdir(parents=True, exist_ok=True)

    artifact_id = out_dir.name
    all_warnings: list[Stage3Warning] = []
    all_audit_notes: list[dict[str, Any]] = []
    unit_files: list[Path] = []

    log.info(
        "extract_skip_vlm: artifact=%s pages=%s",
        artifact_id,
        selected_page_nos,
    )

    for idx, page_info in enumerate(selected_pages_data):
        pno: int = page_info["page"]
        page_kind: str = page_info["kind"]

        out_path = out_dir / f"unit_{idx:04d}.json"
        unit_files.append(out_path)

        if out_path.exists() and not force:
            data = json.loads(out_path.read_text(encoding="utf-8"))
            if "audit_notes" in data:
                all_audit_notes.extend(data["audit_notes"])
            log.info(
                "extract_skip_vlm unit %d/%d page=%d reused=Y",
                idx + 1,
                len(selected_pages_data),
                pno,
            )
            continue

        log.info(
            "extract_skip_vlm unit %d/%d page=%d reused=N",
            idx + 1,
            len(selected_pages_data),
            pno,
        )

        (
            draft_blocks,
            evidence_refs,
            page_warnings,
            page_audit_notes,
            candidate_edges,
        ) = _process_page(
            doc, pno, page_kind, selected_set, artifact_id, images_dir=images_dir
        )

        all_warnings.extend(page_warnings)
        all_audit_notes.extend(page_audit_notes)

        _write_unit(
            out_path=out_path,
            pno=pno,
            page_kind=page_kind,
            draft_blocks=draft_blocks,
            evidence_refs=evidence_refs,
            candidate_edges=candidate_edges,
            audit_notes=page_audit_notes,
        )

    # Sidecars
    audit_path = out_dir / "audit_notes.json"
    audit_path.write_text(
        json.dumps(all_audit_notes, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    book_memory_path = out_dir / "book_memory.json"
    if not book_memory_path.exists():
        book_memory_path.write_text(
            BookMemory().model_dump_json(indent=2), encoding="utf-8"
        )

    evidence_index_path = out_dir / "evidence_index.json"
    evidence_index = _build_evidence_index(
        doc=doc,
        pages_data=selected_pages_data,
        artifact_id=artifact_id,
        images_dir=images_dir,
    )
    evidence_index_path.write_text(
        evidence_index.model_dump_json(indent=2), encoding="utf-8"
    )

    warnings_path = out_dir / "warnings.json"
    warnings_path.write_text(
        json.dumps(
            [w.model_dump() for w in all_warnings], ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )

    return Stage3ExtractionResult(
        mode="skip_vlm",
        unit_files=unit_files,
        audit_notes_path=audit_path,
        book_memory_path=book_memory_path,
        evidence_index_path=evidence_index_path,
        warnings_path=warnings_path,
        selected_pages=sorted(selected_page_nos),
        toc_pages=sorted(all_toc_pages),
        complex_pages=sorted(all_complex_pages),
        warnings=all_warnings,
    )


# ---------------------------------------------------------------------------
# Per-page processing
# ---------------------------------------------------------------------------


def _process_page(
    doc: DoclingDocument,
    pno: int,
    page_kind: str,
    selected_set: set[int],
    artifact_id: str,
    images_dir: Path | None = None,
) -> tuple[
    list[dict[str, Any]],  # draft_blocks (serialised)
    list[str],  # evidence_refs
    list[Stage3Warning],
    list[dict[str, Any]],  # audit_notes
    dict[str, Any],  # candidate_edges
]:
    """Extract draft blocks and evidence refs from one page of *doc*."""
    draft_blocks: list[dict[str, Any]] = []
    evidence_refs: list[str] = []
    warnings: list[Stage3Warning] = []
    audit_notes: list[dict[str, Any]] = []
    item_refs_in_order: list[str] = []

    for _node, _level in doc.iterate_items(page_no=pno):
        if not isinstance(_node, DocItem):
            continue
        item: DocItem = _node
        ref: str = item.self_ref
        label: DocItemLabel = item.label

        # Gather provenance from first prov entry on this page
        bbox_list: list[float] | None = None
        for prov in item.prov:
            if prov.page_no == pno:
                bbox = prov.bbox
                if bbox is not None:
                    bbox_list = [bbox.l, bbox.t, bbox.r, bbox.b]
                break

        provenance = Provenance(
            page=pno,
            bbox=bbox_list,
            source="docling",
            raw_ref=ref,
            raw_label=label.value,
            artifact_id=artifact_id,
            evidence_ref=ref,
        )

        evidence_refs.append(ref)
        item_refs_in_order.append(ref)

        block = _label_to_block(
            item=item,
            label=label,
            doc=doc,
            pno=pno,
            provenance=provenance,
            warnings=warnings,
            audit_notes=audit_notes,
            images_dir=images_dir,
        )

        if block is not None:
            draft_blocks.append(block.model_dump(exclude_none=True))

    # Candidate edges
    prev_page = pno - 1 if (pno - 1) in selected_set else None
    next_page = pno + 1 if (pno + 1) in selected_set else None

    leading = item_refs_in_order[:_EDGE_ITEM_COUNT]
    trailing = (
        item_refs_in_order[-_EDGE_ITEM_COUNT:]
        if len(item_refs_in_order) > _EDGE_ITEM_COUNT
        else []
    )

    candidate_edges: dict[str, Any] = {}
    if prev_page is not None:
        candidate_edges["previous_selected_page"] = prev_page
    if next_page is not None:
        candidate_edges["next_selected_page"] = next_page
    candidate_edges["leading_item_refs"] = leading
    candidate_edges["trailing_item_refs"] = trailing

    return draft_blocks, evidence_refs, warnings, audit_notes, candidate_edges


# ---------------------------------------------------------------------------
# Label → draft block mapping
# ---------------------------------------------------------------------------


def _label_to_block(
    *,
    item: Any,
    label: DocItemLabel,
    doc: DoclingDocument,
    pno: int,
    provenance: Provenance,
    warnings: list[Stage3Warning],
    audit_notes: list[dict[str, Any]],
    images_dir: Path | None = None,
) -> Paragraph | Equation | Figure | Table | None:
    """Map a Docling item to a draft block or return None (evidence only)."""

    # --- Plain body text ---
    if label in _BODY_LABELS:
        text = getattr(item, "text", "") or ""
        return Paragraph(text=text, role="body", provenance=provenance)

    # --- Title candidate ---
    if label == DocItemLabel.TITLE:
        text = getattr(item, "text", "") or ""
        return Paragraph(
            text=text, role="docling_title_candidate", provenance=provenance
        )

    # --- Section header candidate ---
    if label == DocItemLabel.SECTION_HEADER:
        text = getattr(item, "text", "") or ""
        return Paragraph(
            text=text, role="docling_heading_candidate", provenance=provenance
        )

    # --- Footnote candidate (not Footnote IR) ---
    if label == DocItemLabel.FOOTNOTE:
        text = getattr(item, "text", "") or ""
        return Paragraph(
            text=text, role="docling_footnote_candidate", provenance=provenance
        )

    # --- List item candidate (not list structure) ---
    if label == DocItemLabel.LIST_ITEM:
        text = getattr(item, "text", "") or ""
        return Paragraph(
            text=text, role="docling_list_item_candidate", provenance=provenance
        )

    # --- Caption candidate (not attributed to figure/table) ---
    if label == DocItemLabel.CAPTION:
        text = getattr(item, "text", "") or ""
        return Paragraph(
            text=text, role="docling_caption_candidate", provenance=provenance
        )

    # --- Code (verbatim) ---
    if label == DocItemLabel.CODE:
        text = getattr(item, "text", "") or ""
        return Paragraph(text=text, role="code", provenance=provenance)

    # --- Handwritten text --- warning
    if label == DocItemLabel.HANDWRITTEN_TEXT:
        text = getattr(item, "text", "") or ""
        msg = f"Handwritten text detected on page {pno} (ref={item.self_ref})"
        log.warning(msg)
        warnings.append(Stage3Warning(message=msg, page=pno, item_ref=item.self_ref))
        audit_notes.append(
            {
                "kind": "other",
                "page": pno,
                "hint": "handwritten_text detected",
                "block_index": None,
            }
        )
        return Paragraph(
            text=text, role="docling_handwritten_candidate", provenance=provenance
        )

    # --- Field labels (only when non-empty text) ---
    if label in _FIELD_LABELS:
        text = getattr(item, "text", "") or ""
        if text:
            return Paragraph(
                text=text, role="docling_field_candidate", provenance=provenance
            )
        return None

    # --- Checkbox labels (only when non-empty text) ---
    if label in _CHECKBOX_LABELS:
        text = getattr(item, "text", "") or ""
        if text:
            return Paragraph(
                text=text, role="docling_checkbox_candidate", provenance=provenance
            )
        return None

    # --- Grading scale — field candidate + warning ---
    if label == DocItemLabel.GRADING_SCALE:
        text = getattr(item, "text", "") or ""
        msg = f"Grading scale detected on page {pno} (ref={item.self_ref})"
        log.warning(msg)
        warnings.append(Stage3Warning(message=msg, page=pno, item_ref=item.self_ref))
        audit_notes.append(
            {
                "kind": "other",
                "page": pno,
                "hint": "grading_scale detected",
                "block_index": None,
            }
        )
        return Paragraph(
            text=text, role="docling_field_candidate", provenance=provenance
        )

    # --- Formula → Equation ---
    if label == DocItemLabel.FORMULA:
        text = getattr(item, "text", "") or ""
        return Equation(latex=text, provenance=provenance, bbox=provenance.bbox)

    # --- Table → Table with raw HTML ---
    if label == DocItemLabel.TABLE:
        assert isinstance(item, TableItem)
        try:
            html = item.export_to_html(doc=doc, add_caption=False)
        except Exception as exc:
            msg = f"Table HTML export failed on page {pno} (ref={item.self_ref}): {exc}"
            log.warning(msg)
            warnings.append(
                Stage3Warning(message=msg, page=pno, item_ref=item.self_ref)
            )
            audit_notes.append(
                {
                    "kind": "other",
                    "page": pno,
                    "hint": "table_export_failed",
                    "block_index": None,
                }
            )
            html = "<!-- table export failed -->"
        return Table(
            html=html,
            table_title="",
            caption="",
            continuation=False,
            multi_page=False,
            bbox=provenance.bbox,
            provenance=provenance,
        )

    # --- Picture / Chart → Figure with mechanical image_ref ---
    if label in _PICTURE_LABELS:
        assert isinstance(item, PictureItem)
        ref_id = item.self_ref.replace("/", "_").replace("#", "_").lstrip("_")
        page_for_ref = pno
        image_ref = f"p{page_for_ref:04d}_{ref_id}.png"
        if images_dir is not None and not (images_dir / image_ref).is_file():
            warnings.append(
                Stage3Warning(
                    page=pno,
                    item_ref=item.self_ref,
                    message=f"Figure crop not found: {image_ref}",
                )
            )
            image_ref = None
        return Figure(
            image_ref=image_ref, caption="", bbox=provenance.bbox, provenance=provenance
        )

    # --- Marker: evidence only unless non-empty text ---
    if label == DocItemLabel.MARKER:
        text = getattr(item, "text", "") or ""
        if text:
            return Paragraph(
                text=text, role="docling_unknown_candidate", provenance=provenance
            )
        return None

    # --- Evidence-only labels ---
    if label in _EVIDENCE_ONLY_LABELS:
        return None

    # --- Unknown label ---
    text = getattr(item, "text", "") or ""
    label_str = label.value if isinstance(label, DocItemLabel) else str(label)
    if text:
        msg = f"Unknown Docling label '{label_str}' with text on page {pno} (ref={item.self_ref})"
        log.warning(msg)
        warnings.append(Stage3Warning(message=msg, page=pno, item_ref=item.self_ref))
        return Paragraph(
            text=text, role="docling_unknown_candidate", provenance=provenance
        )
    else:
        log.debug(
            "Unknown label '%s' without text on page %d ref=%s — evidence only",
            label_str,
            pno,
            item.self_ref,
        )
        return None


# ---------------------------------------------------------------------------
# Unit writer
# ---------------------------------------------------------------------------


def _write_unit(
    *,
    out_path: Path,
    pno: int,
    page_kind: str,
    draft_blocks: list[dict[str, Any]],
    evidence_refs: list[str],
    candidate_edges: dict[str, Any],
    audit_notes: list[dict[str, Any]],
) -> None:
    data = {
        "unit": {
            "kind": "docling_page",
            "pages": [pno],
            "page_kinds": [page_kind],
            "extractor": "skip_vlm",
            "contract_version": 3,
        },
        "draft_blocks": draft_blocks,
        "evidence_refs": evidence_refs,
        "candidate_edges": candidate_edges,
        "audit_notes": audit_notes,
    }
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Evidence index builder
# ---------------------------------------------------------------------------


def _build_evidence_index(
    doc: DoclingDocument,
    pages_data: list[dict[str, Any]],
    artifact_id: str,
    images_dir: Path | None = None,
) -> EvidenceIndex:
    """Build evidence index for all selected non-TOC pages."""
    selected_page_nos = {p["page"] for p in pages_data}
    page_kind_map = {p["page"]: p["kind"] for p in pages_data}

    page_items: dict[int, list[dict[str, Any]]] = {pno: [] for pno in selected_page_nos}
    refs: dict[str, dict[str, Any]] = {}

    for item in itertools.chain(
        doc.texts, doc.tables, doc.pictures, doc.key_value_items, doc.form_items
    ):
        ref: str | None = getattr(item, "self_ref", None) or getattr(item, "_ref", None)
        text = getattr(item, "text", "")
        label = item.label
        label_str = label.value if isinstance(label, DocItemLabel) else str(label)

        # Gather caption_refs, footnote_refs, reference_refs for floating items
        caption_refs = [r.cref for r in getattr(item, "captions", [])]
        footnote_refs = [r.cref for r in getattr(item, "footnotes", [])]
        reference_refs = [r.cref for r in getattr(item, "references", [])]

        for prov in item.prov:
            pno = prov.page_no
            if pno not in selected_page_nos:
                continue

            bbox = prov.bbox
            bbox_list: list[float] | None = None
            if bbox is not None:
                bbox_list = [bbox.l, bbox.t, bbox.r, bbox.b]

            # marker for list items
            marker: str | None = getattr(item, "marker", None)

            # image_ref for pictures
            image_ref: str | None = None
            if label in _PICTURE_LABELS:
                ref_id = (ref or "").replace("/", "_").replace("#", "_").lstrip("_")
                image_ref = f"p{pno:04d}_{ref_id}.png"
                if images_dir is not None and not (images_dir / image_ref).is_file():
                    image_ref = None

            # table html
            html: str | None = None
            if label == DocItemLabel.TABLE:
                assert isinstance(item, TableItem)
                try:
                    html = item.export_to_html(doc=doc, add_caption=False)
                except Exception:
                    html = None

            item_index = len(page_items[pno])
            entry: dict[str, Any] = {
                "ref": ref,
                "page": pno,
                "source": "docling",
                "label": label_str,
                "text": text,
                "html": html,
                "bbox": bbox_list,
                "image_ref": image_ref,
                "marker": marker,
                "caption_refs": caption_refs,
                "footnote_refs": footnote_refs,
                "reference_refs": reference_refs,
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
        }

    return EvidenceIndex(
        schema_version=3,
        artifact_id=artifact_id,
        mode="skip_vlm",
        source_pdf="source/source.pdf",
        pages=pages_dict,
        refs=refs,
    )
