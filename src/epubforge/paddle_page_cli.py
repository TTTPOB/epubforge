"""Agent-facing Paddle page-image CLI.

The revision-stage book editor runs this CLI on clean page JPEGs that Stage 4
placed under ``pages/``.  It runs LayoutDetection and TextDetection, writes
evidence and candidate JSON plus evidence JPEGs, caches the raw model output
for cheap postprocessing reruns, and optionally merges per-page evidence into
one ``paddle-evidence.json`` that Python uses to validate ``bbox-repairs.json``.

The CLI never touches a PDF and accepts only JPEG page images.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from epubforge.paddle_common import (
    CANDIDATE_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    LAYOUT_MODEL,
    PaddleError,
    RAW_SCHEMA_VERSION,
    TEXT_MODEL,
    _atomic_write_bytes,
    _file_sha256,
    _inference_params,
    _postprocess_params,
    _read_raw_json,
    _validate_reuse_meta,
    _versions,
    _write_json,
    apply_page_patch,
    build_candidate_boxes,
    build_evidence,
    draw_annotated_jpeg,
    draw_raw_evidence_jpeg,
    extract_layout_boxes,
    extract_text_lines,
    json_ready,
    jpeg_dimensions_from_bytes,
    load_models,
    run_models,
)
from epubforge.paddle_contract import (
    PAGE_CANDIDATE_COORDINATE_SPACE,
    PAGE_EVIDENCE_COORDINATE_SPACE,
)

EVIDENCE_OUT_SCHEMA_VERSION = 1
PAGE_DIR_PREFIX = "page-"
_JPEG_MAGIC = b"\xff\xd8"
MAX_PAGE_IMAGE_BYTES = 64 * 1024 * 1024
MAX_PAGES_PER_RUN = 64
MAX_PAGE_PIXELS = 25_000_000
MAX_TOTAL_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_EVIDENCE_OUT_BYTES = 256 * 1024 * 1024


def _parse_page_pairs(
    images: Sequence[Path], page_indices: Sequence[int]
) -> list[tuple[Path, int, str]]:
    if not images or len(images) != len(page_indices):
        raise PaddleError("--image and --page-idx must repeat the same number of times")
    if len(images) > MAX_PAGES_PER_RUN:
        raise PaddleError(
            f"a single invocation accepts at most {MAX_PAGES_PER_RUN} pages"
        )
    pairs: list[tuple[Path, int, str]] = []
    seen_indices: set[int] = set()
    for image, page_idx in zip(images, page_indices, strict=True):
        if page_idx < 0:
            raise PaddleError("--page-idx values are 0-based and must be non-negative")
        if page_idx in seen_indices:
            raise PaddleError(f"duplicate --page-idx value: {page_idx}")
        seen_indices.add(page_idx)
        pairs.append((image, page_idx, str(image)))
    return sorted(pairs, key=lambda pair: pair[1])


def _validate_image_path(path: Path) -> None:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise PaddleError(f"page image does not exist: {resolved}")
    if resolved.suffix.lower() == ".pdf":
        raise PaddleError(f"page image must not be a PDF: {resolved}")
    if resolved.suffix.lower() not in {".jpg", ".jpeg"}:
        raise PaddleError(f"page image must be a JPEG file: {resolved}")


def _read_input_snapshot(path: Path) -> bytes:
    """Read one bounded bytes snapshot of the input JPEG.

    The same snapshot feeds the magic check, the header pixel check, the
    SHA-256, and the cached ``source.jpg`` copy, so the source path is read
    exactly once and later path changes cannot affect the run.
    """
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_PAGE_IMAGE_BYTES + 1)
    except OSError as exc:
        raise PaddleError(f"page image is unreadable: {path}: {exc}") from exc
    if len(data) > MAX_PAGE_IMAGE_BYTES:
        raise PaddleError(f"page image exceeds the size limit: {path}")
    if not data.startswith(_JPEG_MAGIC):
        raise PaddleError(f"page image is not a JPEG file: {path}")
    return data


_EVIDENCE_PAGE_KEYS = frozenset(
    {
        "candidate",
        "evidence",
        "image",
        "page_idx",
        "source_image",
        "source_image_sha256",
    }
)


def _validate_existing_pages(pages: Sequence[Any], evidence_out: Path) -> None:
    """Strictly validate every existing aggregated evidence page entry.

    All failures are PaddleError; malformed page indices can never reach the
    later sort.
    """
    seen_indices: set[int] = set()
    for page in pages:
        if not isinstance(page, dict):
            raise PaddleError(
                f"existing evidence file has an invalid pages list: {evidence_out}"
            )
        if set(page) != _EVIDENCE_PAGE_KEYS:
            raise PaddleError(
                f"existing evidence page has an invalid contract: {evidence_out}"
            )
        page_idx = page["page_idx"]
        if not isinstance(page_idx, int) or isinstance(page_idx, bool) or page_idx < 0:
            raise PaddleError(
                f"existing evidence page has an invalid page_idx: {evidence_out}"
            )
        if page_idx in seen_indices:
            raise PaddleError(
                f"existing evidence file repeats page_idx {page_idx}: {evidence_out}"
            )
        seen_indices.add(page_idx)
        source_image = page["source_image"]
        if not isinstance(source_image, str) or not source_image:
            raise PaddleError(
                f"existing evidence page has an invalid source_image: {evidence_out}"
            )
        source_sha = page["source_image_sha256"]
        if (
            not isinstance(source_sha, str)
            or len(source_sha) != 64
            or any(character not in "0123456789abcdef" for character in source_sha)
        ):
            raise PaddleError(
                f"existing evidence page has an invalid source image hash: "
                f"{evidence_out}"
            )
        image = page["image"]
        if not isinstance(image, dict):
            raise PaddleError(
                f"existing evidence page has an invalid image: {evidence_out}"
            )
        width, height = image.get("width"), image.get("height")
        if (
            not isinstance(width, int)
            or isinstance(width, bool)
            or width <= 0
            or not isinstance(height, int)
            or isinstance(height, bool)
            or height <= 0
        ):
            raise PaddleError(
                f"existing evidence page has invalid image dimensions: {evidence_out}"
            )
        if not isinstance(page["evidence"], dict) or not isinstance(
            page["candidate"], dict
        ):
            raise PaddleError(
                f"existing evidence page has invalid evidence or candidate: "
                f"{evidence_out}"
            )


def _page_evidence_merge(
    evidence_out: Path,
    page_idx: int,
    *,
    source_image: str,
    source_image_sha256: str,
    image: dict[str, int],
    evidence: dict[str, Any],
    candidate: dict[str, Any],
    budget: Any | None = None,
) -> None:
    if evidence_out.exists():
        try:
            existing_size = evidence_out.stat().st_size
        except OSError as exc:
            raise PaddleError(
                f"cannot inspect evidence file: {evidence_out}: {exc}"
            ) from exc
        if existing_size > MAX_EVIDENCE_OUT_BYTES:
            raise PaddleError("aggregated paddle evidence exceeds the size limit")
        try:
            data = evidence_out.read_bytes()
        except OSError as exc:
            raise PaddleError(
                f"cannot read existing evidence file: {evidence_out}: {exc}"
            ) from exc
        if len(data) > MAX_EVIDENCE_OUT_BYTES:
            raise PaddleError("aggregated paddle evidence exceeds the size limit")
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise PaddleError(
                f"cannot read existing evidence file: {evidence_out}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != (
            EVIDENCE_OUT_SCHEMA_VERSION
        ):
            raise PaddleError(
                f"existing evidence file has an invalid schema: {evidence_out}"
            )
        pages = payload.get("pages")
        if not isinstance(pages, list):
            raise PaddleError(
                f"existing evidence file has an invalid pages list: {evidence_out}"
            )
        _validate_existing_pages(pages, evidence_out)
    else:
        pages = []
    kept = [page for page in pages if page["page_idx"] != page_idx]
    kept.append(
        {
            "page_idx": page_idx,
            "source_image": source_image,
            "source_image_sha256": source_image_sha256,
            "image": {"width": int(image["width"]), "height": int(image["height"])},
            "evidence": evidence,
            "candidate": candidate,
        }
    )
    kept.sort(key=lambda page: page["page_idx"])
    payload = {
        "schema_version": EVIDENCE_OUT_SCHEMA_VERSION,
        "pages": kept,
    }
    serialized = json.dumps(json_ready(payload), indent=2, ensure_ascii=False) + "\n"
    if len(serialized.encode("utf-8")) > MAX_EVIDENCE_OUT_BYTES:
        raise PaddleError("aggregated paddle evidence exceeds the size limit")
    _atomic_write_bytes(evidence_out, serialized.encode("utf-8"), budget=budget)


def execute(args: argparse.Namespace) -> dict[str, Any]:
    pairs = _parse_page_pairs(args.image, args.page_idx)
    output_dir = args.output.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    versions = _versions()
    models: tuple[Any, Any] | None = None
    page_records: list[dict[str, Any]] = []
    total_written = 0

    def output_budget(size: int) -> None:
        nonlocal total_written
        total_written += size
        if total_written > MAX_TOTAL_OUTPUT_BYTES:
            raise PaddleError("paddle output exceeds the total byte limit")

    for image_path, page_idx, source_image in pairs:
        _validate_image_path(image_path)
        image_bytes = _read_input_snapshot(image_path)
        image_sha256 = hashlib.sha256(image_bytes).hexdigest()
        page_dir = output_dir / f"{PAGE_DIR_PREFIX}{page_idx:04d}"
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
        inference_params = _inference_params(args, image_sha256)
        if args.reuse_raw:
            meta = _validate_reuse_meta(
                meta_path,
                inference_params,
                page_idx,
                max_pixels=MAX_PAGE_PIXELS,
            )
            if meta.get("image_sha256") != image_sha256:
                raise PaddleError(
                    f"cached raw evidence belongs to a different image for page "
                    f"{page_idx}: cached={meta.get('image_sha256')!r} "
                    f"input={image_sha256!r}"
                )
            # Three-way consistency: input image, cached metadata, and the
            # cached source.jpg copy must all be the same snapshot.
            if _file_sha256(source_path) != meta.get("image_sha256"):
                raise PaddleError(
                    f"cached source.jpg does not match the input image for page "
                    f"{page_idx}"
                )
            layout_raw = _read_raw_json(layout_raw_path, page_idx)
            text_raw = _read_raw_json(text_raw_path, page_idx)
            width, height = (
                int(meta["image"]["width"]),
                int(meta["image"]["height"]),
            )
            _checked_page_dims(width, height, page_idx)
            page_versions = dict(meta.get("versions", versions))
        else:
            _header_dims(image_bytes, page_idx)
            _atomic_write_bytes(
                source_path,
                image_bytes,
                budget=output_budget,
            )
            width, height = _decode_dims(source_path, page_idx)
            _checked_page_dims(width, height, page_idx)
            if models is None:
                models = load_models(args, source_path, page_idx)
            layout_raw, text_raw = run_models(source_path, args, page_idx, models)
            page_versions = versions
            _write_json(layout_raw_path, layout_raw, budget=output_budget)
            _write_json(text_raw_path, text_raw, budget=output_budget)
            _write_json(
                meta_path,
                {
                    "schema_version": RAW_SCHEMA_VERSION,
                    "page": page_idx,
                    "image_sha256": image_sha256,
                    "image": {"width": width, "height": height},
                    "coordinate_space": "workspace page raster pixels, origin top-left",
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
                budget=output_budget,
            )

        layout_boxes = extract_layout_boxes(layout_raw)
        text_lines = extract_text_lines(text_raw)
        evidence, layout_boxes, text_lines = build_evidence(
            layout_boxes,
            text_lines,
            page_number=page_idx,
            width=width,
            height=height,
            figure_text_overlap=args.figure_text_overlap,
            coordinate_space=PAGE_EVIDENCE_COORDINATE_SPACE,
        )
        _write_json(evidence_path, evidence, budget=output_budget)
        draw_raw_evidence_jpeg(
            source_path,
            layout_raw_image_path,
            evidence["layout_boxes"],
            kind="layout",
            quality=args.jpeg_quality,
            budget=output_budget,
        )
        draw_raw_evidence_jpeg(
            source_path,
            text_raw_image_path,
            evidence["db_lines"],
            kind="db",
            quality=args.jpeg_quality,
            budget=output_budget,
        )
        boxes = build_candidate_boxes(
            layout_boxes,
            text_lines,
            page_number=page_idx,
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
        if args.page_patch is not None:
            boxes, page_patch_record = apply_page_patch(
                args.page_patch,
                page_number=page_idx,
                width=width,
                height=height,
                evidence=evidence,
                layout_boxes=layout_boxes,
                text_lines=text_lines,
                candidate_boxes=boxes,
                postprocess_params=_postprocess_params(args),
            )
        candidate = {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "page": page_idx,
            "image": {"width": width, "height": height},
            "coordinate_space": dict(PAGE_CANDIDATE_COORDINATE_SPACE),
            "models": {
                "layout": LAYOUT_MODEL,
                "text_detection": TEXT_MODEL,
                "versions": page_versions,
            },
            "inference_params": inference_params,
            "postprocess_params": _postprocess_params(args),
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
        _write_json(candidate_path, candidate, budget=output_budget)
        draw_annotated_jpeg(
            source_path,
            annotated_path,
            boxes,
            quality=args.jpeg_quality,
            budget=output_budget,
        )
        if args.evidence_out is not None:
            _page_evidence_merge(
                args.evidence_out,
                page_idx,
                source_image=source_image,
                source_image_sha256=image_sha256,
                image={"width": width, "height": height},
                evidence=evidence,
                candidate=candidate,
                budget=output_budget,
            )
        page_records.append(
            {
                "page_idx": page_idx,
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
    return {
        "schema_version": 1,
        "output": str(output_dir),
        "pages": page_records,
        "reuse_raw": bool(args.reuse_raw),
        "models": {
            "layout": LAYOUT_MODEL,
            "text_detection": TEXT_MODEL,
            "versions": versions,
        },
    }


def _checked_page_dims(width: int, height: int, page_idx: int) -> None:
    if width <= 0 or height <= 0:
        raise PaddleError(f"page image has invalid dimensions: {page_idx}")
    if width * height > MAX_PAGE_PIXELS:
        raise PaddleError(
            f"page image {page_idx} exceeds the pixel limit ({width}x{height})"
        )


def _header_dims(data: bytes, page_idx: int) -> tuple[int, int]:
    """Parse and check the JPEG header dims from the input snapshot.

    Runs before the snapshot file is written or any model is loaded, so an
    oversized input never reaches a pixel buffer.
    """
    try:
        header_width, header_height = jpeg_dimensions_from_bytes(data)
    except PaddleError as exc:
        raise PaddleError(
            f"page image {page_idx} is not a readable JPEG: {exc}"
        ) from exc
    _checked_page_dims(header_width, header_height, page_idx)
    return header_width, header_height


def _decode_dims(source_path: Path, page_idx: int) -> tuple[int, int]:
    """Decode the snapshot file and return its real dimensions."""
    try:
        import fitz

        pixmap = fitz.Pixmap(str(source_path))
        return pixmap.width, pixmap.height
    except Exception as exc:
        raise PaddleError(
            f"page image {page_idx} is unreadable: {source_path}: {exc}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run Paddle layout and text detection on clean workspace page JPEGs. "
            "Inputs must be workspace page images; PDFs are rejected."
        )
    )
    parser.add_argument(
        "--image",
        type=Path,
        action="append",
        required=True,
        metavar="PATH",
        help="Clean workspace page JPEG (repeat per page).",
    )
    parser.add_argument(
        "--page-idx",
        type=int,
        action="append",
        required=True,
        metavar="N",
        help="0-based page index matching content page_idx (repeat per page).",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--evidence-out",
        type=Path,
        help=(
            "Merge each page's evidence and candidate into this JSON file for "
            "Python-side repair validation."
        ),
    )
    parser.add_argument(
        "--page-patch",
        type=Path,
        metavar="PATH",
        help=(
            "Execute trusted local Python patch_page(context) code after "
            "candidate generation; applies to every processed page."
        ),
    )
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
    parser.add_argument(
        "--reuse-raw",
        action="store_true",
        help="Skip model inference and rerun postprocessing from the cached raw output.",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    try:
        _parse_page_pairs(args.image, args.page_idx)
    except PaddleError as exc:
        parser.error(str(exc))
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
    except (PaddleError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(json_ready(run), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
