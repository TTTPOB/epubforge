"""Stage 1 parsers — PDF → DoclingDocument JSON."""

from __future__ import annotations

from epubforge.parser.docling_parser import parse_pdf
from epubforge.parser.granite_parser import GraniteParseResult, parse_pdf_granite

__all__ = ["parse_pdf", "parse_pdf_granite", "GraniteParseResult"]
