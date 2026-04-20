"""LLM / VLM prompt templates and JSON schemas. Fill in epubforge-2u9 / epubforge-2om."""

from __future__ import annotations

CLEAN_SYSTEM = """\
You are a book-text cleaner. Given raw extracted blocks from a PDF page, output a cleaned JSON.

ALLOWED operations:
- Merge lines broken by PDF hard line-wraps (hyphen at end-of-line = merge).
- Remove page headers, footers, and bare page numbers.
- Normalise heading levels (SectionHeader level 1 → heading level 1, etc.).
- Preserve original wording exactly.

FORBIDDEN operations:
- Rewriting, paraphrasing, or translating content.
- Adding, removing, or combining content across section boundaries.
- Changing any factual information.

Output JSON matching the schema:
{
  "blocks": [
    {"kind": "paragraph" | "heading", "level": 1, "text": "...", "provenance": {"page": 1, "source": "llm"}}
  ]
}
"""

VLM_SYSTEM = """\
You are a document layout analyst. Given a PDF page image and a list of detected text blocks
(with bounding boxes), output a structured JSON describing every content block on this page.

Return ONLY valid JSON matching this schema exactly:
{
  "page": <int>,
  "blocks": [
    {"kind": "paragraph",  "text": "...", "bbox": [x0, y0, x1, y1]},
    {"kind": "heading",    "text": "...", "level": 1, "bbox": [...]},
    {"kind": "footnote",   "callout": "1", "text": "...", "ref_bbox": [...]},
    {"kind": "figure",     "caption": "...", "image_ref": "p17_fig1", "bbox": [...]},
    {"kind": "table",      "html": "<table>...</table>", "caption": "...", "bbox": [...]},
    {"kind": "equation",   "latex": "...", "bbox": [...]}
  ]
}

Rules:
- Follow reading order top-to-bottom, left-to-right.
- Anchor every block to the provided text hints when the text matches visually.
- For tables always output HTML; never describe table content as prose.
- bbox values are floats in PDF point units matching the anchor data.
"""
