#!/usr/bin/env python3
"""Tune PaddleOCR layout and text-detection parameters on selected PDF pages."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import math
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
CANDIDATE_SCHEMA_VERSION = 3
EVIDENCE_SCHEMA_VERSION = 1


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

CANDIDATE_TYPES = frozenset(
    {
        "FIGURE",
        "BODY",
        "CAPTION",
        "FOOTNOTE",
        "TITLE",
        "HEADER",
        "LIST",
        "TABLE",
        "FORMULA",
        "OTHER",
    }
)
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
        "coordinate_space": "CropBox raster pixels, origin top-left",
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
    canonical = []
    for value in bbox:
        number = float(value)
        if number == 0.0:
            number = 0.0
        canonical.append(number.hex())
    identity = f"{page_number}:{box_type}:" + ":".join(canonical)
    digest = hashlib.sha1(identity.encode("ascii")).hexdigest()[:10]
    return f"p{page_number:04d}-{box_type.lower()}-{digest}"


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
        raise TuneError(f"candidate {field} must be a list of strings")
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
                raise TuneError(f"candidate has unsupported source {source!r}")
            sources.append(source)
        if any(source_name not in CANDIDATE_SOURCES for source_name in sources):
            raise TuneError("candidate sources contain an unsupported value")
        evidence_ids = _candidate_string_list(
            box.get("source_evidence_ids", []), "source_evidence_ids"
        )
        figure_relations = box.get("figure_relations", [])
        if not isinstance(figure_relations, list) or not all(
            isinstance(relation, Mapping) for relation in figure_relations
        ):
            raise TuneError("candidate figure_relations must be a list of mappings")
        box["sources"] = _unique_items(sources)
        box["source_evidence_ids"] = _unique_items(evidence_ids)
        box["figure_relations"] = list(figure_relations)
        identity = _candidate_identity(box)
        box_id = str(box["id"])
        previous_identity = identity_by_id.get(box_id)
        if previous_identity is not None and previous_identity != identity:
            raise TuneError(f"stable candidate ID collision: {box_id}")
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


_COLORS = {
    "FIGURE": (220 / 255, 38 / 255, 38 / 255),
    "CAPTION": (234 / 255, 88 / 255, 12 / 255),
    "BODY": (22 / 255, 101 / 255, 52 / 255),
    "FOOTNOTE": (126 / 255, 34 / 255, 206 / 255),
    "TABLE": (2 / 255, 132 / 255, 199 / 255),
    "FORMULA": (190 / 255, 24 / 255, 93 / 255),
}

# Keep annotation geometry explicit so visual review and tests share one contract.
ANNOTATED_BBOX_STROKE_WIDTH = 4.0
ANNOTATED_LABEL_FONT_NAME = "helv"
ANNOTATED_LABEL_FONT_SIZE = 12.0
ANNOTATED_LABEL_PADDING = 2.0
ANNOTATED_LABEL_GAP = 2.0
ANNOTATED_LABEL_FILL_OPACITY = 0.5
ANNOTATED_LABEL_BLACK = (0.0, 0.0, 0.0)
ANNOTATED_LABEL_WHITE = (1.0, 1.0, 1.0)


def _label_text_color(color: Sequence[float]) -> tuple[float, float, float]:
    luminance = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
    return ANNOTATED_LABEL_BLACK if luminance > 0.5 else ANNOTATED_LABEL_WHITE


def _label_geometry(
    label: str,
    candidate_rect: fitz.Rect,
    *,
    page_width: float,
    page_height: float,
) -> tuple[fitz.Rect, tuple[float, float], float] | None:
    font = fitz.Font(fontname=ANNOTATED_LABEL_FONT_NAME)
    label_width = (
        fitz.get_text_length(
            label,
            fontname=ANNOTATED_LABEL_FONT_NAME,
            fontsize=ANNOTATED_LABEL_FONT_SIZE,
        )
        + 2.0 * ANNOTATED_LABEL_PADDING
    )
    label_height = (
        ANNOTATED_LABEL_FONT_SIZE * (font.ascender - font.descender)
        + 2.0 * ANNOTATED_LABEL_PADDING
    )
    if label_width > page_width or label_height > page_height:
        return None

    label_x = min(
        max(0.0, float(candidate_rect.x0)),
        max(0.0, page_width - label_width),
    )
    above_y = float(candidate_rect.y0) - ANNOTATED_LABEL_GAP - label_height
    below_y = float(candidate_rect.y1) + ANNOTATED_LABEL_GAP
    if above_y >= 0.0:
        label_y = above_y
    elif below_y + label_height <= page_height:
        label_y = below_y
    else:
        label_y = min(max(0.0, above_y), page_height - label_height)

    rect = fitz.Rect(label_x, label_y, label_x + label_width, label_y + label_height)
    origin = (
        label_x + ANNOTATED_LABEL_PADDING,
        label_y + ANNOTATED_LABEL_PADDING + ANNOTATED_LABEL_FONT_SIZE * font.ascender,
    )
    return rect, origin, ANNOTATED_LABEL_FONT_SIZE


def _draw_annotated_candidate(
    page: fitz.Page,
    box: dict[str, Any],
    *,
    page_width: float,
    page_height: float,
) -> None:
    color = _COLORS.get(str(box["type"]), (0.1, 0.1, 0.1))
    candidate_rect = fitz.Rect(box["x0"], box["y0"], box["x1"], box["y1"])
    page.draw_rect(
        candidate_rect,
        color=color,
        width=ANNOTATED_BBOX_STROKE_WIDTH,
    )
    label = f"{box['reading_order']} {box['id']} {box['type']}"
    geometry = _label_geometry(
        label,
        candidate_rect,
        page_width=page_width,
        page_height=page_height,
    )
    if geometry is None:
        return
    label_rect, text_origin, font_size = geometry
    page.draw_rect(
        label_rect,
        color=None,
        fill=color,
        fill_opacity=ANNOTATED_LABEL_FILL_OPACITY,
        width=0,
    )
    page.insert_text(
        text_origin,
        label,
        fontname=ANNOTATED_LABEL_FONT_NAME,
        fontsize=font_size,
        color=_label_text_color(color),
    )


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
        _atomic_write_bytes(output_path, rendered.tobytes("jpeg", jpg_quality=quality))
    finally:
        doc.close()


def draw_raw_evidence_jpeg(
    source_path: Path,
    output_path: Path,
    items: Sequence[dict[str, Any]],
    *,
    kind: str,
    quality: int,
) -> None:
    """Draw neutral evidence labels without implying candidate acceptance."""
    if not source_path.is_file():
        raise TuneError(f"source JPEG is missing: {source_path}")
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
        "figure_text_policy": args.figure_text_policy,
        "figure_obstacle_split": args.figure_obstacle_split,
        "candidate_source": args.candidate_source,
    }


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
        raise TuneError(f"{prefix}: expected a list of candidate mappings")
    existing_by_id = {str(box["id"]): box for box in existing_boxes}
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(result):
        if not isinstance(item, Mapping):
            raise TuneError(f"{prefix}: item {index} is not a mapping")
        box_type = item.get("type")
        if not isinstance(box_type, str) or not box_type.strip():
            raise TuneError(f"{prefix}: item {index} has an empty type")
        box_type = box_type.strip()
        if box_type not in CANDIDATE_TYPES:
            raise TuneError(f"{prefix}: item {index} has unsupported type {box_type!r}")
        sources_value = item.get("sources", [])
        if not isinstance(sources_value, list) or not all(
            isinstance(source, str) for source in sources_value
        ):
            raise TuneError(f"{prefix}: item {index} sources must be a list of strings")
        if any(source not in CANDIDATE_SOURCES for source in sources_value):
            raise TuneError(f"{prefix}: item {index} sources contain an unknown value")
        source_evidence_ids = item.get("source_evidence_ids")
        if not isinstance(source_evidence_ids, list) or not source_evidence_ids:
            raise TuneError(
                f"{prefix}: item {index} source_evidence_ids must be a non-empty list"
            )
        if not all(isinstance(item_id, str) for item_id in source_evidence_ids):
            raise TuneError(
                f"{prefix}: item {index} source_evidence_ids must contain strings"
            )
        unknown_evidence_ids = sorted(set(source_evidence_ids) - evidence_ids)
        if unknown_evidence_ids:
            raise TuneError(
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
                raise TuneError(f"{prefix}: item {index} has invalid {name}")
            coordinates.append(float(value))
        x0, y0, x1, y1 = coordinates
        if x0 < 0 or y0 < 0 or x1 > width or y1 > height or x1 <= x0 or y1 <= y0:
            raise TuneError(f"{prefix}: item {index} bbox is outside the CropBox")

        score_value = item.get("score", 1.0)
        if (
            not isinstance(score_value, (int, float))
            or isinstance(score_value, bool)
            or not math.isfinite(float(score_value))
            or not 0 <= float(score_value) <= 1
        ):
            raise TuneError(f"{prefix}: item {index} has an invalid score")

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
    except TuneError as exc:
        raise TuneError(f"{prefix}: {exc}") from exc


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
            "__name__": (f"paddle_layout_page_patch_{page_number}_{patch_sha256[:12]}"),
        }
        code = compile(source, str(patch_path), "exec")
        exec(code, module_globals)  # noqa: S102 - This CLI runs explicit trusted hooks.
        patch_page = module_globals.get("patch_page")
        if not callable(patch_page):
            raise TypeError("module does not export callable patch_page(context)")
        result = patch_page(context)
    except Exception as exc:
        raise TuneError(
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
