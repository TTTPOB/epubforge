"""Canonical page geometry contract shared by the ingestion stages.

``page_geometry`` uses top-left coordinates in the page's displayed
orientation.  Its width and height match the MinerU ``page_size`` values.
Consumers must compare those dimensions with the displayed PDF ``page.rect``
before scaling bboxes.  A rotated page therefore uses the already rotated
width and height; callers must not apply another rotation transform.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
from typing import Any


PAGE_GEOMETRY_TOLERANCE = 1e-3
PAGE_GEOMETRY_CONTRACT = (
    "top-left coordinates in displayed orientation; dimensions match MinerU "
    "layout page_size and displayed PDF page.rect; no rotation transform"
)


class PageGeometryError(ValueError):
    """Raised when a page geometry record violates the shared contract."""


def normalize_page_geometry(
    value: Any,
    *,
    page_count: int,
) -> tuple[dict[str, int | float], ...]:
    """Return page geometry with canonical float dimensions and ordered pages."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PageGeometryError("page_geometry must be an array")
    if len(value) != page_count:
        raise PageGeometryError(
            "page_geometry must contain exactly one record per page"
        )

    normalized: list[dict[str, int | float]] = []
    for position, entry in enumerate(value):
        if not isinstance(entry, Mapping) or set(entry) != {
            "page_idx",
            "width",
            "height",
        }:
            raise PageGeometryError(
                f"page_geometry entry {position} must contain page_idx, width, height"
            )
        page_idx = entry.get("page_idx")
        if (
            not isinstance(page_idx, int)
            or isinstance(page_idx, bool)
            or page_idx != position
        ):
            raise PageGeometryError("page_geometry must be ordered by page_idx")
        dimensions: dict[str, int | float] = {"page_idx": page_idx}
        for name in ("width", "height"):
            part = entry.get(name)
            if (
                not isinstance(part, (int, float))
                or isinstance(part, bool)
                or not math.isfinite(float(part))
                or float(part) <= 0
            ):
                raise PageGeometryError(f"page_geometry {name} is invalid")
            dimensions[name] = float(part)
        normalized.append(dimensions)
    return tuple(normalized)


def content_source_sha256(
    items: Sequence[Mapping[str, Any]],
    page_geometry: Sequence[Mapping[str, Any]],
) -> str:
    """Hash items and canonical page geometry with stable int/float semantics."""
    geometry = normalize_page_geometry(page_geometry, page_count=len(page_geometry))
    payload = {"items": list(items), "page_geometry": list(geometry)}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "PAGE_GEOMETRY_CONTRACT",
    "PAGE_GEOMETRY_TOLERANCE",
    "PageGeometryError",
    "content_source_sha256",
    "normalize_page_geometry",
]
