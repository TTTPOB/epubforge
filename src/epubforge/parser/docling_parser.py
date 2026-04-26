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
    segment_size: int | None = None,
) -> None:
    """Parse *pdf_path* with Docling and write DoclingDocument JSON to *out_path*.

    The PDF is converted in batches of ``page_batch_size`` pages at a time and
    the resulting DoclingDocuments are merged into one. Figure crops are saved
    under *images_dir* as ``p{page:04d}_{ref_id}.png`` using the **absolute**
    page number, so file names stay identical to the pre-batched layout.

    Requires ``generate_picture_images=True`` so ``PictureItem.get_image()``
    works during ``_save_figure_crops``.

    Memory-boundedness:
        Even with the per-batch ``gc.collect()`` cycle below, onnxruntime
        InferenceSession objects retain shape-cache mmap regions across
        ``convert()`` calls (no public release API exists). On long PDFs
        (300+ pages, OCR enabled) this accumulates past 5 GiB and OOMs an
        8 GiB WSL2 box. Setting ``segment_size`` reroutes through
        ``_parse_pdf_segmented`` which restarts the worker process every
        ``segment_size`` pages — process exit is the only reliable way to
        force the OS to reclaim those mmaps.

    Args:
        segment_size: when set and ``segment_size < total_pages``, run the
            parse in subprocess-isolated segments of N pages each. Each
            segment writes a partial DoclingDocument JSON, then the parent
            merges them into ``out_path``. None (default) preserves the
            single-process path for backward compatibility.
    """
    if page_batch_size <= 0:
        raise ValueError(f"page_batch_size must be > 0, got {page_batch_size}")
    if segment_size is not None and segment_size <= 0:
        raise ValueError(
            f"segment_size must be > 0 when set, got {segment_size}"
        )

    images_dir.mkdir(parents=True, exist_ok=True)

    total_pages = _count_pdf_pages(pdf_path)
    if total_pages <= 0:
        raise RuntimeError(f"Could not determine page count for {pdf_path}")

    if segment_size is not None and segment_size < total_pages:
        _parse_pdf_segmented(
            pdf_path,
            out_path,
            images_dir=images_dir,
            ocr_settings=ocr_settings,
            page_batch_size=page_batch_size,
            segment_size=segment_size,
            total_pages=total_pages,
        )
        return

    _apply_inner_batch_env_override()

    log.info(
        "parse: pdf=%s total_pages=%d batch_size=%d (single-process)",
        pdf_path.name,
        total_pages,
        page_batch_size,
    )

    merged_data, n_pictures_total = _parse_pdf_range(
        pdf_path,
        page_range=(1, total_pages),
        images_dir=images_dir,
        ocr_settings=ocr_settings,
        page_batch_size=page_batch_size,
    )

    if merged_data is None:
        raise RuntimeError(
            f"Parse produced no batches for {pdf_path} (total_pages={total_pages})"
        )

    _save_merged_doc(merged_data, out_path, n_pictures_total=n_pictures_total)


def _apply_inner_batch_env_override() -> None:
    inner_batch_env = os.environ.get("EPUBFORGE_EXTRACT_DOCLING_INNER_BATCH")
    if inner_batch_env is not None:
        from docling.datamodel.settings import settings as _docling_settings

        _docling_settings.perf.page_batch_size = int(inner_batch_env)
        log.info(
            "docling inner page_batch_size override: %d",
            _docling_settings.perf.page_batch_size,
        )


def _save_merged_doc(
    merged_data: dict[str, Any],
    out_path: Path,
    *,
    n_pictures_total: int,
) -> None:
    """Round-trip through DoclingDocument and save to *out_path*.

    The round-trip is required so the on-disk JSON uses the same serializer
    as the single-shot path (custom serializer drops empty
    field_regions/field_items, etc.).
    """
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


def _parse_pdf_range(
    pdf_path: Path,
    *,
    page_range: tuple[int, int],
    images_dir: Path,
    ocr_settings: Any,
    page_batch_size: int,
) -> tuple[dict[str, Any] | None, int]:
    """Run the page-batched docling loop over ``[start, end]``.

    Returns ``(merged_data | None, n_pictures_saved)``. Used by both the
    single-process ``parse_pdf`` path and by the subprocess worker behind
    ``_parse_pdf_segmented``. The converter is rebuilt fresh on every
    call so this function is safe to invoke as the entire body of a
    short-lived worker process.
    """
    start, end = page_range
    if start < 1 or end < start:
        raise ValueError(f"Invalid page_range: {page_range!r}")

    ocr_enabled = bool(ocr_settings is not None and ocr_settings.enabled)
    pipeline_opts = _build_pipeline_options(ocr_settings)

    merged_data: dict[str, Any] | None = None
    n_pictures_total = 0

    converter = _build_converter(pipeline_opts, ocr_enabled=ocr_enabled)
    try:
        for batch_start in range(start, end + 1, page_batch_size):
            batch_end = min(batch_start + page_batch_size - 1, end)
            log.info(
                "parse: batch pages=[%d..%d] of [%d..%d]",
                batch_start,
                batch_end,
                start,
                end,
            )

            try:
                result = converter.convert(
                    str(pdf_path), page_range=(batch_start, batch_end)
                )
                doc = result.document

                # Save figure crops immediately while ``doc`` still owns the
                # in-memory PIL images, so we can free the doc before the
                # next batch. Page numbers in ``prov`` are already absolute.
                n_pictures_total += _save_figure_crops(doc, images_dir)

                batch_data = doc.export_to_dict()
                merged_data = _merge_batch_into(merged_data, batch_data)
            finally:
                # Drop heavy per-batch refs and force a collection cycle.
                try:
                    del result, doc, batch_data  # noqa: F821
                except NameError:
                    pass
                gc.collect()
    finally:
        del converter
        gc.collect()

    return merged_data, n_pictures_total


def _segment_ranges(total_pages: int, segment_size: int) -> list[tuple[int, int]]:
    """Compute 1-based inclusive ``(start, end)`` pairs for segmented parse.

    >>> _segment_ranges(95, 40)
    [(1, 40), (41, 80), (81, 95)]
    >>> _segment_ranges(40, 40)
    [(1, 40)]
    >>> _segment_ranges(50, 20)
    [(1, 20), (21, 40), (41, 50)]
    """
    if total_pages <= 0:
        raise ValueError(f"total_pages must be > 0, got {total_pages}")
    if segment_size <= 0:
        raise ValueError(f"segment_size must be > 0, got {segment_size}")
    ranges: list[tuple[int, int]] = []
    for start in range(1, total_pages + 1, segment_size):
        end = min(start + segment_size - 1, total_pages)
        ranges.append((start, end))
    return ranges


def _parse_pdf_segmented(
    pdf_path: Path,
    out_path: Path,
    *,
    images_dir: Path,
    ocr_settings: Any,
    page_batch_size: int,
    segment_size: int,
    total_pages: int,
) -> None:
    """Run the page-batched parse in subprocess-isolated segments.

    Each segment is processed by ``epubforge.parser._segment_worker`` which
    invokes ``_parse_pdf_range`` on ``[seg_start, seg_end]`` and writes a
    partial DoclingDocument JSON. After every segment subprocess exits the
    OS reclaims that worker's mmap pages (onnxruntime / torch shape cache
    accumulation), bounding peak RSS at one segment's worth.

    We **do not** clean up segment files on failure so a hung run can be
    resumed by inspecting them.
    """
    import subprocess
    import sys

    segments = _segment_ranges(total_pages, segment_size)
    segments_dir = out_path.with_suffix(out_path.suffix + ".segments")
    segments_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "parse: pdf=%s total_pages=%d segment_size=%d page_batch_size=%d "
        "segments=%d (subprocess-isolated)",
        pdf_path.name,
        total_pages,
        segment_size,
        page_batch_size,
        len(segments),
    )

    # Serialise OCR settings for the worker. The segment worker reconstructs
    # these via pydantic; if ocr_settings is None we send an empty payload.
    ocr_json: str
    if ocr_settings is None:
        ocr_json = ""
    else:
        ocr_json = ocr_settings.model_dump_json()

    segment_outs: list[Path] = []
    for idx, (seg_start, seg_end) in enumerate(segments):
        seg_out = segments_dir / f"segment_{idx:03d}.json"
        segment_outs.append(seg_out)
        log.info(
            "parse: segment %d/%d pages=[%d..%d] → subprocess",
            idx + 1,
            len(segments),
            seg_start,
            seg_end,
        )

        cmd: list[str] = [
            sys.executable,
            "-m",
            "epubforge.parser._segment_worker",
            "docling",
            "--pdf",
            str(pdf_path),
            "--out",
            str(seg_out),
            "--images-dir",
            str(images_dir),
            "--start",
            str(seg_start),
            "--end",
            str(seg_end),
            "--page-batch-size",
            str(page_batch_size),
        ]
        if ocr_json:
            cmd.extend(["--ocr-json", ocr_json])

        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"parse: segment {idx + 1}/{len(segments)} pages=[{seg_start}..{seg_end}] "
                f"failed with exit code {exc.returncode}; intermediate segment "
                f"artefacts preserved under {segments_dir}"
            ) from exc

    # Merge all segment JSONs in order.
    merged_data: dict[str, Any] | None = None
    for seg_out in segment_outs:
        seg_data = json.loads(seg_out.read_text(encoding="utf-8"))
        merged_data = _merge_batch_into(merged_data, seg_data)
        # Free the per-segment dict before reading the next one.
        del seg_data
        gc.collect()

    if merged_data is None:
        raise RuntimeError(
            f"parse: segmented merge produced no data for {pdf_path}"
        )

    # Picture counter — we do not persist a per-segment count, so derive
    # from the merged doc for the log line.
    n_pictures_total = len(merged_data.get("pictures") or [])
    _save_merged_doc(merged_data, out_path, n_pictures_total=n_pictures_total)

    # Best-effort cleanup of segment files. We keep them on failure (handled
    # via the early-raise above) but remove on success to avoid clutter.
    for seg_out in segment_outs:
        try:
            seg_out.unlink()
        except OSError as exc:
            log.warning("parse: failed to remove segment file %s: %s", seg_out, exc)
    try:
        segments_dir.rmdir()
    except OSError:
        # Directory may be non-empty if cleanup failed above; non-fatal.
        pass


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
