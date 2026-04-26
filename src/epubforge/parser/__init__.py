"""Stage 1 parsers — PDF → DoclingDocument JSON."""

from __future__ import annotations

from epubforge.parser.docling_parser import parse_pdf
from epubforge.parser.granite_parser import (
    GranitePagesResult,
    GraniteParseResult,
    parse_pdf_granite,
    parse_pdf_granite_segmented,
)

__all__ = [
    "parse_pdf",
    "parse_pdf_granite",
    "parse_pdf_granite_segmented",
    "GraniteParseResult",
    "GranitePagesResult",
]
