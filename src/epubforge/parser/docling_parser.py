"""Stage 1 — Docling PDF parser."""

from __future__ import annotations

import json
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption


def parse_pdf(pdf_path: Path, out_path: Path, *, images_dir: Path) -> None:
    """Parse *pdf_path* with Docling and write DoclingDocument JSON to *out_path*.

    Figure crops are saved under *images_dir* as p{page}_{ref_id}.png.
    Requires generate_picture_images=True so PictureItem.get_image() works.
    """
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

    raw = doc.export_to_dict()
    out_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    images_dir.mkdir(parents=True, exist_ok=True)
    _save_figure_crops(doc, images_dir)


def _save_figure_crops(doc, images_dir: Path) -> None:
    from docling_core.types.doc import PictureItem

    for element, _level in doc.iterate_items():
        if not isinstance(element, PictureItem):
            continue
        pil_img = element.get_image(doc)
        if pil_img is None:
            continue
        ref_id = element.self_ref.replace("/", "_").lstrip("_")
        page = element.prov[0].page_no if element.prov else 0
        img_path = images_dir / f"p{page:04d}_{ref_id}.png"
        pil_img.save(img_path, format="PNG")
