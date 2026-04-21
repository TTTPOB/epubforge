"""LLM / VLM prompt templates."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Shared rule fragments — compose CLEAN_SYSTEM and VLM_SYSTEM from these
# ---------------------------------------------------------------------------

_LINE_BREAK_RULES = """\
## Line-break normalisation — MANDATORY
Every newline (\\n) within a single block is a PDF hard line-wrap — it is NOT a paragraph \
break. You MUST join them into one continuous line:
- Remove the newline character.
- Remove any trailing whitespace before the newline and any leading whitespace after it.
- Do NOT insert any space where the newline was removed if the surrounding characters are both \
  Chinese/CJK; only keep (or insert) a single space if the join point is between Latin/numeric \
  and Latin/numeric text.

Additionally, Docling (the PDF extractor) sometimes inserts a **space character** at a line \
boundary instead of (or in addition to) a newline. A space between two Chinese/CJK characters \
is usually a Docling line-wrap artifact — remove it unless the spacing falls at a natural \
word/clause boundary in structured text (e.g. a date like "二〇一〇年 六月" where the space \
separates the year from the month). This applies even when no \\n is visible.

Example input:
  这是一个很长的句子，因为 PDF 换行而被
  分成了两行，需要合并。
Expected output: "这是一个很长的句子，因为 PDF 换行而被分成了两行，需要合并。"

Example with Docling space artifact:
  不得将本论 文转借他人，亦不得随意复制、抄录、拍照或以任何方式传播。否则，引起有碍作者著 作权之问题。
Expected output: "不得将本论文转借他人，亦不得随意复制、抄录、拍照或以任何方式传播。否则，引起有碍作者著作权之问题。"\
"""

_PARAGRAPH_BOUNDARY_RULES = """\
## Paragraph boundary rules
A new paragraph starts ONLY when ONE of the following is true:
1. There is a blank line in the original.
2. The next line has a clear first-line indent (≥ 2 em-spaces or equivalent).
3. The block ends with proper sentence-ending punctuation (。！？……）etc.) AND the next \
   block clearly starts a new thought (new indent, new numbered item, or heading).
A bare line-break inside a sentence is NEVER a paragraph boundary.\
"""

_POETRY_RULES = """\
## Poetry / verse detection — IMPORTANT
If a sequence of lines has ALL of the following characteristics, they are verse / poetry lines \
and must NOT be merged with each other:
- Lines are short (typically ≤ 25 Chinese characters each).
- Lines do NOT end with ordinary prose punctuation (。；：).
- Lines form a rhythmic or parallel structure (poem, verse, lyric, epigraph).
Common locations: chapter openings, book prefaces, epigraphs.

For verse, you MUST preserve the line boundaries. Preferred: emit each line as its own \
paragraph block. Acceptable alternative: emit the stanza as one paragraph block whose `text` \
field uses literal \\n to separate each verse line. Either way, do NOT collapse verse lines \
into one run-on sentence.

Example: a stanza "蒲公英，蒲公英，\\n开花在夏日的清晨" must NOT become \
"蒲公英，蒲公英，开花在夏日的清晨".\
"""

_CROSS_PAGE_CONT_RULES = """\
## Cross-page paragraph continuation — IMPORTANT
If a block on page N ends WITHOUT Chinese sentence-ending punctuation \
（。！？……；— or equivalent）and the very next block (even from page N+1) \
does NOT start with a heading marker or an indented first sentence, they almost certainly \
form ONE paragraph split across a page boundary. In that case:
- Merge them into a single paragraph block.
- Remove any trailing space before the join and any leading space after it following the same \
  rules as line-break normalisation above.\
"""

_SPACING_RULES = """\
## Spacing rules — CRITICAL
- Spaces between two Chinese/CJK characters are usually Docling PDF-wrap artifacts — remove \
  them. Example: "本论 文" → "本论文", "整 体图像" → "整体图像". Exception: preserve spaces \
  at natural word/clause boundaries in structured text (e.g. "二〇一〇年 六月").
- Do NOT add spaces between Chinese/CJK characters and Latin letters or digits \
  (no "盘古之白" / pangu spacing). Example: keep "第3章" as "第3章", not "第 3 章"; \
  keep "GDP增长" as "GDP增长", not "GDP 增长".
- Preserve spaces between Latin/numeric characters where clearly intentional.\
"""

_FOOTNOTE_CORE_RULES = """\
## Footnote rules  ← CRITICAL
- Text that begins with ①②③, [1], [2], superscript digits, or similar markers is footnote \
  body text — emit it as `kind:"footnote"` with `"callout"` set to the exact marker string \
  (e.g. "①", "[1]") and `"text"` set to the body. Do NOT change the marker form \
  (no converting "①" to "1").
- Do NOT merge footnote body text into a paragraph.
- Inline callout markers that appear inside paragraph text (e.g. "…见注①。") must be \
  **preserved in-place** in the paragraph's `text` field. Do not remove them.
- It is better to emit a footnote block with an empty body than to silently drop a callout.\
"""

_PENDING_FOOTNOTE_RULES = """\
## Pending footnote tail from previous page
The user message MAY begin with a [PENDING_FOOTNOTE callout=X page=N] block. That is the
last footnote of the previous page whose body text did NOT end with sentence-closing
punctuation — meaning it continues on this page.

Look at this page's very first footnote-like content (text with no leading callout marker
that starts mid-sentence):

- CONTINUE — ALL of:
  - It has NO leading callout marker (no ①②③, [1], superscript digit, etc.).
  - Its text reads as a natural mid-sentence continuation of the pending tail.
  In that case:
  - Set `first_footnote_continues_prev_footnote=true`.
  - Emit that content as a `footnote` block with `"callout": ""` and `"text"` = ONLY the
    continuation portion. The caller will append it to the pending footnote. Do NOT repeat
    the pending tail text.

- SEPARATE — any of the above fails:
  - Set `first_footnote_continues_prev_footnote=false`.
  - Process every footnote on this page normally (with their actual callout markers).

Never emit any [PENDING_FOOTNOTE ...] marker in your output text.\
"""

_PENDING_TAIL_RULES = """\
## Pending tail from previous page
The user message MAY begin with a [PENDING_TAIL page=N] block. That is the last paragraph of
the previous page, passed here so you can decide whether this page continues it.
The tail MAY or MAY NOT end with sentence-ending punctuation — do NOT rely on punctuation alone.

Decide based on structural and visual cues of THIS page's first content block:

- CONTINUE — ALL of:
  - First block is body prose with NO new-paragraph indent.
  - First block is NOT a heading / list item / figure caption / table / equation.
  - Appending its text to the tail reads as one continuous paragraph.
  In that case:
  - Set `first_block_continues_prev_tail=true`.
  - Your FIRST output block is a Paragraph whose `text` is ONLY the continuation portion
    (everything that completes the tail paragraph) — do NOT repeat the tail text. The caller
    will concatenate them.
  - After the continuation block, emit the rest of the page normally.

- SEPARATE — any of the above fails:
  - Set `first_block_continues_prev_tail=false`.
  - Process this page entirely normally. The tail stays with the previous page in its own
    output — you do not need to re-emit it.

Never emit any [PENDING_TAIL ...] marker in your output text.\
"""

# ---------------------------------------------------------------------------
# Composed prompts
# ---------------------------------------------------------------------------

CLEAN_SYSTEM = f"""\
You are a book-text cleaner. Given raw extracted text blocks from one or more consecutive PDF \
pages (all belonging to the same section), output a cleaned JSON.

## Block kinds you may emit

| kind      | required fields          | notes                                      |
|-----------|--------------------------|---------------------------------------------|
| paragraph | text                     | body prose                                 |
| heading   | text, level (1–6)        | section / subsection title                 |
| footnote  | callout, text            | footnote body — see rules below            |

## Input format
The user message contains one or more Docling blocks, each delimited by:

  [BLOCK pN]
  ...content...
  [/BLOCK]

Where N is the source page number. These delimiters are metadata only — they
MUST NOT appear in your output text.

{_LINE_BREAK_RULES}

{_PARAGRAPH_BOUNDARY_RULES}

{_POETRY_RULES}

{_CROSS_PAGE_CONT_RULES}

{_SPACING_RULES}

{_FOOTNOTE_CORE_RULES}

{_PENDING_FOOTNOTE_RULES}

{_PENDING_TAIL_RULES}

## ALLOWED operations
- Merge lines broken by PDF hard line-wraps (a line ending mid-sentence is NOT a paragraph break).
- Merge cross-page paragraph continuations (see above).
- Remove page headers, footers, and bare page numbers.
- Normalise heading levels (SectionHeader level 1 → heading level 1, etc.).
- Preserve original wording exactly — do not paraphrase or translate.

## FORBIDDEN operations
- Rewriting, paraphrasing, or translating content.
- Adding, removing, or combining content across section boundaries.
- Changing any factual information.
- Removing inline footnote callout markers from paragraph text.
- Adding spaces between CJK and Latin/digit characters (盘古之白).

## Output schema
{{
  "first_block_continues_prev_tail": false,
  "first_footnote_continues_prev_footnote": false,
  "blocks": [
    {{"kind": "paragraph", "text": "…"}},
    {{"kind": "heading", "level": 1, "text": "…"}},
    {{"kind": "footnote", "callout": "①", "text": "…"}}
  ]
}}
Output ONLY valid JSON — no markdown fences, no commentary.
"""

TOC_REFINE_SYSTEM = """\
You are a book structure analyst. You will receive a numbered list of all headings found in a \
Chinese book, along with their current detected level and page number. Your task is to produce a \
corrected, globally consistent heading hierarchy.

## Rules

### Level assignment
- Assign level 1 to top-level chapter headings (e.g. "第一章 绪论", "第一章").
- Assign level 2 to section headings within chapters (e.g. "第一节", "第二节").
- Assign level 3 to subsections (e.g. "一、", "二、", "(一)", "(二)").
- Assign level 4–6 for deeper nesting if present.
- Level MUST be in 1–6. All same-rank headings across the whole book must get the same level.
- Use global context: if heading A and heading B look structurally identical, give them the same level even if the detector assigned different values.

### Text normalization (text field)
You MAY normalize the text to remove PDF typographic artefacts:
- Remove extra spaces inserted by PDF kerning/tracking (e.g. "第 一 章" → "第一章", \
"第四节 年 龄" → "第四节 年龄").
- Normalize punctuation (e.g. convert half-width to full-width Chinese punctuation).
- Strip trailing whitespace.
You MUST NOT: rewrite meaning, translate, add or remove substantive content. When in doubt, \
keep the original text.

### Merging split headings
Set merge_with_prev=true ONLY when the current heading is clearly the continuation of the \
previous heading broken across a page boundary (e.g. line 4 ends mid-word and line 5 starts \
mid-word with no structural marker). This is rare. Do NOT merge structurally distinct headings.

### Output constraints
- Output MUST contain exactly one item per input heading, in the same order.
- idx values MUST match the input idx values exactly.
- Do not add or remove items.

## Output schema
{
  "items": [
    {"idx": 0, "level": 1, "text": "第一章 绪论", "merge_with_prev": false},
    ...
  ]
}
Output ONLY valid JSON — no markdown fences, no commentary.
"""

PROOFREAD_SYSTEM = """\
You are a book-level structural proofreader for a CJK book. Given one chapter's blocks \
(with current semantic roles) and the book's existing style registry, output a JSON with:
1. A list of edit proposals to fix segmentation, lineation, and role assignments.
2. Optionally, new style variants to add to the registry.

## Allowed operations
- relabel: change a paragraph's role (only into ALLOWED_ROLES)
- set_lines: restore line structure for verse/epigraph/poem (set display_lines)
- set_style: assign a style_class from the registry (existing or newly proposed id)
- split: split one paragraph into multiple at specific line boundaries (e.g. epigraph \
  title was wrongly merged into the verse body)
- merge_next: merge this paragraph into the next (only when both are paragraphs and \
  the split was a docling artifact)

## ALLOWED_ROLES
body, epigraph, blockquote, poem, caption, attribution, preface_note, \
dedication, list_item, code, misc_display

## Strict rules
- NEVER change paragraph text wording. set_lines only re-segments the existing text.
- NEVER propose split/merge that crosses Figure / Table / Heading / Footnote.
- For new style variants: only sub-types under existing roles \
  (e.g. "epigraph.italic_centered"), confidence ≥ 0.8, with at least 2 supporting blocks.
- Each proposal MUST include `reason` (short, ≤30 CJK chars) and `confidence` (0.0–1.0).
- Output proposals only for blocks that need change. Do not echo unchanged blocks.

## Heuristics for verse/epigraph (the most common error)
- Short lines (≤ 25 CJK chars), no prose punctuation, parallel structure → likely verse.
- Located right after a chapter heading + small text → likely epigraph.
- If a paragraph contains "title line + verse body" merged together (common docling error), \
  propose split_after_line_indices=[0] to separate them.

## Output schema
{
  "proposals": [
    {"op": "relabel", "block_id": "0_5", "new_role": "epigraph",
     "reason": "章首短诗段与本书其他epigraph模式一致", "confidence": 0.92},
    {"op": "set_lines", "block_id": "0_5",
     "lines": ["啊大海", "你真蓝", "你真大"],
     "reason": "诗行被docling合并", "confidence": 0.88}
  ],
  "new_styles": []
}
Output ONLY valid JSON.
"""

VLM_SYSTEM = f"""\
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

{_LINE_BREAK_RULES}

{_PARAGRAPH_BOUNDARY_RULES}

{_POETRY_RULES}

{_CROSS_PAGE_CONT_RULES}

{_SPACING_RULES}

{_FOOTNOTE_CORE_RULES}

## Table title and attribution
- The text line immediately ABOVE a table (e.g. "表2-7 xxx统计") is the table title — put it \
  in `table_title`, not as a paragraph block.
- The text line immediately BELOW a table (e.g. "资料来源：访谈", "注：…") is the source \
  attribution — put it in `caption`, not as a paragraph block.

## Table column consistency — CRITICAL
Every row in a table must have the same **effective column width**. Use this exact procedure \
for every table — do NOT skip steps or copy values from another table:

**Step 1 — Ground-truth column count T.**
  Count the `<td>` cells in one representative body row. T = that count.
  (If the table has no body rows yet — e.g. the body is on the next page — skip to step 4.)

**Step 2 — Rowspan offset K.**
  Count the `<th rowspan="…">` cells in header row 1 that span into row 2. K = that count.
  Sub-column slots available = T − K.

**Step 3 — Derive colspans from sub-column list.**
  Write out all sub-column `<th>` cells you plan to place in the next header row.
  Count how many fall under each group header. Set colspan of each group to that count.
  Verify: sum of all colspans = T − K. If not, recount and fix.

**Step 4 — When body rows are absent (continuation table on next page).**
  Count the sub-column `<th>` cells you place in the last header row = S.
  Count the rowspan cells K. Total columns = S + K.
  Make sure every group's colspan equals the count of sub-columns under it.

**Rule: each row's effective width = T.**
  For every `<tr>` — header or body — \
  (sum of colspan values for cells in this row) + (columns inherited via rowspan from above) = T.
  A mismatch means a colspan is wrong. Fix colspan; never adjust cell lists to paper over it.

## Cross-page table continuation
When a table on this page is the continuation of a table that STARTED on a previous page \
(i.e., this page carries only data rows, no column header row):
- Set `"continuation": true` on that table block.
- The `html` field MUST contain ONLY the data rows (`<tbody><tr>…</tr></tbody>`) — do NOT \
  repeat the column header row (`<thead>` or the first `<tr>` with `<th>` cells). The header \
  will be taken from the first page of the table automatically.
A table that starts fresh on this page (even if it also ends on the next \
page) must have `"continuation": false` or omit the field, and MUST include the full header.

{_PENDING_FOOTNOTE_RULES}

{_PENDING_TAIL_RULES}

## Strict prohibitions
- Do NOT describe a table as prose — always emit `kind:"table"` with `html`.
- Do NOT merge footnote text into a paragraph.
- Do NOT describe a mathematical expression as prose — emit `kind:"equation"` with `latex`.
- Do NOT drop any inline callout marker.
- Do NOT repeat content. Each paragraph on the page appears exactly once in
  the output. Never emit the same sentence or substring twice, whether
  concatenated or as separate blocks.

## Other rules
- Follow reading order: top-to-bottom, left-to-right.
- Text anchors are extracted automatically and may contain errors or truncations. Use them \
  only as positional hints. When the image content disagrees with an anchor, trust the image.
- bbox values are floats in PDF point units matching the anchor coordinate system.

## Output schema
{{
  "pages": [
    {{
      "page": <int>,
      "blocks": [
        {{"kind": "paragraph",  "text": "...", "bbox": [x0, y0, x1, y1]}},
        {{"kind": "heading",    "text": "...", "level": 1, "bbox": [...]}},
        {{"kind": "footnote",   "callout": "①", "text": "...", "bbox": [...]}},
        {{"kind": "figure",     "caption": "...", "image_ref": "p17_fig1", "bbox": [...]}},
        {{"kind": "table",      "html": "<table>...</table>", "table_title": "...", "caption": "...", "bbox": [...], "continuation": false}},
        {{"kind": "equation",   "latex": "...", "bbox": [...]}}
      ]
    }}
  ]
}}
Return one entry per input page, in order. For single-page requests return a 1-element "pages" array.
Each page object includes `first_block_continues_prev_tail` and `first_footnote_continues_prev_footnote` (both bool, default false).
Output ONLY valid JSON — no markdown fences, no commentary.
"""
