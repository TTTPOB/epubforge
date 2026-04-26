"""Subprocess worker for ``parse_pdf`` segmented mode.

Invoked by ``epubforge.parser.docling_parser._parse_pdf_segmented`` (and
the granite segmented path) as one of two mutually-exclusive modes:

Docling mode:
    python -m epubforge.parser._segment_worker docling \\
        --pdf ... --out ... --images-dir ... \\
        --start N --end M --page-batch-size B \\
        [--ocr-json '<serialized OcrSettings>']

    Writes a partial DoclingDocument JSON of pages [start, end] to ``--out``.

Granite mode:
    python -m epubforge.parser._segment_worker granite \\
        --pdf ... --out ... --start N --end M \\
        --granite-json '<serialized GraniteSettings>'

    Writes a JSON envelope with per-page doctags + tallies to ``--out``:

        {
          "per_page_doctags": [str],
          "successful_pages": [int],
          "failed_pages": [int],
          "repeated_line_warnings": [int],
          "elapsed_seconds": float,
          "page_range": [start, end]
        }

Why a subprocess: onnxruntime/torch InferenceSession objects retain
shape-cache mmap regions across ``convert()`` calls (no public release
API). On long PDFs this accumulates past 5 GiB and OOMs an 8 GiB WSL2
box. Process exit is the only reliable way to force the OS to reclaim
those pages, so each segment runs in a short-lived worker.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="epubforge.parser._segment_worker",
        description="Subprocess worker for segmented PDF parsing.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    sub = p.add_subparsers(dest="mode", required=True)

    docling = sub.add_parser("docling", help="Run the docling page-batch loop")
    docling.add_argument("--pdf", required=True, type=Path)
    docling.add_argument("--out", required=True, type=Path)
    docling.add_argument("--images-dir", required=True, type=Path)
    docling.add_argument("--start", required=True, type=int)
    docling.add_argument("--end", required=True, type=int)
    docling.add_argument("--page-batch-size", required=True, type=int)
    docling.add_argument(
        "--ocr-json",
        default=None,
        help="Serialized OcrSettings (JSON). Empty / unset = OCR disabled.",
    )

    granite = sub.add_parser("granite", help="Run the granite per-page loop")
    granite.add_argument("--pdf", required=True, type=Path)
    granite.add_argument("--out", required=True, type=Path)
    granite.add_argument("--start", required=True, type=int)
    granite.add_argument("--end", required=True, type=int)
    granite.add_argument(
        "--granite-json",
        required=True,
        help="Serialized GraniteSettings (JSON).",
    )

    return p


def _deserialize_ocr_settings(payload: str | None) -> object | None:
    """Reconstruct an ``OcrSettings`` instance from a JSON payload."""
    if not payload:
        return None
    # Local import keeps top-level worker import cheap.
    from epubforge.config import OcrSettings

    return OcrSettings.model_validate_json(payload)


def _deserialize_granite_settings(payload: str) -> object:
    from epubforge.config import GraniteSettings

    return GraniteSettings.model_validate_json(payload)


def _run_docling(args: argparse.Namespace) -> None:
    from epubforge.parser.docling_parser import (
        _apply_inner_batch_env_override,
        _parse_pdf_range,
        _save_merged_doc,
    )

    _apply_inner_batch_env_override()

    ocr_settings = _deserialize_ocr_settings(args.ocr_json)
    args.images_dir.mkdir(parents=True, exist_ok=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    log.info(
        "segment-worker[docling]: pdf=%s pages=[%d..%d] page_batch_size=%d ocr=%s",
        args.pdf.name,
        args.start,
        args.end,
        args.page_batch_size,
        bool(ocr_settings is not None and getattr(ocr_settings, "enabled", False)),
    )

    merged_data, n_pictures = _parse_pdf_range(
        args.pdf,
        page_range=(args.start, args.end),
        images_dir=args.images_dir,
        ocr_settings=ocr_settings,
        page_batch_size=args.page_batch_size,
    )
    if merged_data is None:
        raise RuntimeError(
            f"segment-worker[docling]: produced no data for pages "
            f"[{args.start}..{args.end}] of {args.pdf}"
        )

    _save_merged_doc(merged_data, args.out, n_pictures_total=n_pictures)
    log.info(
        "segment-worker[docling]: segment written → %s (pictures=%d)",
        args.out,
        n_pictures,
    )


def _run_granite(args: argparse.Namespace) -> None:
    from epubforge.parser.granite_parser import _granite_pages_to_doctags

    granite_settings = _deserialize_granite_settings(args.granite_json)
    log.info(
        "segment-worker[granite]: pages=[%d..%d]", args.start, args.end
    )

    pages_result = _granite_pages_to_doctags(
        args.pdf,
        settings=granite_settings,
        page_range=(args.start, args.end),
    )

    payload = {
        "per_page_doctags": pages_result.per_page_doctags,
        "successful_pages": pages_result.successful_pages,
        "failed_pages": pages_result.failed_pages,
        "repeated_line_warnings": pages_result.repeated_line_warnings,
        "elapsed_seconds": pages_result.elapsed_seconds,
        "page_range": [args.start, args.end],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    log.info(
        "segment-worker[granite]: segment written → %s (succ=%d fail=%d)",
        args.out,
        len(pages_result.successful_pages),
        len(pages_result.failed_pages),
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.mode == "docling":
            _run_docling(args)
        elif args.mode == "granite":
            _run_granite(args)
        else:  # pragma: no cover — argparse guards this
            raise SystemExit(f"unknown mode: {args.mode!r}")
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — clean non-zero exit, full traceback in stderr
        log.exception("segment-worker: unhandled error")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
