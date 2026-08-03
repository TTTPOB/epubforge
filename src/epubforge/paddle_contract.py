"""Paddle contract constants and stable candidate IDs.

The PDF tuning CLI, the agent page-image CLI, and Stage 5 validation all share
this contract surface: schema versions, the accepted candidate type set, and
the deterministic candidate ID derivation.  Keeping them here lets revision
validation recompute every candidate ID from its page/type/bbox instead of
trusting the agent-supplied string.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

RAW_SCHEMA_VERSION = 2
CANDIDATE_SCHEMA_VERSION = 3
EVIDENCE_SCHEMA_VERSION = 1

LAYOUT_MODEL = "PP-DocLayout-S"
TEXT_MODEL = "PP-OCRv5_mobile_det"

# Coordinate-space contracts of the agent page-image CLI.  Revision validates
# collected evidence against exactly these values so a forged or mismatched
# coordinate space is rejected.
PAGE_EVIDENCE_COORDINATE_SPACE = "workspace page raster pixels, origin top-left"
PAGE_CANDIDATE_COORDINATE_SPACE: dict[str, str] = {
    "source": "workspace page image",
    "units": "pixels",
    "origin": "top-left",
    "x_axis": "right",
    "y_axis": "down",
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


def stable_candidate_id(page_number: int, box_type: str, bbox: Sequence[float]) -> str:
    """Derive the deterministic stable candidate ID from geometry.

    Two candidates with the same page, type, and box always share an ID, so a
    validator can recompute the ID and reject forged strings.
    """
    canonical = []
    for value in bbox:
        number = float(value)
        if number == 0.0:
            number = 0.0
        canonical.append(number.hex())
    identity = f"{page_number}:{box_type}:" + ":".join(canonical)
    digest = hashlib.sha1(identity.encode("ascii")).hexdigest()[:10]
    return f"p{page_number:04d}-{box_type.lower()}-{digest}"


__all__ = [
    "CANDIDATE_SCHEMA_VERSION",
    "CANDIDATE_TYPES",
    "EVIDENCE_SCHEMA_VERSION",
    "LAYOUT_MODEL",
    "PAGE_CANDIDATE_COORDINATE_SPACE",
    "PAGE_EVIDENCE_COORDINATE_SPACE",
    "RAW_SCHEMA_VERSION",
    "TEXT_MODEL",
    "stable_candidate_id",
]
