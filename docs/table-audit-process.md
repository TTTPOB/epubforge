# Table Audit Process — Standard Operating Procedure

This document describes how to audit table HTML in `work/<book>/07_footnote_verified.json`,
interpret the results, and manually fix structural issues.

---

## 1. Audit Script

Run the following from the project root to produce a full report:

```bash
uv run python3 - <<'PYEOF' > work/zxgb/table_audit_report.txt
import json, re
from pathlib import Path

data = json.loads(Path('work/zxgb/07_footnote_verified.json').read_text())

TD_RE  = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)
TH_RE  = re.compile(r'<t[hd][^>]*>(.*?)</t[hd]>', re.DOTALL)
ROW_RE = re.compile(r'<tr>(.*?)</tr>', re.DOTALL)

def effective_cols(row_html):
    """Sum of colspan values for all cells in a row."""
    total = 0
    for m in re.finditer(r'<t[hd]([^>]*)>', row_html):
        attrs = m.group(1)
        cs = re.search(r'colspan=["\']?(\d+)', attrs)
        total += int(cs.group(1)) if cs else 1
    return total

output = []

# --- A. Double-tbody scan ---
output.append('=== DOUBLE-TBODY TABLES ===')
double_found = False
for ci, ch in enumerate(data['chapters']):
    for bi, b in enumerate(ch['blocks']):
        if b.get('kind') != 'table': continue
        html = b.get('html','')
        count = html.count('</tbody><tbody>')
        if count:
            pg = b.get('provenance',{}).get('page','?')
            title = (b.get('table_title') or '')[:60]
            output.append(f'  ch{ci} b{bi} p{pg} ({count} joins): {title}')
            double_found = True
if not double_found:
    output.append('  (none found)')
output.append('')

# --- B. Split-row scan ---
output.append('=== LIKELY SPLIT ROWS (body2 first row mostly empty) ===')
split_found = False
for ci, ch in enumerate(data['chapters']):
    for bi, b in enumerate(ch['blocks']):
        if b.get('kind') != 'table': continue
        html = b.get('html','')
        if '</tbody><tbody>' not in html: continue
        bodies = html.split('</tbody><tbody>')
        for body_idx in range(1, len(bodies)):
            body = bodies[body_idx]
            first_row = ROW_RE.search(body)
            if not first_row: continue
            cells = TD_RE.findall(first_row.group(1))
            empty = sum(1 for c in cells if not c.strip())
            if cells and empty >= len(cells) // 2:
                pg = b.get('provenance',{}).get('page','?')
                title = (b.get('table_title') or '')[:50]
                output.append(f'  ch{ci} b{bi} p{pg}: {title}')
                output.append(f'    body{body_idx} first row: {cells}')
                split_found = True
if not split_found:
    output.append('  (none found)')
output.append('')

# --- C. Column-count inconsistency scan ---
output.append('=== COLUMN COUNT INCONSISTENCIES ===')
col_found = False
for ci, ch in enumerate(data['chapters']):
    for bi, b in enumerate(ch['blocks']):
        if b.get('kind') != 'table': continue
        html = b.get('html','')
        rows = ROW_RE.findall(html)
        if len(rows) < 2: continue
        col_counts = [effective_cols(r) for r in rows]
        max_cols = max(col_counts)
        for i, (row_html, cols) in enumerate(zip(rows, col_counts)):
            if cols < max_cols * 0.6 and cols > 0:
                pg = b.get('provenance',{}).get('page','?')
                title = (b.get('table_title') or '')[:50]
                output.append(f'  ch{ci} b{bi} p{pg} row{i}: {cols}/{max_cols} cols — {title}')
                output.append(f'    row HTML: {row_html[:120]}')
                col_found = True
                break  # one report per table
if not col_found:
    output.append('  (none found)')
output.append('')

# --- D. All tables summary ---
output.append('=== ALL TABLES SUMMARY ===')
for ci, ch in enumerate(data['chapters']):
    for bi, b in enumerate(ch['blocks']):
        if b.get('kind') != 'table': continue
        html = b.get('html','')
        pg = b.get('provenance',{}).get('page','?')
        title = (b.get('table_title') or '')[:50]
        rows = ROW_RE.findall(html)
        col_counts = [effective_cols(r) for r in rows]
        max_cols = max(col_counts) if col_counts else 0
        has_cs = 'colspan' in html
        has_rs = 'rowspan' in html
        flags = ('CS ' if has_cs else '') + ('RS ' if has_rs else '')
        double = '2xTBODY ' if '</tbody><tbody>' in html else ''
        output.append(
            f'ch{ci} b{bi} p{pg} | rows={len(rows)} cols={max_cols} | '
            f'{flags}{double}| {title}'
        )

print('\n'.join(output))
PYEOF
```

---

## 2. Known Structural Patterns

### Pattern T1: Double `<tbody>` (cross-page continuation)

**Cause**: The VLM extracts a table that spans two pages as two separate units. The assembler
concatenates them into a single Table block by appending the continuation `<tbody>` after the
main `</tbody>`. This produces `</tbody><tbody>` in the HTML.

**Symptom**: Section A of the audit shows many tables with double-tbody.

**Is it a bug?** Usually no — it is a pagination artifact. The data is complete; the HTML is just
non-standard. A single `<tbody>` is cleaner.

**Fix** (bulk, safe to apply to all):
```python
b['html'] = b['html'].replace('</tbody><tbody>', '')
```

**When it IS a bug** → see Pattern T2 below.

---

### Pattern T2: Split data row across two `<tbody>` sections

**Cause**: A single logical data row was split mid-row by the PDF page break. The VLM extracted
the first half of the row (with truncated cell content) as the last row of body1, and the second
half as the first row of body2. Cells in body2's first row are mostly empty except for the
continuation text.

**Symptom**: Section B of the audit reports a body2 first row with ≥50% empty cells AND cell
content in body1's last row is clearly truncated (e.g., `"团县委副"` instead of `"团县委副书记"`).

**Example** (表6-5, 高玉溪):
```
Body1 last row:  ['高玉溪', '1994.8-', '31-33', '团县委副', '李庄镇镇', '民政局局', '...时']
Body2 first row: [''      , '1996.8' , ''     , '书记'    , '长'       , '长'      , '...局长']
→ Merge: ['高玉溪', '1994.8-1996.8', '31-33', '团县委副书记', '李庄镇镇长', '民政局局长', '...']
```

**Fix**:
```python
import re
ROW_RE = re.compile(r'<tr>(.*?)</tr>', re.DOTALL)
TD_RE  = re.compile(r'<td>(.*?)</td>', re.DOTALL)

parts = html.split('</tbody><tbody>')
body1_rows = ROW_RE.findall(parts[0].split('<tbody>')[-1])
body2_rows = ROW_RE.findall(parts[1].rstrip('</tbody></table>'))

cells1 = TD_RE.findall(body1_rows[-1])   # last row of body1
cells2 = TD_RE.findall(body2_rows[0])    # first row of body2
merged_cells = [(c1.strip() + c2.strip()).strip() for c1, c2 in zip(cells1, cells2)]
merged_row = '<tr>' + ''.join(f'<td>{c}</td>' for c in merged_cells) + '</tr>'

# Rebuild: thead + single tbody with all good rows + merged row + rest
thead = re.match(r'(<table><thead>.*?</thead>)', html, re.DOTALL).group(1)
good_rows1 = ['<tr>' + r + '</tr>' for r in body1_rows[:-1]]
good_rows2 = ['<tr>' + r + '</tr>' for r in body2_rows[1:]]
b['html'] = thead + '<tbody>' + ''.join(good_rows1 + [merged_row] + good_rows2) + '</tbody></table>'
```

**How to distinguish a split row from a legitimate empty-data row**:
- **Split row**: cells contain partial words or truncated dates (e.g., `"1994.8-"` without end date).
  Concatenating body1 and body2 cells produces coherent full values.
- **Empty data**: the category genuinely has no entries in that column. Cross-check with the book
  text to confirm.

---

### Pattern T3: Header column count mismatch with summary rows

**Cause**: A table has data rows with N columns and summary rows (e.g., 总数, 百分比) that
use a row-label in the first cell plus N data cells = N+1 total. The header was extracted without
the row-label column, so it only specifies N columns, causing the summary rows to overflow.

**Example** (表6-1):
- Header: `县领导(cs=4) | 乡镇领导(cs=2) | 县直 | 垂直` = **8** effective columns
- Data row: 8 cells ✓
- 总数 row: `总数 | 7(cs=4) | 8(cs=2) | 24 | 2` = **9** effective columns ✗

**Fix**: Add an empty `<th rowspan="2"></th>` as the first cell in the first header row, making
the header 9 columns wide. Add an empty `<td></td>` to the raw data row(s) that lack a label:

```python
# Before:
# <tr><th colspan="4">县领导...
# After:
# <tr><th rowspan="2"></th><th colspan="4">县领导...
```

**Detection**: Section C of the audit (column-count inconsistency) will flag this because
the summary rows report more effective columns than other rows.

---

### Pattern T4: `rowspan` continuation rows misread as short rows

**Cause**: A table uses `rowspan="N"` on the first cell of a group. The subsequent N-1 rows only
contain the non-spanned columns (e.g., just the last column "轨迹"). The column-count scanner
flags these as "suspicious short rows."

**This is NOT a bug** — it is correct HTML representing grouped data. The `rowspan` cell from
the first row of the group extends to cover the missing column in continuation rows.

**How to distinguish from a real issue**:
- If the short row immediately follows a row with `rowspan="N"`, count effective columns including
  the inherited rowspan cell. If it equals the table max, it is correct.
- If there is no rowspan parent row, the short row is a genuine data-loss issue.

---

## 3. Bulk Fix Script

To merge all double-tbody tables in one pass (safe — no data change, purely structural):

```python
import json
from pathlib import Path

path = Path('work/zxgb/07_footnote_verified.json')
data = json.loads(path.read_text())

fixed = 0
for ch in data['chapters']:
    for b in ch['blocks']:
        if b.get('kind') == 'table' and '</tbody><tbody>' in b.get('html',''):
            b['html'] = b['html'].replace('</tbody><tbody>', '')
            fixed += 1

path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
print(f'Fixed {fixed} tables')
```

---

## 4. Loop-Until-Clean Protocol

After applying fixes, re-run the audit script and verify:

1. Section A (double-tbody) → should be empty after the bulk fix
2. Section B (split rows) → investigate each candidate; fix or confirm as legitimate empty data
3. Section C (column mismatch) → fix header structure or confirm as rowspan false-positive

Rebuild the EPUB after all fixes:

```bash
uv run epubforge build fixtures/zxgb.pdf --force-rerun
```
