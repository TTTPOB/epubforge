# Punctuation Audit Process — Standard Operating Procedure

This document describes how to audit and unify dash/hyphen punctuation in
`work/<book>/07_footnote_verified.json` (or `06_proofread.json` if stage 7 has not run).

---

## 0. Field Coverage

Every block kind has specific text-bearing fields. The audit and fix scripts must cover
**all of them** — missing even one causes visible inconsistency (e.g., a table title with
`1978-2009` while every cell says `1978—2009`):

| Block kind | Text fields |
|------------|-------------|
| `paragraph` | `text` |
| `table` | `html`, `table_title`, `caption` |
| `footnote` | `text`, `callout` |
| `heading` | `text` |
| `figure` | (no free text to audit) |

Run this first to confirm the field map for any new book — VLM schema may add fields:

```python
from collections import defaultdict
import json
from pathlib import Path

data = json.loads(Path('work/<book>/07_footnote_verified.json').read_text())
kind_fields = defaultdict(set)
for ch in data['chapters']:
    for b in ch['blocks']:
        kind = b.get('kind','')
        for k, v in b.items():
            if isinstance(v, str) and v.strip() and k not in ('kind',):
                kind_fields[kind].add(k)
for kind, fields in sorted(kind_fields.items()):
    print(f'{kind}: {sorted(fields)}')
```

---

## 1. Inventory Script

Scan all text fields for dash/hyphen character variants:

```bash
uv run python3 - <<'PYEOF' > work/<book>/punct_audit_report.txt
import json, re
from pathlib import Path
from collections import Counter

data = json.loads(Path('work/<book>/07_footnote_verified.json').read_text())

DASHES = {
    '\u002D': 'HYPHEN-MINUS (-)',
    '\u2010': 'HYPHEN (‐)',
    '\u2011': 'NON-BREAKING HYPHEN (‑)',
    '\u2012': 'FIGURE DASH (‒)',
    '\u2013': 'EN DASH (–)',
    '\u2014': 'EM DASH (—)',
    '\u2015': 'HORIZONTAL BAR (―)',
    '\u2E3A': 'TWO-EM DASH (⸺)',
    '\u2E3B': 'THREE-EM DASH (⸻)',
    '\uFF0D': 'FULLWIDTH HYPHEN-MINUS（－）',
}

FIELD_MAP = {
    'paragraph': ['text'],
    'table':     ['html', 'table_title', 'caption'],
    'footnote':  ['text', 'callout'],
    'heading':   ['text'],
}

counts = Counter()
samples = {}

def scan(text, source):
    for ch, name in DASHES.items():
        if ch in text:
            counts[ch] += text.count(ch)
            if ch not in samples:
                idx = text.index(ch)
                snippet = text[max(0,idx-10):idx+10].replace('\n',' ')
                samples[ch] = f'[{source}] ...{snippet}...'

for ci, ch in enumerate(data['chapters']):
    for bi, b in enumerate(ch['blocks']):
        kind = b.get('kind','')
        src = f'ch{ci} b{bi} {kind}'
        for field in FIELD_MAP.get(kind, []):
            val = b.get(field) or ''
            if val: scan(val, src + f'.{field}')

print('=== DASH CHARACTER INVENTORY ===')
for ch in sorted(counts.keys()):
    print(f'\nU+{ord(ch):04X} {DASHES[ch]}')
    print(f'  count: {counts[ch]}')
    print(f'  sample: {samples.get(ch,"")}')
PYEOF
```

---

## 2. Categorization Script

Categorize each `-` (HYPHEN-MINUS) by surrounding character context across **all fields**:

```bash
uv run python3 - <<'PYEOF'
import json, re
from pathlib import Path
from collections import defaultdict

data = json.loads(Path('work/<book>/07_footnote_verified.json').read_text())

FN_MARKER = re.compile(r'\x02fn-\d+-[^\x03]+\x03')
CJK = r'[\u4e00-\u9fff]'

FIELD_MAP = {
    'paragraph': ['text'],
    'table':     ['html', 'table_title', 'caption'],
    'footnote':  ['text', 'callout'],
    'heading':   ['text'],
}

categories = defaultdict(list)

def scan(text):
    for m in FN_MARKER.finditer(text):
        pass  # just warm up; actual exclusion below
    clean_ranges = []
    prev = 0
    for m in FN_MARKER.finditer(text):
        clean_ranges.append((prev, m.start()))
        prev = m.end()
    clean_ranges.append((prev, len(text)))

    for start, end in clean_ranges:
        for m in re.finditer(r'-', text[start:end]):
            h = start + m.start()
            before = text[h-1] if h > 0 else ''
            after  = text[h+1] if h+1 < len(text) else ''
            ctx = text[max(0,h-10):h+11].replace('\n',' ')
            if re.match(r'\d', before) and re.match(r'\d', after):
                categories['数字范围 digit-digit'].append(ctx)
            elif re.match(CJK, before) and re.match(CJK, after):
                categories['汉字间 cjk-cjk'].append(ctx)
            elif re.match(r'[a-zA-Z]', before) and re.match(r'[a-zA-Z]', after):
                categories['英文连字 alpha-alpha'].append(ctx)
            else:
                categories['其他'].append(ctx)

for ch in data['chapters']:
    for b in ch['blocks']:
        kind = b.get('kind','')
        for field in FIELD_MAP.get(kind, []):
            val = b.get(field) or ''
            if val: scan(val)

for cat, items in sorted(categories.items()):
    seen = set()
    print(f'\n{cat}: {len(items)} occurrences')
    for s in items:
        if s not in seen:
            seen.add(s)
            print(f'  {repr(s)}')
        if len(seen) >= 6:
            break
PYEOF
```

Also check `—` sequence types (single / double / triple):

```bash
uv run python3 - <<'PYEOF'
import json, re
from pathlib import Path
from collections import Counter

data = json.loads(Path('work/<book>/07_footnote_verified.json').read_text())
EM = '\u2014'
seq_counts = Counter()

FIELD_MAP = {
    'paragraph': ['text'],
    'table':     ['html', 'table_title', 'caption'],
    'footnote':  ['text'],
    'heading':   ['text'],
}

for ch in data['chapters']:
    for b in ch['blocks']:
        kind = b.get('kind','')
        for field in FIELD_MAP.get(kind, []):
            for m in re.finditer(f'{EM}+', b.get(field,'') or ''):
                seq_counts[m.group()] += 1

for seq, cnt in sorted(seq_counts.items(), key=lambda x: -x[1]):
    print(f'{repr(seq)} × {cnt}')
PYEOF
```

---

## 3. Known Punctuation Patterns

### Pattern P1: Mixed `-` / `—` in number and date ranges

**Cause**: VLM inconsistently converts the range connector from the PDF. Some pages emit
`-` (HYPHEN-MINUS), others emit `—` (EM DASH) for the same semantic purpose. The
inconsistency can appear **between fields of the same table** — e.g., `table_title` left
in `-` form while cell content was converted to `—`, producing a visible mismatch to the
reader.

**Symptom**: Inventory shows both `U+002D` and `U+2014`; categorization shows a large
`数字范围 digit-digit` class under `-`, and single `—` occurrences in the text are also
clearly ranges (`1978—2000`, `35—55岁`).

**Rule — use `-` (HYPHEN-MINUS) for all numeric ranges**:
- Lighter visually, especially in dense table columns of dates and ages.
- Consistent with the majority of VLM output and with table titles.
- Readers of Chinese academic books find both acceptable; `-` is less ambiguous with
  `——` (破折号) that may appear nearby in prose.

**Includes**:
- Year ranges: `1978-2009`
- Age/count ranges: `28-31岁`, `5-7年`
- Bibliography page ranges: `pp. 585-599` (follow source language convention)
- **Table numbers**: `表7-6`, `表6-15` — the VLM occasionally emits `表7—6`; fix these too.

**Does NOT affect**:
- English compound words: `multi-level`, `full-time` → always `-`
- Chinese compound academic terms: `政-党`, `理性-技术` → keep book's style
- Footnote markers: `\x02fn-10-①\x03` → **never touch**

**Bulk fix** (covers all text-bearing fields):

```python
import json, re
from pathlib import Path

path = Path('work/<book>/07_footnote_verified.json')
data = json.loads(path.read_text())

FN_MARKER = re.compile(r'\x02fn-\d+-[^\x03]+\x03')

FIELD_MAP = {
    'paragraph': ['text'],
    'table':     ['html', 'table_title', 'caption'],
    'footnote':  ['text', 'callout'],
    'heading':   ['text'],
}

def fix_numeric_dashes(text: str) -> tuple[str, int]:
    """Replace em dashes in numeric contexts with hyphen-minus. Skips fn markers."""
    parts = FN_MARKER.split(text)
    markers = FN_MARKER.findall(text)
    total = 0
    new_parts = []
    for part in parts:
        # digit — digit (optional surrounding whitespace)
        part, n1 = re.subn(r'(\d)\s*—\s*(\d)', r'\1-\2', part)
        # 年 — digit
        part, n2 = re.subn(r'(年)\s*—\s*(\d)', r'\1-\2', part)
        # digit — 至今
        part, n3 = re.subn(r'(\d)\s*—\s*(至今)', r'\1-\2', part)
        new_parts.append(part)
        total += n1 + n2 + n3
    result = ''.join(
        p + (markers[i] if i < len(markers) else '')
        for i, p in enumerate(new_parts)
    )
    return result, total

total = 0
detail = {}
for ch in data['chapters']:
    for b in ch['blocks']:
        kind = b.get('kind','')
        for field in FIELD_MAP.get(kind, []):
            val = b.get(field)
            if not isinstance(val, str) or not val: continue
            new_val, n = fix_numeric_dashes(val)
            if n:
                b[field] = new_val
                total += n
                detail[f'{kind}.{field}'] = detail.get(f'{kind}.{field}', 0) + n

path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
print(f'Fixed {total} em dashes → hyphens')
for k, v in sorted(detail.items(), key=lambda x: -x[1]):
    print(f'  {k}: {v}')
```

---

### Pattern P2: Single vs double EM DASH (Chinese 破折号)

**Cause**: VLM sometimes emits a single `—` where Chinese typography requires `——`
(two consecutive EM DASHes = 破折号).

**Symptom**: `——` count is near-zero, but single `—` appears flanked by Chinese characters
in a discourse context: e.g., `从革命老干部——革命动员者和意识` → correct; but if it arrived
as `从革命老干部—革命动员者` → broken 破折号.

**Do NOT bulk-convert all single `—` to `——`** — after P1 fix, remaining single `—` are
either correct 破折号 already, or CJK compound separators. Fix individually if needed.

---

### Pattern P3: Bibliography ibid. (`———`)

Three consecutive EM DASHes mark "same author as above" in Chinese academic bibliography.
**This is correct; do not alter.**

---

### Pattern P4: CJK compound term inconsistency

**Cause**: The same compound concept appears with `-` in most places but `—` in a few
(e.g., `政-党` dominant, `忠诚—命令` isolated).

**When to fix**: Only when both forms refer to the **same term**. Identify the dominant
form (≥ 80% of occurrences) and convert the minority.

**When NOT to fix**: If the two styles carry different semantic weight — `-` for tight
compound nouns, `—` for appositive pairs in quotation marks — leave both; the difference
may be intentional.

---

## 4. Verification

After applying fixes:

1. **Zero residual digit-`—`-digit**:
   ```python
   import json, re
   from pathlib import Path
   data = json.loads(Path('work/<book>/07_footnote_verified.json').read_text())
   FN_MARKER = re.compile(r'\x02fn-\d+-[^\x03]+\x03')
   remaining = sum(
       len(re.findall(r'\d—\d', FN_MARKER.sub('', b.get(f,'') or '')))
       for ch in data['chapters'] for b in ch['blocks']
       for f in ['text','html','table_title','caption','callout']
   )
   print('residual digit-—-digit:', remaining)  # expect 0
   ```

2. **Footnote markers intact** — spot-check a few in the JSON; the `\x02fn-` prefix must
   not have changed.

3. **Rebuild and inspect**:
   ```bash
   uv run epubforge build fixtures/<book>.pdf --force-rerun
   ```
   Open the EPUB; find a table that has both a year range in the title and in the cells
   and confirm both use the same connector.
