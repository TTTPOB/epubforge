"""Stage 1 — Docling PDF parser (page-batched to bound peak memory).

The traditional Docling+OCR pipeline used to call ``DocumentConverter.convert``
on the whole PDF in one shot. With OCR enabled, that easily peaked above 5 GiB
on 50 pages on an 8 GiB WSL2 box and OOM'd on larger documents.

This module now converts the PDF in fixed-size page batches (default 30) and
merges the per-batch ``DoclingDocument`` outputs into a single document that is
byte-equivalent for downstream consumers. The ``DocumentConverter`` is built
once and reused across batches: docling's ``StandardPdfPipeline`` holds
OCR/layout/table model instances that should be shared (re-loading them each
batch causes onnxruntime mmap accumulation, ~200 MiB/batch). Source check:
``DocumentConverter`` does not retain ``ConversionResult``; ``BasePipeline._unload``
releases per-page backends each convert. References to the per-batch result and
document are dropped and ``gc.collect()`` is triggered between batches.

Page numbers are preserved as **absolute** 1-based indices because Docling's
``convert(..., page_range=(s,e))`` already returns ``pages`` keyed by the
absolute page number and ``prov[].page_no`` set to the absolute page number.
We therefore never offset page numbers when merging — only the per-batch
``self_ref`` indices (``#/texts/N``, ``#/groups/N``, etc.) are reindexed to
keep them globally unique.

PDF backend selection strategy
-------------------------------
The backend is chosen by the following priority:

1. **Env override** — if ``EPUBFORGE_EXTRACT_PDF_BACKEND`` is set, that value
   is used unconditionally (``"docling_parse"`` or ``"pypdfium2"``).
2. **OCR auto** — when OCR is enabled the ``pypdfium2`` backend is selected,
   saving ~500 MiB of baseline memory; native text-unit quality does not matter
   because all text comes from RapidOCR in this mode.
3. **No-OCR auto** — when OCR is disabled ``docling_parse`` (V1) is kept for
   its superior native text extraction quality.

See ``docs/explorations/stage1-pdf-parser-memory.md`` for measurements.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# self_ref / $ref reindex helpers
# ---------------------------------------------------------------------------

# Plural keys whose items are referenced as ``#/<plural>/<int>`` in
# DoclingDocument JSON. ``#/body`` and ``#/furniture`` are *not* in this list:
# they are singletons we merge by appending children, not by reindexing.
_INDEXED_ARRAYS: tuple[str, ...] = (
    "groups",
    "texts",
    "pictures",
    "tables",
    "key_value_items",
    "form_items",
    "field_regions",
    "field_items",
)


def _reindex_refs_in_text(json_text: str, offsets: dict[str, int]) -> str:
    """Rewrite every ``#/<plural>/<int>`` occurrence by adding ``offsets[plural]``.

    Operates on the JSON serialization of a DoclingDocument. We rewrite both
    ``"self_ref": "#/texts/N"`` style and ``"$ref": "#/texts/N"`` style
    references in one regex sweep — the path syntax is identical for both.

    Anchors (``#/body``, ``#/furniture``) are not matched because the regex
    requires a trailing ``/<digits>`` segment.
    """
    if not any(offsets.values()):
        return json_text

    pattern = re.compile(
        r'(#/(' + "|".join(_INDEXED_ARRAYS) + r')/)(\d+)'
    )

    def _sub(m: re.Match[str]) -> str:
        prefix = m.group(1)
        plural = m.group(2)
        idx = int(m.group(3))
        return f"{prefix}{idx + offsets.get(plural, 0)}"

    return pattern.sub(_sub, json_text)


def _merge_batch_into(
    target_data: dict[str, Any] | None,
    batch_data: dict[str, Any],
) -> dict[str, Any]:
    """Merge a per-batch DoclingDocument-as-dict into the cumulative target.

    The first batch is taken as the base verbatim. Subsequent batches are
    reindexed (self_ref + $ref) by the current target arrays' lengths, then
    appended.

    We work on dicts (not pydantic models) so the reindex is a single regex
    pass over a JSON string — robust against new ref-bearing fields landing
    in future docling versions, since the ref grammar is fixed.
    """
    if target_data is None:
        return batch_data

    offsets: dict[str, int] = {
        plural: len(target_data.get(plural, [])) for plural in _INDEXED_ARRAYS
    }

    # Reindex the batch: dump → regex rewrite → reload.
    batch_text = json.dumps(batch_data, ensure_ascii=False)
    rewritten_text = _reindex_refs_in_text(batch_text, offsets)
    rewritten = json.loads(rewritten_text)

    # Append plural arrays.
    for plural in _INDEXED_ARRAYS:
        items = rewritten.get(plural)
        if not items:
            continue
        target_data.setdefault(plural, []).extend(items)

    # Merge ``body.children`` and ``furniture.children`` (refs are already
    # rewritten by the regex pass above).
    for anchor in ("body", "furniture"):
        if anchor not in rewritten:
            continue
        anchor_block = rewritten[anchor]
        anchor_children = anchor_block.get("children") or []
        if not anchor_children:
            continue
        target_anchor = target_data.setdefault(
            anchor, {"self_ref": f"#/{anchor}", "name": "_root_", "children": []}
        )
        target_anchor.setdefault("children", []).extend(anchor_children)

    # Merge pages dict (keys are absolute page numbers — see module docstring).
    for page_key, page_val in (rewritten.get("pages") or {}).items():
        target_data.setdefault("pages", {})[page_key] = page_val

    return target_data


# ---------------------------------------------------------------------------
# Pipeline option construction
# ---------------------------------------------------------------------------


def _build_pipeline_options(ocr_settings: Any) -> PdfPipelineOptions:
    if ocr_settings is not None and ocr_settings.enabled:
        from docling.datamodel.pipeline_options import RapidOcrOptions
        from rapidocr import OCRVersion, ModelType

        rapidocr_params = {
            "Det.ocr_version": OCRVersion(ocr_settings.ocr_version),
            "Det.model_type": ModelType(ocr_settings.model_type),
            "Rec.ocr_version": OCRVersion(ocr_settings.ocr_version),
            "Rec.model_type": ModelType(ocr_settings.model_type),
        }
        ocr_options = RapidOcrOptions(
            force_full_page_ocr=ocr_settings.force_full_page_ocr,
            text_score=ocr_settings.text_score,
            bitmap_area_threshold=ocr_settings.bitmap_area_threshold,
            backend=ocr_settings.backend,
            rapidocr_params=rapidocr_params,
        )
        return PdfPipelineOptions(
            generate_picture_images=True,
            generate_page_images=False,
            do_table_structure=True,
            do_ocr=True,
            ocr_options=ocr_options,
        )
    return PdfPipelineOptions(
        generate_picture_images=True,
        generate_page_images=False,
        do_table_structure=True,
        do_ocr=False,
    )


def _build_converter(
    pipeline_opts: PdfPipelineOptions, *, ocr_enabled: bool
) -> DocumentConverter:
    backend_env = os.environ.get("EPUBFORGE_EXTRACT_PDF_BACKEND")

    if backend_env is not None:
        # Explicit env override — honour it regardless of OCR mode.
        backend_name = backend_env
        decision = "env override"
    else:
        # Auto-select based on OCR mode.
        backend_name = "pypdfium2" if ocr_enabled else "docling_parse"
        decision = f"auto: ocr_enabled={ocr_enabled}"

    log.info("parse: pdf_backend=%s (%s)", backend_name, decision)

    if backend_name == "docling_parse":
        fmt_option = PdfFormatOption(pipeline_options=pipeline_opts)
    elif backend_name == "pypdfium2":
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

        fmt_option = PdfFormatOption(
            pipeline_options=pipeline_opts,
            backend=PyPdfiumDocumentBackend,
        )
    else:
        raise ValueError(f"unknown EPUBFORGE_EXTRACT_PDF_BACKEND: {backend_name!r}")
    return DocumentConverter(format_options={InputFormat.PDF: fmt_option})


# ---------------------------------------------------------------------------
# Page-count probe
# ---------------------------------------------------------------------------


def _count_pdf_pages(pdf_path: Path) -> int:
    """Return total page count of *pdf_path* via pypdfium2."""
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return len(pdf)
    finally:
        pdf.close()


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def parse_pdf(
    pdf_path: Path,
    out_path: Path,
    *,
    images_dir: Path,
    ocr_settings: Any = None,
    page_batch_size: int = 20,
) -> None:
    """Parse *pdf_path* with Docling and write DoclingDocument JSON to *out_path*.

    The PDF is converted in batches of ``page_batch_size`` pages at a time and
    the resulting DoclingDocuments are merged into one. Figure crops are saved
    under *images_dir* as ``p{page:04d}_{ref_id}.png`` using the **absolute**
    page number, so file names stay identical to the pre-batched layout.

    Requires ``generate_picture_images=True`` so ``PictureItem.get_image()``
    works during ``_save_figure_crops``.
    """
    if page_batch_size <= 0:
        raise ValueError(f"page_batch_size must be > 0, got {page_batch_size}")

    inner_batch_env = os.environ.get("EPUBFORGE_EXTRACT_DOCLING_INNER_BATCH")
    if inner_batch_env is not None:
        from docling.datamodel.settings import settings as _docling_settings

        _docling_settings.perf.page_batch_size = int(inner_batch_env)
        log.info(
            "docling inner page_batch_size override: %d",
            _docling_settings.perf.page_batch_size,
        )

    ocr_enabled = bool(ocr_settings is not None and ocr_settings.enabled)
    pipeline_opts = _build_pipeline_options(ocr_settings)
    images_dir.mkdir(parents=True, exist_ok=True)

    total_pages = _count_pdf_pages(pdf_path)
    if total_pages <= 0:
        raise RuntimeError(f"Could not determine page count for {pdf_path}")

    log.info(
        "parse: pdf=%s total_pages=%d batch_size=%d",
        pdf_path.name,
        total_pages,
        page_batch_size,
    )

    merged_data: dict[str, Any] | None = None
    n_pictures_total = 0

    # Build the converter once outside the loop. StandardPdfPipeline holds
    # OCR/layout/table model objects that should be reused across batches —
    # rebuilding each batch causes onnxruntime mmap accumulation (~200 MiB/batch).
    # DocumentConverter does not retain ConversionResult; BasePipeline._unload
    # releases per-page backends after each convert call.
    converter = _build_converter(pipeline_opts, ocr_enabled=ocr_enabled)
    try:
        for batch_start in range(1, total_pages + 1, page_batch_size):
            batch_end = min(batch_start + page_batch_size - 1, total_pages)
            log.info(
                "parse: batch pages=[%d..%d] of %d",
                batch_start,
                batch_end,
                total_pages,
            )

            try:
                result = converter.convert(
                    str(pdf_path), page_range=(batch_start, batch_end)
                )
                doc = result.document

                # Save figure crops immediately while ``doc`` still owns the
                # in-memory PIL images, so we can free the doc before the next
                # batch. Page numbers in ``prov`` are already absolute.
                n_pictures_total += _save_figure_crops(doc, images_dir)

                batch_data = doc.export_to_dict()
                merged_data = _merge_batch_into(merged_data, batch_data)
            finally:
                # Drop heavy per-batch refs and force a collection cycle.
                # This is the crucial step for memory boundedness — without it,
                # OCR intermediates from prior batches stay pinned.
                try:
                    del result, doc, batch_data  # noqa: F821
                except NameError:
                    pass
                gc.collect()
    finally:
        del converter
        gc.collect()

    if merged_data is None:
        raise RuntimeError(
            f"Parse produced no batches for {pdf_path} (total_pages={total_pages})"
        )

    # Round-trip through DoclingDocument so the on-disk JSON uses the same
    # serializer as the single-shot path (custom serializer drops empty
    # field_regions/field_items, etc.).
    from docling_core.types.doc import DoclingDocument

    merged_doc = DoclingDocument.model_validate(merged_data)
    merged_doc.save_as_json(out_path)

    n_pages = len(merged_doc.pages)
    log.info(
        "parse: pages=%d pictures=%d → %s",
        n_pages,
        n_pictures_total,
        out_path.name,
    )


# ---------------------------------------------------------------------------
# Figure crop saving
# ---------------------------------------------------------------------------


def _save_figure_crops(doc: Any, images_dir: Path) -> int:
    """Save PNG crops for every PictureItem in *doc*. Returns crop count.

    File naming uses the **absolute** page number from ``element.prov[0].page_no``
    so the layout is identical to the pre-batched parser.
    """
    from docling_core.types.doc import PictureItem

    saved = 0
    for element, _level in doc.iterate_items():
        if not isinstance(element, PictureItem):
            continue
        pil_img = element.get_image(doc)
        if pil_img is None:
            log.warning(
                "get_image() returned None for %s, skipping crop", element.self_ref
            )
            continue
        ref_id = element.self_ref.replace("/", "_").replace("#", "_").lstrip("_")
        page = element.prov[0].page_no if element.prov else 0
        img_path = images_dir / f"p{page:04d}_{ref_id}.png"
        pil_img.save(img_path, format="PNG")
        saved += 1
    return saved
