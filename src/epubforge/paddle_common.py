"""Shared PaddleOCR detection and postprocessing logic.

The PDF-based tuning CLI (``scripts/paddle_layout_tune.py``) and the
agent-facing page-image CLI (``epubforge.paddle_page_cli``) both run the same
layout and text detection models and postprocess their results into stable
candidates.  This module keeps that shared pipeline in one place so neither
CLI copies the other's logic.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import fitz

from epubforge.annotation import (
    ANNOTATION_COLORS,
    draw_annotation,
    label_geometry,
    label_text_color,
)
from epubforge.paddle_contract import (
    CANDIDATE_SCHEMA_VERSION,
    CANDIDATE_TYPES,
    EVIDENCE_SCHEMA_VERSION,
    LAYOUT_MODEL,
    RAW_SCHEMA_VERSION,
    TEXT_MODEL,
    stable_candidate_id,
)

# Backward-compatible private alias used by this module and the tune script.
_stable_id = stable_candidate_id


class PaddleError(RuntimeError):
    """Describe an actionable Paddle detection failure."""


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

CANDIDATE_SOURCES = frozenset({"layout", "db_group", "page_patch"})


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


def _intersection_area(a: Sequence[float], b: Sequence[float]) -> float:
    return _area(
        [
            max(a[0], b[0]),
            max(a[1], b[1]),
            min(a[2], b[2]),
            min(a[3], b[3]),
        ]
    )


def _union_bbox(a: Sequence[float], b: Sequence[float]) -> list[float]:
    return [
        min(a[0], b[0]),
        min(a[1], b[1]),
        max(a[2], b[2]),
        max(a[3], b[3]),
    ]


def _merge_crosses_obstacle(
    a: Sequence[float],
    b: Sequence[float],
    obstacles: Sequence[dict[str, Any]],
    *,
    overlap_threshold: float,
) -> bool:
    merged = _union_bbox(a, b)
    for obstacle in obstacles:
        obstacle_bbox = obstacle["bbox"]
        a_contained = intersection_ratio(a, obstacle_bbox) >= overlap_threshold
        b_contained = intersection_ratio(b, obstacle_bbox) >= overlap_threshold
        if a_contained and b_contained:
            continue
        if a_contained != b_contained:
            return True
        existing_overlap = max(
            _intersection_area(a, obstacle_bbox),
            _intersection_area(b, obstacle_bbox),
        )
        if _intersection_area(merged, obstacle_bbox) > existing_overlap + 1e-9:
            return True
    return False


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


def _figure_relations(
    bbox: Sequence[float],
    figures: Sequence[dict[str, Any]],
    *,
    overlap_threshold: float,
) -> list[dict[str, Any]]:
    relations: list[dict[str, Any]] = []
    for figure in figures:
        ratio = intersection_ratio(bbox, figure["bbox"])
        if ratio <= 0:
            continue
        relations.append(
            {
                "figure_evidence_id": figure.get("evidence_id"),
                "overlap_ratio": round(ratio, 6),
                "contained": ratio >= 1.0 - 1e-9,
                "meets_threshold": ratio >= overlap_threshold,
            }
        )
    return relations


def build_evidence(
    layout_boxes: Sequence[dict[str, Any]],
    text_lines: Sequence[dict[str, Any]],
    *,
    page_number: int,
    width: int,
    height: int,
    figure_text_overlap: float,
    coordinate_space: str = "CropBox raster pixels, origin top-left",
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Assign stable per-page evidence IDs and describe figure relationships."""
    normalized_layout: list[dict[str, Any]] = []
    for index, box in enumerate(layout_boxes, start=1):
        normalized_box: dict[str, Any] = dict(box)
        normalized_box["bbox"] = [float(value) for value in box["bbox"]]
        normalized_box["evidence_id"] = f"L{index:03d}"
        normalized_layout.append(normalized_box)
    figures = [box for box in normalized_layout if box["type"] == "FIGURE"]
    for box in normalized_layout:
        box["figure_relations"] = _figure_relations(
            box["bbox"], figures, overlap_threshold=figure_text_overlap
        )

    normalized_lines: list[dict[str, Any]] = []
    for index, line in enumerate(text_lines, start=1):
        normalized_line: dict[str, Any] = dict(line)
        normalized_line["bbox"] = [float(value) for value in line["bbox"]]
        normalized_line["evidence_id"] = f"D{index:03d}"
        normalized_lines.append(normalized_line)
    for line in normalized_lines:
        line["figure_relations"] = _figure_relations(
            line["bbox"], figures, overlap_threshold=figure_text_overlap
        )

    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "page": page_number,
        "image": {"width": width, "height": height},
        "coordinate_space": coordinate_space,
        "layout_boxes": [
            {
                "evidence_id": box["evidence_id"],
                "label": box.get("label"),
                "type": box.get("type"),
                "score": round(float(box.get("score", 0.0)), 6),
                "bbox": box["bbox"],
                "figure_relations": box["figure_relations"],
            }
            for box in normalized_layout
        ],
        "db_lines": [
            {
                "evidence_id": line["evidence_id"],
                "label": line.get("label"),
                "type": line.get("type"),
                "score": round(float(line.get("score", 1.0)), 6),
                "bbox": line["bbox"],
                "figure_relations": line["figure_relations"],
            }
            for line in normalized_lines
        ],
    }
    return evidence, normalized_layout, normalized_lines


def aggregate_text_lines(
    lines: Sequence[dict[str, Any]],
    *,
    page_height: int,
    vertical_gap: float,
    horizontal_overlap_threshold: float,
    obstacles: Sequence[dict[str, Any]] = (),
    obstacle_overlap_threshold: float = 0.5,
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
                and not _merge_crosses_obstacle(
                    group["bbox"],
                    box,
                    obstacles,
                    overlap_threshold=obstacle_overlap_threshold,
                )
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
                    "evidence_ids": (
                        [line["evidence_id"]] if "evidence_id" in line else []
                    ),
                    "assigned_layout_evidence_ids": sorted(
                        set(line.get("assigned_layout_evidence_ids", []))
                    ),
                }
            )
            continue
        group = groups[best_index]
        group["bbox"] = _union_bbox(group["bbox"], box)
        count = int(group["line_count"])
        group["score"] = (group["score"] * count + float(line.get("score", 1.0))) / (
            count + 1
        )
        group["line_count"] = count + 1
        if "evidence_id" in line:
            group["evidence_ids"].append(line["evidence_id"])
            group["evidence_ids"] = sorted(set(group["evidence_ids"]))
        group["assigned_layout_evidence_ids"] = sorted(
            {
                *group["assigned_layout_evidence_ids"],
                *line.get("assigned_layout_evidence_ids", []),
            }
        )
    return groups


def _assign_line_types(
    lines: Sequence[dict[str, Any]], layout_boxes: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], set[int]]:
    text_types = {"BODY", "CAPTION", "FOOTNOTE", "TITLE", "HEADER", "LIST"}
    assigned_layout: set[int] = set()
    typed: list[dict[str, Any]] = []
    for line in lines:
        candidates = [
            (
                intersection_ratio(line["bbox"], box["bbox"]),
                index,
                box["type"],
                box.get("evidence_id"),
            )
            for index, box in enumerate(layout_boxes)
            if box["type"] in text_types
        ]
        ratio, index, line_type, layout_evidence_id = max(
            candidates, default=(0.0, -1, "BODY", None)
        )
        if ratio >= 0.5:
            assigned_layout.add(index)
            assigned_evidence_ids = (
                [layout_evidence_id] if isinstance(layout_evidence_id, str) else []
            )
        else:
            line_type = "BODY"
            assigned_evidence_ids = []
        typed.append(
            {
                **line,
                "type": line_type,
                "assigned_layout_evidence_ids": assigned_evidence_ids,
            }
        )
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
        raise PaddleError("candidate boxes contain duplicate stable IDs")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise PaddleError("reading-order override contains duplicate stable IDs")
    expected, supplied = set(by_id), set(ordered_ids)
    if expected != supplied:
        missing = sorted(expected - supplied)
        unknown = sorted(supplied - expected)
        raise PaddleError(
            f"reading-order IDs do not match candidates; missing={missing}, unknown={unknown}"
        )
    ordered = [by_id[box_id] for box_id in ordered_ids]
    for order, box in enumerate(ordered, start=1):
        box["reading_order"] = order
    return ordered


def _candidate_identity(
    box: Mapping[str, Any],
) -> tuple[str, float, float, float, float]:
    return (
        str(box["type"]),
        float(box["x0"]),
        float(box["y0"]),
        float(box["x1"]),
        float(box["y1"]),
    )


def _unique_items(values: Iterable[Any]) -> list[Any]:
    unique: list[Any] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique


def _candidate_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PaddleError(f"candidate {field} must be a list of strings")
    return list(value)


def _deduplicate_candidate_boxes(
    boxes: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate equal geometry while rejecting IDs shared by distinct boxes."""
    by_identity: dict[tuple[str, float, float, float, float], dict[str, Any]] = {}
    identity_by_id: dict[str, tuple[str, float, float, float, float]] = {}
    for candidate in boxes:
        box = dict(candidate)
        sources = _candidate_string_list(box.get("sources", []), "sources")
        source = box.get("source")
        if source is not None:
            if not isinstance(source, str) or source not in CANDIDATE_SOURCES:
                raise PaddleError(f"candidate has unsupported source {source!r}")
            sources.append(source)
        if any(source_name not in CANDIDATE_SOURCES for source_name in sources):
            raise PaddleError("candidate sources contain an unsupported value")
        evidence_ids = _candidate_string_list(
            box.get("source_evidence_ids", []), "source_evidence_ids"
        )
        figure_relations = box.get("figure_relations", [])
        if not isinstance(figure_relations, list) or not all(
            isinstance(relation, Mapping) for relation in figure_relations
        ):
            raise PaddleError("candidate figure_relations must be a list of mappings")
        box["sources"] = _unique_items(sources)
        box["source_evidence_ids"] = _unique_items(evidence_ids)
        box["figure_relations"] = list(figure_relations)
        identity = _candidate_identity(box)
        box_id = str(box["id"])
        previous_identity = identity_by_id.get(box_id)
        if previous_identity is not None and previous_identity != identity:
            raise PaddleError(f"stable candidate ID collision: {box_id}")
        identity_by_id[box_id] = identity
        existing = by_identity.get(identity)
        if existing is None:
            by_identity[identity] = box
            continue
        existing["sources"] = _unique_items(
            [
                *existing.get("sources", []),
                *box.get("sources", []),
                box.get("source"),
            ]
        )
        existing["sources"] = [
            source for source in existing["sources"] if source is not None
        ]
        existing["source_evidence_ids"] = _unique_items(
            [
                *existing.get("source_evidence_ids", []),
                *box.get("source_evidence_ids", []),
            ]
        )
        existing["figure_relations"] = _unique_items(
            [
                *existing.get("figure_relations", []),
                *box.get("figure_relations", []),
            ]
        )
        existing["score"] = max(
            float(existing.get("score", 0.0)), float(box.get("score", 0.0))
        )
    return list(by_identity.values())


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
    figure_text_policy: str = "keep",
    figure_obstacle_split: bool = False,
    candidate_source: str = "combined",
) -> list[dict[str, Any]]:
    figures = [box for box in layout_boxes if box["type"] == "FIGURE"]
    visible_lines = (
        suppress_figure_text(text_lines, figures, overlap_threshold=figure_text_overlap)
        if figure_text_policy == "exclude"
        else [dict(line) for line in text_lines]
    )
    typed_lines, assigned_layout = _assign_line_types(visible_lines, layout_boxes)
    groups = aggregate_text_lines(
        typed_lines,
        page_height=page_height,
        vertical_gap=vertical_gap,
        horizontal_overlap_threshold=horizontal_overlap_threshold,
        obstacles=figures if figure_obstacle_split else (),
        obstacle_overlap_threshold=0.5,
    )
    _infer_captions(
        groups,
        figures,
        page_height=page_height,
        caption_gap=caption_gap,
        overlap_threshold=horizontal_overlap_threshold,
    )

    textual = {"BODY", "CAPTION", "FOOTNOTE", "TITLE", "HEADER", "LIST"}
    retained: list[dict[str, Any]] = []
    if candidate_source != "db-only":
        for index, box in enumerate(layout_boxes):
            if (
                candidate_source == "combined"
                and box["type"] in textual
                and index in assigned_layout
            ):
                continue
            if (
                figure_text_policy == "exclude"
                and box["type"] in textual
                and any(
                    intersection_ratio(box["bbox"], figure["bbox"])
                    >= figure_text_overlap
                    for figure in figures
                )
            ):
                continue
            retained.append(dict(box))

    selected_groups = [] if candidate_source == "layout-only" else groups
    normalized: list[dict[str, Any]] = []
    for source in [*retained, *selected_groups]:
        bbox = [float(value) for value in source["bbox"]]
        box_type = str(source["type"])
        source_kind = "layout" if "label" in source else "db_group"
        normalized.append(
            {
                "id": _stable_id(page_number, box_type, bbox),
                "type": box_type,
                "x0": bbox[0],
                "y0": bbox[1],
                "x1": bbox[2],
                "y1": bbox[3],
                "score": round(float(source.get("score", 0.0)), 6),
                "source": source_kind,
                "source_evidence_ids": (
                    [source["evidence_id"]]
                    if "evidence_id" in source
                    else sorted(
                        {
                            *source.get("evidence_ids", []),
                            *source.get("assigned_layout_evidence_ids", []),
                        }
                    )
                ),
                "figure_relations": _figure_relations(
                    bbox, figures, overlap_threshold=figure_text_overlap
                ),
                **(
                    {"source_label": source["label"]}
                    if "label" in source
                    else {"text_line_count": int(source.get("line_count", 1))}
                ),
            }
        )
    return assign_reading_order(_deduplicate_candidate_boxes(normalized))


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


def draw_annotated_jpeg(
    source_path: Path,
    output_path: Path,
    boxes: Sequence[dict[str, Any]],
    *,
    quality: int,
    budget: Any | None = None,
) -> None:
    if not source_path.is_file():
        raise PaddleError(f"source JPEG is missing: {source_path}")
    pixmap = fitz.Pixmap(str(source_path))
    doc = fitz.open()
    try:
        page = doc.new_page(width=pixmap.width, height=pixmap.height)
        page.insert_image(page.rect, filename=str(source_path))
        for box in boxes:
            _draw_annotated_candidate(
                page,
                box,
                page_width=float(pixmap.width),
                page_height=float(pixmap.height),
            )
        rendered = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
        _atomic_write_bytes(
            output_path,
            rendered.tobytes("jpeg", jpg_quality=quality),
            budget=budget,
        )
    finally:
        doc.close()


def draw_raw_evidence_jpeg(
    source_path: Path,
    output_path: Path,
    items: Sequence[dict[str, Any]],
    *,
    kind: str,
    quality: int,
    budget: Any | None = None,
) -> None:
    """Draw neutral evidence labels without implying candidate acceptance."""
    if not source_path.is_file():
        raise PaddleError(f"source JPEG is missing: {source_path}")
    pixmap = fitz.Pixmap(str(source_path))
    doc = fitz.open()
    page = doc.new_page(width=pixmap.width, height=pixmap.height)
    page.insert_image(page.rect, filename=str(source_path))
    color = (0.25, 0.25, 0.25)
    for item in items:
        bbox = item["bbox"]
        page.draw_rect(fitz.Rect(*bbox), color=color, width=1)
        if kind == "layout":
            detail = f"{item.get('label', 'unknown')}/{item.get('type', 'OTHER')}"
        else:
            detail = "DB"
        label = f"{item['evidence_id']} {detail} {float(item.get('score', 0.0)):.3f}"
        page.insert_text(
            (float(bbox[0]), max(10.0, float(bbox[1]) - 2.0)),
            label,
            fontsize=7,
            color=color,
        )
    rendered = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
    _atomic_write_bytes(
        output_path,
        rendered.tobytes("jpeg", jpg_quality=quality),
        budget=budget,
    )
    doc.close()


def _atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    budget: Any | None = None,
) -> None:
    """Write *data* atomically, optionally charging a byte budget first.

    ``budget`` is an optional callable invoked with the payload size before
    anything is written; it raises when the caller's budget is exhausted.
    """
    if budget is not None:
        budget(len(data))
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


def _write_json(
    path: Path,
    payload: Any,
    *,
    budget: Any | None = None,
) -> None:
    serialized = json.dumps(json_ready(payload), indent=2, ensure_ascii=False) + "\n"
    _atomic_write_bytes(
        path,
        serialized.encode("utf-8"),
        budget=budget,
    )


def load_models(
    args: argparse.Namespace, source_path: Path, page_number: int
) -> tuple[Any, Any]:
    """Load Paddle models only on the real inference path."""
    os.environ.setdefault("OMP_NUM_THREADS", str(args.cpu_threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(args.cpu_threads))
    try:
        from paddleocr import LayoutDetection, TextDetection
    except ImportError as exc:
        raise PaddleError(
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
        raise PaddleError(
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
        raise PaddleError(
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
        raise PaddleError(
            f"model prediction failed; stage=predict model={LAYOUT_MODEL} "
            f"page={page_number} image={source_path}: {exc}"
        ) from exc
    try:
        text = text_model.predict(str(source_path), batch_size=1)
    except Exception as exc:
        raise PaddleError(
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


_JPEG_SOF_MARKERS = frozenset(
    marker for marker in range(0xC0, 0xD0) if marker not in {0xC4, 0xC8, 0xCC}
)
_MAX_JPEG_HEADER_BYTES = 64 * 1024


def jpeg_dimensions_from_bytes(data: bytes) -> tuple[int, int]:
    """Read JPEG width and height from the SOF marker of a bytes snapshot.

    The scan only walks segment headers (stdlib parsing, no image library), so
    oversized inputs can be rejected before any pixel buffer exists.
    """
    if not data.startswith(b"\xff\xd8"):
        raise PaddleError("data is not a JPEG file")
    position = 2
    length = len(data)
    while position + 3 < length:
        if data[position] != 0xFF:
            position += 1
            continue
        marker = data[position + 1]
        if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        if marker == 0xD9:
            break
        segment_length = (data[position + 2] << 8) | data[position + 3]
        if marker in _JPEG_SOF_MARKERS:
            if position + 8 >= length:
                raise PaddleError("JPEG header is truncated")
            height = (data[position + 5] << 8) | data[position + 6]
            width = (data[position + 7] << 8) | data[position + 8]
            if width <= 0 or height <= 0:
                raise PaddleError("JPEG header has invalid dimensions")
            return width, height
        position += 2 + segment_length
    raise PaddleError("JPEG dimensions are not readable")


def read_jpeg_dimensions(path: Path) -> tuple[int, int]:
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_JPEG_HEADER_BYTES)
    except OSError as exc:
        raise PaddleError(f"page image is unreadable: {path}: {exc}") from exc
    try:
        return jpeg_dimensions_from_bytes(data)
    except PaddleError as exc:
        raise PaddleError(f"{exc}: {path}") from exc


def _versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution in ("paddleocr", "paddlepaddle"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "not-installed"
    return versions


def _inference_params(args: argparse.Namespace, source_sha256: str) -> dict[str, Any]:
    return {
        "source_sha256": source_sha256,
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
        "figure_text_policy": args.figure_text_policy,
        "figure_obstacle_split": args.figure_obstacle_split,
        "candidate_source": args.candidate_source,
    }


def _validated_patch_boxes(
    result: Any,
    *,
    existing_boxes: Sequence[dict[str, Any]],
    page_number: int,
    width: int,
    height: int,
    patch_path: Path,
    evidence_ids: set[str],
    figures: Sequence[dict[str, Any]],
    figure_overlap_threshold: float,
) -> list[dict[str, Any]]:
    prefix = f"invalid page patch result; page={page_number} path={patch_path}"
    if not isinstance(result, list):
        raise PaddleError(f"{prefix}: expected a list of candidate mappings")
    existing_by_id = {str(box["id"]): box for box in existing_boxes}
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(result):
        if not isinstance(item, Mapping):
            raise PaddleError(f"{prefix}: item {index} is not a mapping")
        box_type = item.get("type")
        if not isinstance(box_type, str) or not box_type.strip():
            raise PaddleError(f"{prefix}: item {index} has an empty type")
        box_type = box_type.strip()
        if box_type not in CANDIDATE_TYPES:
            raise PaddleError(
                f"{prefix}: item {index} has unsupported type {box_type!r}"
            )
        sources_value = item.get("sources", [])
        if not isinstance(sources_value, list) or not all(
            isinstance(source, str) for source in sources_value
        ):
            raise PaddleError(
                f"{prefix}: item {index} sources must be a list of strings"
            )
        if any(source not in CANDIDATE_SOURCES for source in sources_value):
            raise PaddleError(
                f"{prefix}: item {index} sources contain an unknown value"
            )
        source_evidence_ids = item.get("source_evidence_ids")
        if not isinstance(source_evidence_ids, list) or not source_evidence_ids:
            raise PaddleError(
                f"{prefix}: item {index} source_evidence_ids must be a non-empty list"
            )
        if not all(isinstance(item_id, str) for item_id in source_evidence_ids):
            raise PaddleError(
                f"{prefix}: item {index} source_evidence_ids must contain strings"
            )
        unknown_evidence_ids = sorted(set(source_evidence_ids) - evidence_ids)
        if unknown_evidence_ids:
            raise PaddleError(
                f"{prefix}: item {index} references unknown evidence IDs "
                f"{unknown_evidence_ids}"
            )
        coordinates: list[float] = []
        for name in ("x0", "y0", "x1", "y1"):
            value = item.get(name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise PaddleError(f"{prefix}: item {index} has invalid {name}")
            coordinates.append(float(value))
        x0, y0, x1, y1 = coordinates
        if x0 < 0 or y0 < 0 or x1 > width or y1 > height or x1 <= x0 or y1 <= y0:
            raise PaddleError(f"{prefix}: item {index} bbox is outside the CropBox")

        score_value = item.get("score", 1.0)
        if (
            not isinstance(score_value, (int, float))
            or isinstance(score_value, bool)
            or not math.isfinite(float(score_value))
            or not 0 <= float(score_value) <= 1
        ):
            raise PaddleError(f"{prefix}: item {index} has an invalid score")

        supplied_id = item.get("id")
        existing = existing_by_id.get(str(supplied_id)) if supplied_id else None
        identity_matches = existing is not None and (
            str(existing["type"]) == box_type
            and [
                float(existing["x0"]),
                float(existing["y0"]),
                float(existing["x1"]),
                float(existing["y1"]),
            ]
            == coordinates
        )
        box_id = (
            str(supplied_id)
            if identity_matches
            else _stable_id(page_number, box_type, coordinates)
        )
        normalized_box = dict(item)
        input_sources = list(sources_value)
        input_sources.append("page_patch")
        normalized_box.update(
            {
                "id": box_id,
                "type": box_type,
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "score": round(float(score_value), 6),
                "source": "page_patch",
                "sources": _unique_items(input_sources),
                "source_evidence_ids": sorted(set(source_evidence_ids)),
                "figure_relations": _figure_relations(
                    coordinates,
                    figures,
                    overlap_threshold=figure_overlap_threshold,
                ),
            }
        )
        normalized_box.pop("reading_order", None)
        normalized.append(normalized_box)
    try:
        return assign_reading_order(_deduplicate_candidate_boxes(normalized))
    except PaddleError as exc:
        raise PaddleError(f"{prefix}: {exc}") from exc


def apply_page_patch(
    patch_path: Path,
    *,
    page_number: int,
    width: int,
    height: int,
    evidence: Mapping[str, Any],
    layout_boxes: Sequence[dict[str, Any]],
    text_lines: Sequence[dict[str, Any]],
    candidate_boxes: Sequence[dict[str, Any]],
    postprocess_params: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Execute one explicitly selected trusted hook, without a sandbox."""
    context = {
        "page": page_number,
        "image": {"width": width, "height": height},
        "layout_evidence": copy.deepcopy(evidence["layout_boxes"]),
        "db_evidence": copy.deepcopy(evidence["db_lines"]),
        "normalized_layout_boxes": copy.deepcopy(list(layout_boxes)),
        "normalized_text_lines": copy.deepcopy(list(text_lines)),
        "candidate_boxes": copy.deepcopy(list(candidate_boxes)),
        "postprocess_params": copy.deepcopy(dict(postprocess_params)),
    }
    layout_evidence = evidence["layout_boxes"]
    db_evidence = evidence["db_lines"]
    evidence_ids = {
        str(item["evidence_id"]) for item in [*layout_evidence, *db_evidence]
    }
    figures = [item for item in layout_evidence if item.get("type") == "FIGURE"]
    try:
        source = patch_path.read_bytes()
        patch_sha256 = hashlib.sha256(source).hexdigest()
        module_globals: dict[str, Any] = {
            "__file__": str(patch_path),
            "__name__": (f"paddle_page_patch_{page_number}_{patch_sha256[:12]}"),
        }
        code = compile(source, str(patch_path), "exec")
        exec(code, module_globals)  # noqa: S102 - This CLI runs explicit trusted hooks.
        patch_page = module_globals.get("patch_page")
        if not callable(patch_page):
            raise TypeError("module does not export callable patch_page(context)")
        result = patch_page(context)
    except Exception as exc:
        raise PaddleError(
            f"page patch failed; page={page_number} path={patch_path}"
        ) from exc
    return (
        _validated_patch_boxes(
            result,
            existing_boxes=candidate_boxes,
            page_number=page_number,
            width=width,
            height=height,
            patch_path=patch_path,
            evidence_ids=evidence_ids,
            figures=figures,
            figure_overlap_threshold=float(
                postprocess_params.get("figure_text_overlap", 0.5)
            ),
        ),
        {"path": str(patch_path), "sha256": patch_sha256},
    )


def _validate_reuse_meta(
    meta_path: Path,
    expected: Mapping[str, Any],
    page_number: int,
    *,
    max_pixels: int | None = None,
) -> Mapping[str, Any]:
    if not meta_path.is_file():
        raise PaddleError(
            f"raw metadata is missing for page {page_number}: {meta_path}"
        )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PaddleError(
            f"raw metadata is unreadable for page {page_number}: {exc}"
        ) from exc
    if not isinstance(meta, Mapping):
        raise PaddleError(
            f"raw metadata must be a JSON object for page {page_number}: {meta_path}"
        )
    if meta.get("schema_version") != RAW_SCHEMA_VERSION:
        raise PaddleError(
            f"raw schema mismatch for page {page_number}: "
            f"raw={meta.get('schema_version')!r} expected={RAW_SCHEMA_VERSION}"
        )
    if meta.get("page") != page_number:
        raise PaddleError(
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
        raise PaddleError(
            f"raw parameters do not match for page {page_number}: {details}"
        )

    image = meta.get("image")
    if not isinstance(image, Mapping):
        raise PaddleError(f"raw image metadata is missing for page {page_number}")
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
            f"raw rendering dimensions are invalid for page {page_number}: "
            f"width={width!r} height={height!r}"
        )

    files = meta.get("files")
    if not isinstance(files, Mapping):
        raise PaddleError(f"raw file hashes are missing for page {page_number}")
    required_names = ("source.jpg", "layout_raw.json", "text_raw.json")
    for name in required_names:
        path = meta_path.parent / name
        if not path.is_file():
            raise PaddleError(
                f"raw reuse file is missing for page {page_number}: {path}"
            )
        record = files.get(name)
        expected_sha = record.get("sha256") if isinstance(record, Mapping) else None
        actual_sha = _file_sha256(path)
        if expected_sha != actual_sha:
            raise PaddleError(
                f"raw file hash mismatch for page {page_number}: {name} "
                f"metadata={expected_sha!r} actual={actual_sha}"
            )
    source_path = meta_path.parent / "source.jpg"
    # Check the JPEG header before any full decode, and reject oversized
    # cached pages before a pixel buffer is allocated.
    try:
        header_width, header_height = read_jpeg_dimensions(source_path)
    except PaddleError as exc:
        raise PaddleError(
            f"cached source JPEG is invalid for page {page_number}: {exc}"
        ) from exc
    if max_pixels is not None and header_width * header_height > max_pixels:
        raise PaddleError(
            f"cached source JPEG exceeds the pixel limit for page {page_number}: "
            f"{header_width}x{header_height}"
        )
    if (header_width, header_height) != (width, height):
        raise PaddleError(
            f"raw rendering dimensions do not match the cached source JPEG "
            f"header for page {page_number}: "
            f"metadata={width}x{height} header={header_width}x{header_height}"
        )
    try:
        source = fitz.Pixmap(str(source_path))
    except Exception as exc:
        raise PaddleError(
            f"source JPEG is unreadable for page {page_number}: {source_path}: {exc}"
        ) from exc
    if (source.width, source.height) != (width, height):
        raise PaddleError(
            f"raw rendering dimensions do not match source JPEG for page {page_number}: "
            f"metadata={width}x{height} actual={source.width}x{source.height}"
        )
    return meta


def _read_raw_json(path: Path, page_number: int) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PaddleError(
            f"raw JSON is unreadable for page {page_number}: {path}: {exc}"
        ) from exc


__all__ = [
    "CANDIDATE_SCHEMA_VERSION",
    "CANDIDATE_SOURCES",
    "CANDIDATE_TYPES",
    "EVIDENCE_SCHEMA_VERSION",
    "LAYOUT_MODEL",
    "PaddleError",
    "RAW_SCHEMA_VERSION",
    "TEXT_MODEL",
    "_atomic_write_bytes",
    "_deduplicate_candidate_boxes",
    "_draw_annotated_candidate",
    "_file_sha256",
    "_inference_params",
    "_postprocess_params",
    "_read_raw_json",
    "_stable_id",
    "_validate_reuse_meta",
    "_validated_patch_boxes",
    "_versions",
    "_write_json",
    "aggregate_text_lines",
    "apply_page_patch",
    "apply_reading_order",
    "assign_reading_order",
    "build_candidate_boxes",
    "build_evidence",
    "draw_annotated_jpeg",
    "draw_raw_evidence_jpeg",
    "extract_layout_boxes",
    "extract_text_lines",
    "horizontal_overlap",
    "intersection_ratio",
    "json_ready",
    "jpeg_dimensions_from_bytes",
    "load_models",
    "map_layout_label",
    "normalize_results",
    "read_jpeg_dimensions",
    "run_models",
]
