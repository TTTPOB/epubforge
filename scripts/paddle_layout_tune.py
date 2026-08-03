#!/usr/bin/env python3
"""Tune PaddleOCR layout and text-detection parameters on selected PDF pages.

The shared detection and postprocessing pipeline lives in
``epubforge.paddle_common``; this script adds the PDF-specific rendering,
page-list handling, and argparse interface for tuning runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import fitz

from epubforge.annotation import (
    ANNOTATED_BBOX_STROKE_WIDTH,
    ANNOTATED_LABEL_BLACK,
    ANNOTATED_LABEL_FILL_OPACITY,
    ANNOTATED_LABEL_FONT_NAME,
    ANNOTATED_LABEL_FONT_SIZE,
    ANNOTATED_LABEL_GAP,
    ANNOTATED_LABEL_PADDING,
    ANNOTATED_LABEL_WHITE,
    ANNOTATION_COLORS,
    draw_annotation,
    label_geometry,
    label_text_color,
)
from epubforge.paddle_common import (
    CANDIDATE_SCHEMA_VERSION,
    CANDIDATE_SOURCES,
    EVIDENCE_SCHEMA_VERSION,
    LAYOUT_MODEL,
    PaddleError,
    RAW_SCHEMA_VERSION,
    TEXT_MODEL,
    _atomic_write_bytes,
    _deduplicate_candidate_boxes,
    _file_sha256,
    _inference_params as common_inference_params,
    _postprocess_params,
    _read_raw_json,
    _stable_id,
    _validate_reuse_meta,
    _validated_patch_boxes,
    _versions,
    _write_json,
    aggregate_text_lines,
    apply_page_patch,
    apply_reading_order,
    assign_reading_order,
    build_candidate_boxes,
    build_evidence,
    draw_annotated_jpeg,
    draw_raw_evidence_jpeg,
    extract_layout_boxes,
    extract_text_lines,
    json_ready,
    load_models,
    map_layout_label,
    normalize_results,
    run_models,
)

# Backward-compatible public name for callers and tests of this script.
TuneError = PaddleError

_COLORS = ANNOTATION_COLORS
_label_text_color = label_text_color
_label_geometry = label_geometry


def _inference_params(args: argparse.Namespace, pdf_sha256: str) -> dict[str, Any]:
    """Bind the rendered source.jpg parameters to the raw cache.

    The raw detection depends on the CropBox JPEG the script renders, so a
    DPI or JPEG quality change must invalidate the cache.
    """
    params = common_inference_params(args, pdf_sha256)
    params["dpi"] = args.dpi
    params["jpeg_quality"] = args.jpeg_quality
    return params


def parse_pages(value: str) -> list[int]:
    """Parse comma-separated one-indexed pages and inclusive ranges."""
    pages: list[int] = []
    seen: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if not token:
            raise ValueError("page list contains an empty item")
        if "-" in token:
            parts = token.split("-", 1)
            try:
                start, end = (int(part) for part in parts)
            except ValueError as exc:
                raise ValueError(f"invalid page range: {token!r}") from exc
            if start < 1 or end < start:
                raise ValueError(f"invalid page range: {token!r}")
            values: Iterable[int] = range(start, end + 1)
        else:
            try:
                page = int(token)
            except ValueError as exc:
                raise ValueError(f"invalid page number: {token!r}") from exc
            if page < 1:
                raise ValueError(f"page numbers are 1-indexed: {page}")
            values = (page,)
        for page in values:
            if page not in seen:
                seen.add(page)
                pages.append(page)
    if not pages:
        raise ValueError("at least one page is required")
    return pages


def parse_page_patch(value: str) -> tuple[int, Path]:
    """Parse PAGE=PATH for a trusted page-specific Python hook."""
    page_value, separator, path_value = value.partition("=")
    if not separator or not page_value or not path_value:
        raise ValueError("page patch must use PAGE=PATH")
    try:
        page_number = int(page_value)
    except ValueError as exc:
        raise ValueError(f"invalid page patch page: {page_value!r}") from exc
    if page_number < 1:
        raise ValueError(f"page patch pages are 1-indexed: {page_number}")
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"page patch file does not exist: {path}")
    return page_number, path


def _draw_annotated_candidate(
    page: fitz.Page,
    box: dict[str, Any],
    *,
    page_width: float,
    page_height: float,
) -> None:
    draw_annotation(
        page,
        box,
        page_width=page_width,
        page_height=page_height,
    )


def render_cropbox_jpeg(
    pdf_page: fitz.Page, output_path: Path, *, dpi: int, quality: int
) -> tuple[int, int]:
    scale = dpi / 72.0
    pixmap = pdf_page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        colorspace=fitz.csRGB,
        alpha=False,
    )
    _atomic_write_bytes(output_path, pixmap.tobytes("jpeg", jpg_quality=quality))
    return pixmap.width, pixmap.height


def _resolve_page_patches(
    values: Sequence[tuple[int, Path]], requested_pages: Sequence[int]
) -> dict[int, Path]:
    patches: dict[int, Path] = {}
    requested = set(requested_pages)
    for page_number, path in values:
        if page_number in patches:
            raise TuneError(f"duplicate page patch for page {page_number}")
        if page_number not in requested:
            raise TuneError(
                f"page patch targets unrequested page {page_number}: {path}"
            )
        if not path.is_file():
            raise TuneError(
                f"page patch file does not exist for page {page_number}: {path}"
            )
        patches[page_number] = path.expanduser().resolve()
    return patches


def _load_reading_orders(path: Path | None) -> dict[int, list[str]]:
    if path is None:
        return {}
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise TuneError(f"reading-order file does not exist: {resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TuneError(f"reading-order file is unreadable: {resolved}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise TuneError("reading-order file must be an object keyed by page number")
    orders: dict[int, list[str]] = {}
    for page_key, ids in payload.items():
        try:
            page_number = int(page_key)
        except (TypeError, ValueError) as exc:
            raise TuneError(f"invalid reading-order page key: {page_key!r}") from exc
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise TuneError(
                f"reading order for page {page_number} must be a list of stable IDs"
            )
        orders[page_number] = ids
    return orders


def execute(args: argparse.Namespace) -> dict[str, Any]:
    pdf_path = args.pdf.expanduser().resolve()
    if not pdf_path.is_file():
        raise TuneError(f"PDF file does not exist: {pdf_path}")
    output_dir = args.output.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_sha256 = _file_sha256(pdf_path)
    inference_params = _inference_params(args, pdf_sha256)
    postprocess_params = _postprocess_params(args)
    reading_orders = _load_reading_orders(args.reading_order_file)
    page_patches = _resolve_page_patches(args.page_patch, args.pages)
    page_patch_records: dict[int, dict[str, str]] = {}
    unrequested_order_pages = sorted(set(reading_orders) - set(args.pages))
    if unrequested_order_pages:
        raise TuneError(
            f"reading-order file contains unrequested pages: {unrequested_order_pages}"
        )

    try:
        document = fitz.open(pdf_path)
    except Exception as exc:
        raise TuneError(f"cannot open PDF {pdf_path}: {exc}") from exc
    if not document.is_pdf:
        document.close()
        raise TuneError(f"input is not a PDF: {pdf_path}")
    out_of_range = [page for page in args.pages if page > document.page_count]
    if out_of_range:
        document.close()
        raise TuneError(
            f"page numbers exceed PDF page count {document.page_count}: {out_of_range}"
        )

    page_records: list[dict[str, Any]] = []
    versions = _versions()
    models: tuple[Any, Any] | None = None
    try:
        for page_number in args.pages:
            page_dir = output_dir / f"page-{page_number:04d}"
            page_dir.mkdir(parents=True, exist_ok=True)
            source_path = page_dir / "source.jpg"
            layout_raw_path = page_dir / "layout_raw.json"
            text_raw_path = page_dir / "text_raw.json"
            meta_path = page_dir / "raw_meta.json"
            evidence_path = page_dir / "evidence.json"
            layout_raw_image_path = page_dir / "layout_raw.jpg"
            text_raw_image_path = page_dir / "text_raw.jpg"
            candidate_path = page_dir / "candidate.json"
            annotated_path = page_dir / "annotated.jpg"
            if args.reuse_raw:
                meta = _validate_reuse_meta(meta_path, inference_params, page_number)
                layout_raw = _read_raw_json(layout_raw_path, page_number)
                text_raw = _read_raw_json(text_raw_path, page_number)
                width, height = (
                    int(meta["image"]["width"]),
                    int(meta["image"]["height"]),
                )
                page_versions = dict(meta.get("versions", versions))
            else:
                width, height = render_cropbox_jpeg(
                    document[page_number - 1],
                    source_path,
                    dpi=args.dpi,
                    quality=args.jpeg_quality,
                )
                if models is None:
                    models = load_models(args, source_path, page_number)
                layout_raw, text_raw = run_models(
                    source_path, args, page_number, models
                )
                page_versions = versions
                _write_json(layout_raw_path, layout_raw)
                _write_json(text_raw_path, text_raw)
                _write_json(
                    meta_path,
                    {
                        "schema_version": RAW_SCHEMA_VERSION,
                        "page": page_number,
                        "image": {"width": width, "height": height},
                        "coordinate_space": "CropBox raster pixels, origin top-left",
                        "inference_params": inference_params,
                        "versions": page_versions,
                        "files": {
                            name: {"sha256": _file_sha256(page_dir / name)}
                            for name in (
                                "source.jpg",
                                "layout_raw.json",
                                "text_raw.json",
                            )
                        },
                    },
                )

            layout_boxes = extract_layout_boxes(layout_raw)
            text_lines = extract_text_lines(text_raw)
            evidence, layout_boxes, text_lines = build_evidence(
                layout_boxes,
                text_lines,
                page_number=page_number,
                width=width,
                height=height,
                figure_text_overlap=args.figure_text_overlap,
            )
            _write_json(evidence_path, evidence)
            draw_raw_evidence_jpeg(
                source_path,
                layout_raw_image_path,
                evidence["layout_boxes"],
                kind="layout",
                quality=args.jpeg_quality,
            )
            draw_raw_evidence_jpeg(
                source_path,
                text_raw_image_path,
                evidence["db_lines"],
                kind="db",
                quality=args.jpeg_quality,
            )
            boxes = build_candidate_boxes(
                layout_boxes,
                text_lines,
                page_number=page_number,
                page_height=height,
                vertical_gap=args.vertical_gap,
                horizontal_overlap_threshold=args.horizontal_overlap,
                caption_gap=args.caption_gap,
                figure_text_overlap=args.figure_text_overlap,
                figure_text_policy=args.figure_text_policy,
                figure_obstacle_split=args.figure_obstacle_split,
                candidate_source=args.candidate_source,
            )
            page_patch_record: dict[str, str] | None = None
            if page_number in page_patches:
                boxes, page_patch_record = apply_page_patch(
                    page_patches[page_number],
                    page_number=page_number,
                    width=width,
                    height=height,
                    evidence=evidence,
                    layout_boxes=layout_boxes,
                    text_lines=text_lines,
                    candidate_boxes=boxes,
                    postprocess_params=postprocess_params,
                )
                page_patch_records[page_number] = page_patch_record
            if page_number in reading_orders:
                try:
                    boxes = apply_reading_order(boxes, reading_orders[page_number])
                except TuneError as exc:
                    raise TuneError(f"page {page_number}: {exc}") from exc
            candidate = {
                "schema_version": CANDIDATE_SCHEMA_VERSION,
                "page": page_number,
                "image": {"width": width, "height": height},
                "coordinate_space": {
                    "source": "PDF CropBox",
                    "units": "pixels",
                    "origin": "top-left",
                    "x_axis": "right",
                    "y_axis": "down",
                },
                "models": {
                    "layout": LAYOUT_MODEL,
                    "text_detection": TEXT_MODEL,
                    "versions": page_versions,
                },
                "inference_params": inference_params,
                "postprocess_params": postprocess_params,
                "page_patch": page_patch_record,
                "artifacts": {
                    "source": "source.jpg",
                    "layout_raw": "layout_raw.json",
                    "text_raw": "text_raw.json",
                    "raw_meta": "raw_meta.json",
                    "evidence": "evidence.json",
                    "layout_evidence_image": "layout_raw.jpg",
                    "text_evidence_image": "text_raw.jpg",
                    "annotated": "annotated.jpg",
                    "candidate": "candidate.json",
                },
                "boxes": boxes,
            }
            _write_json(candidate_path, candidate)
            draw_annotated_jpeg(
                source_path, annotated_path, boxes, quality=args.jpeg_quality
            )
            page_records.append(
                {
                    "page": page_number,
                    "directory": page_dir.name,
                    "box_count": len(boxes),
                    "image": {"width": width, "height": height},
                    "reused_raw": bool(args.reuse_raw),
                    "page_patch": page_patch_record,
                    "artifacts": {
                        name: str(path.relative_to(output_dir))
                        for name, path in {
                            "source": source_path,
                            "layout_raw": layout_raw_path,
                            "text_raw": text_raw_path,
                            "raw_meta": meta_path,
                            "evidence": evidence_path,
                            "layout_evidence_image": layout_raw_image_path,
                            "text_evidence_image": text_raw_image_path,
                            "candidate": candidate_path,
                            "annotated": annotated_path,
                        }.items()
                    },
                }
            )
    finally:
        document.close()

    run = {
        "schema_version": 1,
        "source_pdf": str(pdf_path),
        "output": str(output_dir),
        "pages": page_records,
        "inference_params": inference_params,
        "postprocess_params": postprocess_params,
        "models": {
            "layout": LAYOUT_MODEL,
            "text_detection": TEXT_MODEL,
            "versions": versions,
        },
        "reuse_raw": bool(args.reuse_raw),
        "reading_order_file": (
            str(args.reading_order_file.expanduser().resolve())
            if args.reading_order_file is not None
            else None
        ),
        "page_patches": [
            {"page": page_number, **page_patch_records[page_number]}
            for page_number in sorted(page_patch_records)
        ],
    }
    _write_json(output_dir / "run.json", run)
    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Paddle layout tuning on selected one-indexed PDF pages."
    )
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--pages", type=parse_pages, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--jpeg-quality", type=int, default=82)
    parser.add_argument("--layout-threshold", type=float, default=0.35)
    parser.add_argument(
        "--layout-nms", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument("--db-thresh", type=float, default=0.25)
    parser.add_argument("--db-box-thresh", type=float, default=0.45)
    parser.add_argument("--db-unclip-ratio", type=float, default=1.5)
    parser.add_argument(
        "--vertical-gap",
        type=float,
        default=0.012,
        help="Maximum text-line gap as a fraction of page height.",
    )
    parser.add_argument(
        "--horizontal-overlap",
        type=float,
        default=0.5,
        help="Minimum overlap divided by the narrower box width.",
    )
    parser.add_argument(
        "--caption-gap",
        type=float,
        default=0.025,
        help="Maximum figure-to-caption gap as a fraction of page height.",
    )
    parser.add_argument(
        "--figure-text-overlap",
        type=float,
        default=0.5,
        help="Text area fraction used for figure relationships and optional exclusion.",
    )
    parser.add_argument(
        "--figure-text-policy",
        choices=("keep", "exclude"),
        default="keep",
        help="Keep figure-related text evidence or exclude it from candidates.",
    )
    parser.add_argument(
        "--figure-obstacle-split",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Prevent DB groups from merging across layout FIGURE boxes.",
    )
    parser.add_argument(
        "--candidate-source",
        choices=("combined", "layout-only", "db-only"),
        default="combined",
        help="Select which normalized detections produce candidate boxes.",
    )
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--reuse-raw", action="store_true")
    parser.add_argument(
        "--reading-order-file",
        type=Path,
        help="JSON object mapping page numbers to complete stable-ID order lists.",
    )
    parser.add_argument(
        "--page-patch",
        type=parse_page_patch,
        action="append",
        default=[],
        metavar="PAGE=PATH",
        help=(
            "Execute trusted local Python patch_page(context) code for one page; "
            "no sandbox is provided. Repeat for distinct requested pages."
        ),
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    try:
        _resolve_page_patches(args.page_patch, args.pages)
    except TuneError as exc:
        parser.error(str(exc))
    if args.dpi < 36:
        parser.error("--dpi must be at least 36")
    if not 1 <= args.jpeg_quality <= 100:
        parser.error("--jpeg-quality must be between 1 and 100")
    if not 1 <= args.cpu_threads <= 4:
        parser.error("--cpu-threads must be between 1 and 4")
    for name in (
        "layout_threshold",
        "db_thresh",
        "db_box_thresh",
        "vertical_gap",
        "horizontal_overlap",
        "caption_gap",
        "figure_text_overlap",
        "db_unclip_ratio",
    ):
        value = getattr(args, name)
        if not math.isfinite(value):
            parser.error(f"--{name.replace('_', '-')} must be finite")
        if name in {"vertical_gap", "caption_gap"}:
            valid = value >= 0
        elif name == "db_unclip_ratio":
            valid = value > 0
        else:
            valid = 0 <= value <= 1
        if not valid:
            parser.error(f"--{name.replace('_', '-')} has an invalid value: {value}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    try:
        run = execute(args)
    except (TuneError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(run, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
