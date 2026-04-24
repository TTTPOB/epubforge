"""Stage 1 — Docling PDF parser."""

from __future__ import annotations

import logging
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

log = logging.getLogger(__name__)


def parse_pdf(pdf_path: Path, out_path: Path, *, images_dir: Path, ocr_settings=None) -> None:
    """Parse *pdf_path* with Docling and write DoclingDocument JSON to *out_path*.

    Figure crops are saved under *images_dir* as p{page}_{ref_id}.png.
    Requires generate_picture_images=True so PictureItem.get_image() works.
    """
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
        pipeline_opts = PdfPipelineOptions(
            generate_picture_images=True,
            generate_page_images=False,
            do_table_structure=True,
            do_ocr=True,
            ocr_options=ocr_options,
        )
    else:
        pipeline_opts = PdfPipelineOptions(
            generate_picture_images=True,
            generate_page_images=False,
            do_table_structure=True,
            do_ocr=False,
        )

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_opts)}
    )

    result = converter.convert(str(pdf_path))
    doc = result.document

    doc.save_as_json(out_path)

    images_dir.mkdir(parents=True, exist_ok=True)
    _save_figure_crops(doc, images_dir)

    n_pages = len(doc.pages)
    n_pictures = sum(1 for _ in doc.pictures)
    log.info("parse: pages=%d pictures=%d → %s", n_pages, n_pictures, out_path.name)


def _save_figure_crops(doc, images_dir: Path) -> None:
    from docling_core.types.doc import PictureItem

    for element, _level in doc.iterate_items():
        if not isinstance(element, PictureItem):
            continue
        pil_img = element.get_image(doc)
        if pil_img is None:
            log.warning("get_image() returned None for %s, skipping crop", element.self_ref)
            continue
        ref_id = element.self_ref.replace("/", "_").replace("#", "_").lstrip("_")
        page = element.prov[0].page_no if element.prov else 0
        img_path = images_dir / f"p{page:04d}_{ref_id}.png"
        pil_img.save(img_path, format="PNG")
