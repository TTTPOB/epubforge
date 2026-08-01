#!/usr/bin/env python3
"""Tune PaddleOCR layout and text-detection parameters on selected PDF pages."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import fitz

LAYOUT_MODEL = "PP-DocLayout-S"
TEXT_MODEL = "PP-OCRv5_mobile_det"
RAW_SCHEMA_VERSION = 2


class TuneError(RuntimeError):
    """Describe an actionable tuning-run failure."""


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


_LABEL_TYPES = {
    "image": "FIGURE",
    "figure": "FIGURE",
    "chart": "FIGURE",
    "seal": "FIGURE",
    "figure_title": "CAPTION",
    "figure_caption": "CAPTION",
    "image_caption": "CAPTION",
    "caption": "CAPTION",
    "text": "BODY",
    "paragraph": "BODY",
    "reference": "BODY",
    "abstract": "BODY",
    "content": "BODY",
    "title": "TITLE",
    "document_title": "TITLE",
    "doc_title": "TITLE",
    "paragraph_title": "TITLE",
    "section_header": "TITLE",
    "header": "HEADER",
    "page_header": "HEADER",
    "footer": "FOOTNOTE",
    "page_footer": "FOOTNOTE",
    "footnote": "FOOTNOTE",
    "figure_footnote": "CAPTION",
    "table_footnote": "FOOTNOTE",
    "aside_text": "BODY",
    "algorithm": "BODY",
    "number": "BODY",
    "table": "TABLE",
    "table_title": "CAPTION",
    "table_caption": "CAPTION",
    "formula": "FORMULA",
    "equation": "FORMULA",
    "formula_number": "FORMULA",
    "list": "LIST",
    "list_item": "LIST",
}


def map_layout_label(label: str) -> str:
    normalized = label.strip().lower().replace("-", "_").replace(" ", "_")
    return _LABEL_TYPES.get(normalized, "OTHER")


def json_ready(value: Any) -> Any:
    """Convert Paddle result objects and numeric arrays to JSON-compatible data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_ready(item) for item in value]
    for method_name in ("tolist", "item"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                converted = method()
            except (TypeError, ValueError):
                continue
            if converted is not value:
                return json_ready(converted)
    for attr_name in ("json", "to_dict", "dict"):
        attr = getattr(value, attr_name, None)
        if attr is None:
            continue
        try:
            converted = attr() if callable(attr) else attr
        except (TypeError, ValueError):
            continue
        if isinstance(converted, str):
            try:
                converted = json.loads(converted)
            except json.JSONDecodeError:
                pass
        if converted is not value:
            return json_ready(converted)
    return str(value)


def normalize_results(results: Any) -> list[Any]:
    if isinstance(results, Iterable) and not isinstance(results, (str, bytes, Mapping)):
        return [json_ready(result) for result in results]
    return [json_ready(results)]


def _result_payload(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if not isinstance(raw, Mapping):
        return {}
    payload = raw.get("res", raw)
    return payload if isinstance(payload, Mapping) else raw


def _bbox(value: Any) -> list[float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    values = json_ready(value)
    if not isinstance(values, list):
        return None
    if len(values) == 4 and all(isinstance(item, (int, float)) for item in values):
        x0, y0, x1, y1 = (float(item) for item in values)
        return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
    points = [point for point in values if isinstance(point, list) and len(point) >= 2]
    if not points:
        return None
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def extract_layout_boxes(raw: Any) -> list[dict[str, Any]]:
    payload = _result_payload(raw)
    source = payload.get("boxes", payload.get("layout_det_res", []))
    if isinstance(source, Mapping):
        source = source.get("boxes", [])
    boxes: list[dict[str, Any]] = []
    if not isinstance(source, Sequence):
        return boxes
    for item in source:
        if not isinstance(item, Mapping):
            continue
        coords = _bbox(item.get("coordinate", item.get("bbox", item.get("box", []))))
        if coords is None:
            continue
        label = str(item.get("label", item.get("cls_name", "unknown")))
        boxes.append(
            {
                "bbox": coords,
                "label": label,
                "type": map_layout_label(label),
                "score": float(item.get("score", 0.0)),
            }
        )
    return boxes


def extract_text_lines(raw: Any) -> list[dict[str, Any]]:
    payload = _result_payload(raw)
    polys = payload.get("dt_polys", payload.get("polys", payload.get("boxes", [])))
    scores = payload.get("dt_scores", payload.get("scores", []))
    polys = json_ready(polys)
    scores = json_ready(scores)
    if not isinstance(polys, list):
        return []
    lines: list[dict[str, Any]] = []
    for index, poly in enumerate(polys):
        coords = _bbox(poly)
        if coords is None:
            continue
        score = 1.0
        if isinstance(scores, list) and index < len(scores):
            score = float(scores[index])
        lines.append({"bbox": coords, "score": score})
    return lines


def _area(box: Sequence[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def intersection_ratio(inner: Sequence[float], outer: Sequence[float]) -> float:
    intersection = [
        max(inner[0], outer[0]),
        max(inner[1], outer[1]),
        min(inner[2], outer[2]),
        min(inner[3], outer[3]),
    ]
    return _area(intersection) / max(_area(inner), 1e-9)


def horizontal_overlap(a: Sequence[float], b: Sequence[float]) -> float:
    overlap = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    return overlap / max(min(a[2] - a[0], b[2] - b[0]), 1e-9)


def suppress_figure_text(
    lines: Sequence[dict[str, Any]],
    figures: Sequence[dict[str, Any]],
    *,
    overlap_threshold: float,
) -> list[dict[str, Any]]:
    return [
        dict(line)
        for line in lines
        if not any(
            intersection_ratio(line["bbox"], figure["bbox"]) >= overlap_threshold
            for figure in figures
        )
    ]


def aggregate_text_lines(
    lines: Sequence[dict[str, Any]],
    *,
    page_height: int,
    vertical_gap: float,
    horizontal_overlap_threshold: float,
) -> list[dict[str, Any]]:
    """Merge vertically adjacent lines using page-normalized thresholds."""
    groups: list[dict[str, Any]] = []
    ordered = sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))
    for line in ordered:
        box = line["bbox"]
        line_type = str(line.get("type", "BODY"))
        best_index: int | None = None
        best_gap = float("inf")
        for index, group in enumerate(groups):
            if group["type"] != line_type:
                continue
            gap = box[1] - group["bbox"][3]
            if (
                -vertical_gap * page_height <= gap <= vertical_gap * page_height
                and horizontal_overlap(box, group["bbox"])
                >= horizontal_overlap_threshold
                and gap < best_gap
            ):
                best_index = index
                best_gap = gap
        if best_index is None:
            groups.append(
                {
                    "bbox": list(box),
                    "type": line_type,
                    "score": float(line.get("score", 1.0)),
                    "line_count": 1,
                }
            )
            continue
        group = groups[best_index]
        group["bbox"] = [
            min(group["bbox"][0], box[0]),
            min(group["bbox"][1], box[1]),
            max(group["bbox"][2], box[2]),
            max(group["bbox"][3], box[3]),
        ]
        count = int(group["line_count"])
        group["score"] = (group["score"] * count + float(line.get("score", 1.0))) / (
            count + 1
        )
        group["line_count"] = count + 1
    return groups


def _assign_line_types(
    lines: Sequence[dict[str, Any]], layout_boxes: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], set[int]]:
    text_types = {"BODY", "CAPTION", "FOOTNOTE", "TITLE", "HEADER", "LIST"}
    assigned_layout: set[int] = set()
    typed: list[dict[str, Any]] = []
    for line in lines:
        candidates = [
            (intersection_ratio(line["bbox"], box["bbox"]), index, box["type"])
            for index, box in enumerate(layout_boxes)
            if box["type"] in text_types
        ]
        ratio, index, line_type = max(candidates, default=(0.0, -1, "BODY"))
        if ratio >= 0.5:
            assigned_layout.add(index)
        else:
            line_type = "BODY"
        typed.append({**line, "type": line_type})
    return typed, assigned_layout


def _infer_captions(
    groups: list[dict[str, Any]],
    figures: Sequence[dict[str, Any]],
    *,
    page_height: int,
    caption_gap: float,
    overlap_threshold: float,
) -> None:
    for group in groups:
        if group["type"] != "BODY":
            continue
        for figure in figures:
            gap = group["bbox"][1] - figure["bbox"][3]
            if (
                0 <= gap <= caption_gap * page_height
                and horizontal_overlap(group["bbox"], figure["bbox"])
                >= overlap_threshold
            ):
                group["type"] = "CAPTION"
                break


def assign_reading_order(boxes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign a deterministic top-to-bottom, then left-to-right order."""
    ordered = sorted(
        (dict(box) for box in boxes),
        key=lambda box: (
            round(float(box["y0"]), 3),
            round(float(box["x0"]), 3),
            round(float(box["y1"]), 3),
        ),
    )
    for order, box in enumerate(ordered, start=1):
        box["reading_order"] = order
    return ordered


def apply_reading_order(
    boxes: Sequence[dict[str, Any]], ordered_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Apply a complete stable-ID order without changing candidate geometry."""
    by_id = {str(box["id"]): dict(box) for box in boxes}
    if len(by_id) != len(boxes):
        raise TuneError("candidate boxes contain duplicate stable IDs")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise TuneError("reading-order override contains duplicate stable IDs")
    expected, supplied = set(by_id), set(ordered_ids)
    if expected != supplied:
        missing = sorted(expected - supplied)
        unknown = sorted(supplied - expected)
        raise TuneError(
            f"reading-order IDs do not match candidates; missing={missing}, unknown={unknown}"
        )
    ordered = [by_id[box_id] for box_id in ordered_ids]
    for order, box in enumerate(ordered, start=1):
        box["reading_order"] = order
    return ordered


def _stable_id(page_number: int, box_type: str, bbox: Sequence[float]) -> str:
    identity = f"{page_number}:{box_type}:" + ":".join(f"{value:.2f}" for value in bbox)
    digest = hashlib.sha1(identity.encode("ascii")).hexdigest()[:10]
    return f"p{page_number:04d}-{box_type.lower()}-{digest}"


def build_candidate_boxes(
    layout_boxes: Sequence[dict[str, Any]],
    text_lines: Sequence[dict[str, Any]],
    *,
    page_number: int,
    page_height: int,
    vertical_gap: float,
    horizontal_overlap_threshold: float,
    caption_gap: float,
    figure_text_overlap: float,
) -> list[dict[str, Any]]:
    figures = [box for box in layout_boxes if box["type"] == "FIGURE"]
    visible_lines = suppress_figure_text(
        text_lines, figures, overlap_threshold=figure_text_overlap
    )
    typed_lines, assigned_layout = _assign_line_types(visible_lines, layout_boxes)
    groups = aggregate_text_lines(
        typed_lines,
        page_height=page_height,
        vertical_gap=vertical_gap,
        horizontal_overlap_threshold=horizontal_overlap_threshold,
    )
    _infer_captions(
        groups,
        figures,
        page_height=page_height,
        caption_gap=caption_gap,
        overlap_threshold=horizontal_overlap_threshold,
    )

    textual = {"BODY", "CAPTION", "FOOTNOTE", "TITLE", "HEADER", "LIST"}
    retained = [
        dict(box)
        for index, box in enumerate(layout_boxes)
        if box["type"] not in textual or index not in assigned_layout
    ]
    normalized: list[dict[str, Any]] = []
    for source in [*retained, *groups]:
        bbox = [float(value) for value in source["bbox"]]
        box_type = str(source["type"])
        normalized.append(
            {
                "id": _stable_id(page_number, box_type, bbox),
                "type": box_type,
                "x0": bbox[0],
                "y0": bbox[1],
                "x1": bbox[2],
                "y1": bbox[3],
                "score": round(float(source.get("score", 0.0)), 6),
                **(
                    {"source_label": source["label"]}
                    if "label" in source
                    else {"text_line_count": int(source.get("line_count", 1))}
                ),
            }
        )
    return assign_reading_order(normalized)


_COLORS = {
    "FIGURE": (220 / 255, 38 / 255, 38 / 255),
    "CAPTION": (234 / 255, 88 / 255, 12 / 255),
    "BODY": (22 / 255, 101 / 255, 52 / 255),
    "FOOTNOTE": (126 / 255, 34 / 255, 206 / 255),
    "TABLE": (2 / 255, 132 / 255, 199 / 255),
    "FORMULA": (190 / 255, 24 / 255, 93 / 255),
}


def draw_annotated_jpeg(
    source_path: Path,
    output_path: Path,
    boxes: Sequence[dict[str, Any]],
    *,
    quality: int,
) -> None:
    if not source_path.is_file():
        raise TuneError(f"source JPEG is missing: {source_path}")
    pixmap = fitz.Pixmap(str(source_path))
    doc = fitz.open()
    page = doc.new_page(width=pixmap.width, height=pixmap.height)
    page.insert_image(page.rect, filename=str(source_path))
    for box in boxes:
        color = _COLORS.get(str(box["type"]), (0.1, 0.1, 0.1))
        rect = fitz.Rect(box["x0"], box["y0"], box["x1"], box["y1"])
        page.draw_rect(rect, color=color, width=2)
        label = f"{box['reading_order']} {box['id']} {box['type']}"
        label_y = max(10.0, float(box["y0"]) - 3.0)
        page.insert_text((float(box["x0"]), label_y), label, fontsize=8, color=color)
    rendered = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
    _atomic_write_bytes(output_path, rendered.tobytes("jpeg", jpg_quality=quality))
    doc.close()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _write_json(path: Path, payload: Any) -> None:
    serialized = json.dumps(json_ready(payload), indent=2, ensure_ascii=False) + "\n"
    _atomic_write_bytes(
        path,
        serialized.encode("utf-8"),
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


def load_models(
    args: argparse.Namespace, source_path: Path, page_number: int
) -> tuple[Any, Any]:
    """Load Paddle models only on the real inference path."""
    os.environ.setdefault("OMP_NUM_THREADS", str(args.cpu_threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(args.cpu_threads))
    try:
        from paddleocr import LayoutDetection, TextDetection
    except ImportError as exc:
        raise TuneError(
            f"model import failed; stage=construct model={LAYOUT_MODEL}/{TEXT_MODEL} "
            f"page={page_number} image={source_path}: {exc}"
        ) from exc

    try:
        layout_model = LayoutDetection(
            model_name=LAYOUT_MODEL,
            device="cpu",
            cpu_threads=args.cpu_threads,
            threshold=args.layout_threshold,
            layout_nms=args.layout_nms,
        )
    except Exception as exc:
        raise TuneError(
            f"model construction failed; stage=construct model={LAYOUT_MODEL} "
            f"page={page_number} image={source_path}: {exc}"
        ) from exc
    try:
        text_model = TextDetection(
            model_name=TEXT_MODEL,
            device="cpu",
            cpu_threads=args.cpu_threads,
            thresh=args.db_thresh,
            box_thresh=args.db_box_thresh,
            unclip_ratio=args.db_unclip_ratio,
        )
    except Exception as exc:
        raise TuneError(
            f"model construction failed; stage=construct model={TEXT_MODEL} "
            f"page={page_number} image={source_path}: {exc}"
        ) from exc
    return layout_model, text_model


def run_models(
    source_path: Path,
    args: argparse.Namespace,
    page_number: int,
    models: tuple[Any, Any] | None = None,
) -> tuple[list[Any], list[Any]]:
    layout_model, text_model = (
        models if models is not None else load_models(args, source_path, page_number)
    )
    try:
        layout = layout_model.predict(str(source_path), batch_size=1)
    except Exception as exc:
        raise TuneError(
            f"model prediction failed; stage=predict model={LAYOUT_MODEL} "
            f"page={page_number} image={source_path}: {exc}"
        ) from exc
    try:
        text = text_model.predict(str(source_path), batch_size=1)
    except Exception as exc:
        raise TuneError(
            f"model prediction failed; stage=predict model={TEXT_MODEL} "
            f"page={page_number} image={source_path}: {exc}"
        ) from exc
    return normalize_results(layout), normalize_results(text)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution in ("paddleocr", "paddlepaddle"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "not-installed"
    return versions


def _inference_params(args: argparse.Namespace, pdf_sha256: str) -> dict[str, Any]:
    return {
        "pdf_sha256": pdf_sha256,
        "dpi": args.dpi,
        "jpeg_quality": args.jpeg_quality,
        "layout_model": LAYOUT_MODEL,
        "text_model": TEXT_MODEL,
        "layout_threshold": args.layout_threshold,
        "layout_nms": args.layout_nms,
        "db_thresh": args.db_thresh,
        "db_box_thresh": args.db_box_thresh,
        "db_unclip_ratio": args.db_unclip_ratio,
        "device": "cpu",
    }


def _postprocess_params(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "vertical_gap": args.vertical_gap,
        "horizontal_overlap": args.horizontal_overlap,
        "caption_gap": args.caption_gap,
        "figure_text_overlap": args.figure_text_overlap,
    }


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


def _validate_reuse_meta(
    meta_path: Path, expected: Mapping[str, Any], page_number: int
) -> dict[str, Any]:
    if not meta_path.is_file():
        raise TuneError(f"raw metadata is missing for page {page_number}: {meta_path}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TuneError(
            f"raw metadata is unreadable for page {page_number}: {exc}"
        ) from exc
    if meta.get("schema_version") != RAW_SCHEMA_VERSION:
        raise TuneError(
            f"raw schema mismatch for page {page_number}: "
            f"raw={meta.get('schema_version')!r} expected={RAW_SCHEMA_VERSION}"
        )
    if meta.get("page") != page_number:
        raise TuneError(
            f"raw page mismatch for requested page {page_number}: "
            f"metadata page={meta.get('page')!r}"
        )
    actual = meta.get("inference_params")
    mismatches = {
        key: {
            "raw": actual.get(key) if isinstance(actual, Mapping) else None,
            "requested": value,
        }
        for key, value in expected.items()
        if not isinstance(actual, Mapping) or actual.get(key) != value
    }
    if mismatches:
        details = ", ".join(
            f"{key} raw={values['raw']!r} requested={values['requested']!r}"
            for key, values in mismatches.items()
        )
        raise TuneError(
            f"raw parameters do not match for page {page_number}: {details}"
        )

    image = meta.get("image")
    if not isinstance(image, Mapping):
        raise TuneError(f"raw image metadata is missing for page {page_number}")
    width, height = image.get("width"), image.get("height")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or width <= 0
        or not isinstance(height, int)
        or isinstance(height, bool)
        or height <= 0
    ):
        raise TuneError(
            f"raw rendering dimensions are invalid for page {page_number}: "
            f"width={width!r} height={height!r}"
        )

    files = meta.get("files")
    if not isinstance(files, Mapping):
        raise TuneError(f"raw file hashes are missing for page {page_number}")
    required_names = ("source.jpg", "layout_raw.json", "text_raw.json")
    for name in required_names:
        path = meta_path.parent / name
        if not path.is_file():
            raise TuneError(f"raw reuse file is missing for page {page_number}: {path}")
        record = files.get(name)
        expected_sha = record.get("sha256") if isinstance(record, Mapping) else None
        actual_sha = _file_sha256(path)
        if expected_sha != actual_sha:
            raise TuneError(
                f"raw file hash mismatch for page {page_number}: {name} "
                f"metadata={expected_sha!r} actual={actual_sha}"
            )
    source_path = meta_path.parent / "source.jpg"
    try:
        source = fitz.Pixmap(str(source_path))
    except Exception as exc:
        raise TuneError(
            f"source JPEG is unreadable for page {page_number}: {source_path}: {exc}"
        ) from exc
    if (source.width, source.height) != (width, height):
        raise TuneError(
            f"raw rendering dimensions do not match source JPEG for page {page_number}: "
            f"metadata={width}x{height} actual={source.width}x{source.height}"
        )
    return meta


def _read_raw_json(path: Path, page_number: int) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TuneError(
            f"raw JSON is unreadable for page {page_number}: {path}: {exc}"
        ) from exc


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
            boxes = build_candidate_boxes(
                layout_boxes,
                text_lines,
                page_number=page_number,
                page_height=height,
                vertical_gap=args.vertical_gap,
                horizontal_overlap_threshold=args.horizontal_overlap,
                caption_gap=args.caption_gap,
                figure_text_overlap=args.figure_text_overlap,
            )
            if page_number in reading_orders:
                try:
                    boxes = apply_reading_order(boxes, reading_orders[page_number])
                except TuneError as exc:
                    raise TuneError(f"page {page_number}: {exc}") from exc
            candidate = {
                "schema_version": 1,
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
                "boxes": boxes,
            }
            candidate_path = page_dir / "candidate.json"
            annotated_path = page_dir / "annotated.jpg"
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
        help="Text-line area fraction that triggers figure suppression.",
    )
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--reuse-raw", action="store_true")
    parser.add_argument(
        "--reading-order-file",
        type=Path,
        help="JSON object mapping page numbers to complete stable-ID order lists.",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
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
    ):
        value = getattr(args, name)
        if name in {"vertical_gap", "caption_gap"}:
            valid = value >= 0
        else:
            valid = 0 <= value <= 1
        if not valid:
            parser.error(f"--{name.replace('_', '-')} has an invalid value: {value}")
    if args.db_unclip_ratio <= 0:
        parser.error("--db-unclip-ratio must be greater than zero")


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
