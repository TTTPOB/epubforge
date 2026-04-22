# EPUB Editorial Process — Standard Operating Procedure

This document covers post-pipeline manual adjustments to the EPUB structure:
front matter reorganization, heading styling, TOC control, and page breaks.

All edits target `work/<book>/07_footnote_verified.json` (or the latest stage
output). Rebuild with:

```bash
uv run epubforge build fixtures/<book>.pdf --force-rerun
```

---

## 1. IR Quick Reference

### Block kinds and text fields

| kind | relevant fields |
|------|----------------|
| `paragraph` | `text`, `role`, `style_class` |
| `heading` | `text`, `level`, `id`, `style_class` |
| `table` | `html`, `table_title`, `caption` |
| `footnote` | `text`, `callout` |

### Heading rendering and TOC rules

- `Heading.level` determines both the HTML tag and TOC depth:
  - level 1 → `<h2>` on page, TOC depth 2 (child of chapter)
  - level 2 → `<h3>` on page, TOC depth 3
  - Chapter title is always H1, TOC depth 1.
- **`id` controls TOC entry**: heading appears in the TOC if and only if `id`
  is a non-empty string. Set `id: null` (or omit) to render on the page but
  exclude from the TOC.
- `Heading.style_class` maps to a CSS class on the rendered HTML element
  (e.g., `<h2 class="centered-section">`).

### Available CSS classes (epub_builder._CSS_BASE)

| class | effect |
|-------|--------|
| `p.epigraph` | italic, indented margin — for chapter-opening quotes |
| `p.poem` | centered, pre-wrap — for standalone poems |
| `p.attribution` | right-aligned italic — for poem/quote sources |
| `p.dedication` | centered italic — for dedications |
| `p.blockquote` | indented margin |
| `p.preface-note` | smaller font, indented |
| `p.centered-bold` | centered, bold — for author/affiliation lines |
| `h2.centered` | H2 centered, no page break |
| `h2.centered-section` | H2 centered + `break-before: page` (EPUB page break) |

---

## 2. Merging Front Matter Chapters

**When**: The VLM emits several short chapters for the thesis cover page,
dedication, poem, abstract, and English abstract. For EPUB distribution these
read better as one chapter with named H2 sections.

**Pattern** (implemented for zxgb):

```python
import json, copy
from pathlib import Path

path = Path('work/<book>/07_footnote_verified.json')
data = json.loads(path.read_text())
chs = data['chapters']

PROV = {'page': 1, 'source': 'passthrough'}

def heading(text, id_=None, style_class=None, page=1):
    b = {'kind': 'heading', 'level': 1, 'text': text,
         'provenance': {'page': page, 'source': 'passthrough'}}
    if id_ is not None:
        b['id'] = id_
    if style_class:
        b['style_class'] = style_class
    return b

# Pull source chapters by index (adjust for your book)
ch_cover   = chs[2]   # e.g. 博士研究生学位论文
ch_poem    = chs[5]   # e.g. 献诗 蒲公英
ch_abstract_cn = chs[6]
ch_title_en    = chs[7]  # English title page (chapter title = thesis title)
ch_abstract_en = chs[8]

blocks = []

# ── H2 sections (centered-section = centered + page break) ──────────────
blocks.append(heading('原封面', id_='cover', style_class='centered-section'))
for b in ch_cover['blocks']:
    blocks.append(copy.deepcopy(b))

blocks.append(heading('献给中县干部', id_='dedication', style_class='centered-section'))
# if the source chapter has no blocks, the heading alone serves as the page

blocks.append(heading('献诗 蒲公英', id_='poem', style_class='centered-section'))
for b in ch_poem['blocks']:
    b2 = copy.deepcopy(b)
    b2['role'] = 'poem'   # centered, not epigraph-indented
    blocks.append(b2)

blocks.append(heading('摘要', id_='abstract-cn', style_class='centered-section'))
for b in ch_abstract_cn['blocks']:
    blocks.append(copy.deepcopy(b))

blocks.append(heading('Abstract', id_='abstract-en', style_class='centered-section'))
# Thesis English title: visual H2, NOT in TOC (no id_)
blocks.append(heading(ch_title_en['title'], id_=None, style_class='centered'))
# Author / department / supervisor as bold-centered paragraphs
for b in ch_title_en['blocks']:
    b2 = copy.deepcopy(b)
    if b2.get('kind') == 'heading':
        b2 = {'kind': 'paragraph', 'role': 'body',
              'style_class': 'centered-bold',
              'text': b2['text'], 'provenance': b2['provenance']}
    blocks.append(b2)
for b in ch_abstract_en['blocks']:
    blocks.append(copy.deepcopy(b))

new_ch = {'title': '论文信息', 'blocks': blocks}

# Replace source chapters with new merged chapter; keep the rest
# Adjust slice to match which chapters were consumed
data['chapters'] = [new_ch] + chs[9:]

path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
print(f'{len(blocks)} blocks in new chapter; {len(data["chapters"])} chapters total')
```

**Notes**:
- `Provenance.source` must be one of `"llm"`, `"vlm"`, `"passthrough"`.
  Use `"passthrough"` for manually injected blocks.
- The English title page is often a single-string chapter title with author and
  department concatenated (VLM limitation). Split it into separate
  `centered-bold` paragraphs manually after inspecting `ch_title_en['title']`.

---

## 3. TOC Suppression (visual heading, no TOC entry)

Set `id: null` on the heading block. The epub_builder only adds a heading to
the TOC when `block.id` is a non-empty string:

```python
# In TOC (normal)
{'kind': 'heading', 'level': 1, 'id': 'abstract-en',
 'style_class': 'centered-section', 'text': 'Abstract', ...}

# On page only, NOT in TOC
{'kind': 'heading', 'level': 1, 'id': None,
 'style_class': 'centered', 'text': "Zhong County's Cadre", ...}
```

---

## 4. Page Breaks Between Sections

Use `style_class: "centered-section"` on headings that should start on a new
page. The CSS rule is:

```css
h2.centered-section { text-align: center; break-before: page; page-break-before: always; }
```

`page-break-before` is the legacy property (Kindle, older readers);
`break-before` is the modern CSS property. Both are included for compatibility.

For non-heading page breaks (e.g., before a figure or table), add a
zero-height paragraph with `style_class: "page-break"` and the CSS:

```css
p.page-break { break-before: page; page-break-before: always; margin: 0; }
```

---

## 5. Poem vs Epigraph Roles

| role | CSS | Use when |
|------|-----|----------|
| `epigraph` | italic, left-indent (`margin: 1em 3em`) | chapter-opening quote, short |
| `poem` | centered, pre-wrap | standalone poem page, song lyrics |

Change role on existing blocks:
```python
for b in ch['blocks']:
    if b.get('role') == 'epigraph':
        b['role'] = 'poem'
```

---

## 6. Verification Checklist

After rebuilding, confirm in the EPUB file:

```bash
unzip -q out/<book>.epub -d /tmp/epub_check
```

1. **H2 classes**: `grep 'centered-section\|centered-bold' /tmp/epub_check/EPUB/chap0000.xhtml`
2. **TOC nesting**: open `nav.xhtml`, confirm front-matter H2s are `<ol>` children
   of the chapter `<li>`, not siblings.
3. **TOC suppression**: confirm the no-id heading text does NOT appear in `nav.xhtml`.
4. **Page breaks**: open in a real EPUB reader (Calibre, Apple Books) and page
   through — each `centered-section` heading should start on a fresh page.
