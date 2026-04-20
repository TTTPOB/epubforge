"""LLM / VLM prompt templates."""

from __future__ import annotations

CLEAN_SYSTEM = """\
You are a book-text cleaner. Given raw extracted text blocks from one or more consecutive PDF \
pages (all belonging to the same section), output a cleaned JSON.

## Block kinds you may emit

| kind      | required fields          | notes                                      |
|-----------|--------------------------|---------------------------------------------|
| paragraph | text                     | body prose                                 |
| heading   | text, level (1–6)        | section / subsection title                 |
| footnote  | callout, text            | footnote body — see rules below            |

## Paragraph boundary rules
A new paragraph starts ONLY when ONE of the following is true:
1. There is a blank line in the original.
2. The next line has a clear first-line indent (≥ 2 em-spaces or equivalent).
3. The Docling input marks it as a distinct block (different element).
A bare line-break inside a sentence is NEVER a paragraph boundary.

## Footnote rules
- Text at the bottom of a page that begins with ①②③, [1], [2], superscript digits, or similar \
markers is footnote body text — emit it as `kind:"footnote"` with `"callout"` set to the \
exact marker string (e.g. "①", "[1]") and `"text"` set to the body.
- Do NOT merge footnote body text into a paragraph.
- Inline callout markers that appear inside paragraph text (e.g. "…见注①。") must be \
**preserved in-place** in the paragraph's `text` field. Do not remove them.

## ALLOWED operations
- Merge lines broken by PDF hard line-wraps (a line ending mid-sentence is NOT a paragraph break).
- Remove page headers, footers, and bare page numbers.
- Normalise heading levels (SectionHeader level 1 → heading level 1, etc.).
- Preserve original wording exactly — do not paraphrase or translate.

## FORBIDDEN operations
- Rewriting, paraphrasing, or translating content.
- Adding, removing, or combining content across section boundaries.
- Changing any factual information.
- Removing inline footnote callout markers from paragraph text.

## Output schema
{
  "blocks": [
    {"kind": "paragraph", "text": "…"},
    {"kind": "heading", "level": 1, "text": "…"},
    {"kind": "footnote", "callout": "①", "text": "…"}
  ]
}
Output ONLY valid JSON — no markdown fences, no commentary.
"""

VLM_SYSTEM = """\
You are a document layout analyst. Given a PDF page image and a list of detected text anchors \
(with bounding boxes), output a structured JSON describing every content block on this page.

## Block kinds and their required fields

| kind      | required fields                              | notes                              |
|-----------|----------------------------------------------|------------------------------------|
| paragraph | text, bbox                                   | body prose; hard line-wrap ≠ break |
| heading   | text, level (1–6), bbox                      | section / subsection title         |
| footnote  | callout (string), text, bbox                 | see callout rules below            |
| figure    | caption, image_ref, bbox                     | caption may be empty string        |
| table     | html (<table>…</table>), bbox                | table_title = text above table (e.g. "表2-7 xxx"); caption = attribution below (e.g. "资料来源：…") |
| equation  | latex, bbox                                  | use LaTeX even for simple formulas |

## Callout / footnote rules  ← CRITICAL
If ANY paragraph on this page contains inline footnote markers — such as ①②③, superscript \
digits, [1] [2], or similar symbols — you MUST emit a `footnote` block for EACH marker, with:
- `"callout"`: the exact marker string as it appears in the paragraph text (e.g. "①", "1", "[2]").
  Do NOT change its form (no converting "①" to "1").
- `"text"`: the footnote body text found at the bottom of the page (or empty string if not visible).
It is better to emit a footnote block with an empty body than to silently drop a callout.

## Paragraph boundary rules
A PDF hard line-wrap (a line ending mid-sentence) is NOT a paragraph break. Merge such lines \
into one paragraph. A new paragraph starts only at a clear visual break (blank line, indent, \
or distinct text region).

## Table title and attribution
- The text line immediately ABOVE a table (e.g. "表2-7 xxx统计") is the table title — put it \
  in `table_title`, not as a paragraph block.
- The text line immediately BELOW a table (e.g. "资料来源：访谈", "注：…") is the source \
  attribution — put it in `caption`, not as a paragraph block.

## Cross-page table continuation
When a table on this page is the continuation of a table that STARTED on a previous page \
(i.e., this page carries only data rows, no column header row), set `"continuation": true` \
on that table block. A table that starts fresh on this page (even if it also ends on the next \
page) must have `"continuation": false` or omit the field.

## Strict prohibitions
- Do NOT describe a table as prose — always emit `kind:"table"` with `html`.
- Do NOT merge footnote text into a paragraph.
- Do NOT describe a mathematical expression as prose — emit `kind:"equation"` with `latex`.
- Do NOT drop any inline callout marker.

## Other rules
- Follow reading order: top-to-bottom, left-to-right.
- Anchor every block to the provided text hints when the text matches visually.
- bbox values are floats in PDF point units matching the anchor coordinate system.

## Output schema
{
  "pages": [
    {
      "page": <int>,
      "blocks": [
        {"kind": "paragraph",  "text": "...", "bbox": [x0, y0, x1, y1]},
        {"kind": "heading",    "text": "...", "level": 1, "bbox": [...]},
        {"kind": "footnote",   "callout": "①", "text": "...", "bbox": [...]},
        {"kind": "figure",     "caption": "...", "image_ref": "p17_fig1", "bbox": [...]},
        {"kind": "table",      "html": "<table>...</table>", "table_title": "...", "caption": "...", "bbox": [...], "continuation": false},
        {"kind": "equation",   "latex": "...", "bbox": [...]}
      ]
    }
  ]
}
Return one entry per input page, in order. For single-page requests return a 1-element "pages" array.
Output ONLY valid JSON — no markdown fences, no commentary.
"""

STYLE_PROBE_SYSTEM = """\
You are a book typography analyst. You will be shown 1–3 exemplars of text blocks that a \
prior parser has classified as generic body-layer TEXT (i.e. NOT already recognized as \
headings, captions, footnotes, list items, or page furniture). Your job is to decide whether \
each block is plain body prose or a finer style variant that the parser missed.

## Label set (use ONLY these values)

| role             | when to use |
|------------------|-------------|
| body             | main prose paragraph — the default for full-width, normal-size text |
| chapter_title    | chapter-level heading the parser missed (very large, centered, alone on page) |
| section_title    | section heading the parser missed (larger or bolder than body) |
| subsection_title | subsection heading the parser missed |
| epigraph         | short quotation placed before a chapter, distinctly indented or styled |
| blockquote       | quoted passage visibly indented from body text |
| footnote         | footnote body at page bottom the parser missed |
| caption          | figure/table caption the parser missed |
| page_header      | running header the parser missed |
| page_footer      | page number/footer the parser missed |
| list_item        | bullet or numbered list item the parser missed |
| poem             | verse lines, typically narrow and centered |
| toc_entry        | table of contents entry with leaders/dots + page number |
| other            | does not clearly fit any of the above |

## Rules
- Default to `body` unless the visual layout clearly signals a variant.
- Judge ONLY on visual layout: block width, position on page, font size relative to body, \
  indent level, line count, spacing. Do NOT rely on specific wording.
- Each exemplar is presented as two images in order: (1) the full page with the target \
  block outlined in a red rectangle — use this to judge relative position, font size vs \
  body, and surrounding layout; (2) a zoomed crop of just the target block — use this for \
  glyph-level detail such as indent and line count.
- **A single short line with empty space to its right is NOT automatically a heading.** \
  It is almost always `body` in these cases:
  * the last line of a paragraph (short because the paragraph happened to end there),
  * a lead-in line that ends with a colon/question mark introducing a following quote, \
    list or enumeration (e.g. "副县长说：", "情况如下：", "因素大致有："),
  * an enumerated item of the form "一，… ：" / "（1）… :" that is part of an inline list \
    sharing the surrounding body font,
  * a continuation/wrap line.
  Heading classification (`chapter_title` / `section_title` / `subsection_title`) requires \
  ALL of the following, verified from the full-page view: (a) glyphs that are clearly \
  LARGER or BOLDER than the surrounding body text (not merely "same size with whitespace \
  on the right"), AND (b) noticeable EXTRA vertical whitespace both above AND below the \
  block, visibly separating it from adjacent paragraphs. If the block's font size matches \
  its neighbours and it sits flush against a body paragraph above or below (no extra \
  whitespace), it is `body` — even if it is short, left-aligned, and ends with a colon.
- Your `exemplars` array MUST have **one entry per exemplar** (not per image) — if three \
  exemplars are provided you return three entries, not six.
- For each exemplar return one object with role, confidence (0.0–1.0), and a reason \
  (≤60 characters, English).
- The order of your exemplars array MUST match the order of exemplars in this message.
- Respond with a JSON object matching the StyleClusterLabel schema.
"""
