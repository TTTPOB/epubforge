# Footnote Audit Process — Standard Operating Procedure

This document describes how to run the footnote audit for `work/zxgb/07_footnote_verified.json`,
interpret the results, manually fix issues in the JSON, and re-run downstream stages.

---

## 1. How to Run the Audit Script

From the project root (`/home/tpob/playground/epubforge`):

```bash
uv run python3 - <<'PYEOF' > work/zxgb/footnote_audit_report.txt 2>/dev/null
from pathlib import Path
from epubforge.ir.semantic import Book, Footnote, Paragraph, Table
import re

book = Book.model_validate_json(Path('work/zxgb/07_footnote_verified.json').read_text())

MARKER_RE = re.compile(r'\x02fn-(\d+)-([^\x03]+)\x03')

def get_text(block):
    if isinstance(block, Paragraph):
        return block.text or ''
    elif isinstance(block, Table):
        return block.html or ''
    return ''

def get_page(block):
    if hasattr(block, 'provenance') and block.provenance:
        return block.provenance.page
    return None

output = []

# --- Step 1: Duplicate marker scan ---
output.append('=== DUPLICATE MARKERS ===')
dup_found = False
for ci, ch in enumerate(book.chapters):
    marker_map = {}
    for bi, b in enumerate(ch.blocks):
        txt = get_text(b)
        for m in MARKER_RE.finditer(txt):
            key = m.group(0)
            marker_map.setdefault(key, []).append((bi, b))
    for key, blist in marker_map.items():
        if len(blist) > 1:
            pg = get_page(blist[0][1])
            callout = MARKER_RE.match(key).group(2)
            marker_display = f'fn-{MARKER_RE.match(key).group(1)}-{callout}'
            output.append(
                f'ch{ci} b{blist[0][0]} p{pg}: marker {marker_display} appears also in '
                + ', '.join(f'b{x[0]}' for x in blist[1:])
            )
            for idx, (bi2, b2) in enumerate(blist):
                txt2 = get_text(b2)
                mobj = MARKER_RE.search(txt2, txt2.find(key))
                if mobj:
                    pos = mobj.start()
                    start = max(0, pos - 80)
                    end = min(len(txt2), pos + len(key) + 80)
                    ctx = txt2[start:end].replace(key, f'[*{callout}]')
                    ctx = MARKER_RE.sub(lambda mm: f'[*{mm.group(2)}]', ctx)
                    ctx = re.sub(r'<[^>]+>', '', ctx)
                    ctx = re.sub(r'\s+', ' ', ctx).strip()
                    pg2 = get_page(b2)
                    output.append(f'  occurrence {idx+1} b{bi2} p{pg2}: ...{ctx}...')
            output.append('')
            dup_found = True
if not dup_found:
    output.append('(none found)')
output.append('')

# --- Step 2: Unpaired / orphan FN scan ---
output.append('=== UNPAIRED FNs (not orphan) ===')
any_unpaired = False
for ci, ch in enumerate(book.chapters):
    for bi, b in enumerate(ch.blocks):
        if isinstance(b, Footnote) and not b.paired and not b.orphan:
            pg = b.provenance.page if b.provenance else '?'
            txt = (b.text or '')[:60]
            output.append(f'ch{ci} b{bi} p{pg} [{b.callout}]: {txt}')
            any_unpaired = True
if not any_unpaired:
    output.append('(none found)')
output.append('')

# Build marker -> source block lookup
marker_to_blocks = {}
for ci, ch in enumerate(book.chapters):
    for bi, b in enumerate(ch.blocks):
        txt = get_text(b)
        for m in MARKER_RE.finditer(txt):
            key = m.group(0)
            marker_to_blocks.setdefault(key, []).append((ci, bi, b, int(m.group(1)), m.group(2)))

# --- Step 3: Paired FN context ---
output.append('=== PAIRED FN CONTEXT (all, for manual review) ===')
large_page_cases = []
for ci, ch in enumerate(book.chapters):
    for bi, b in enumerate(ch.blocks):
        if not isinstance(b, Footnote) or not b.paired:
            continue
        fn_page = b.provenance.page if b.provenance else None
        callout = b.callout
        fn_text = (b.text or '')[:80]
        marker = f'\x02fn-{fn_page}-{callout}\x03'
        src_entries = marker_to_blocks.get(marker, [])
        if not src_entries:
            output.append(f'[ch{ci} b{bi}] p{fn_page} [{callout}] -> NO SOURCE BLOCK FOUND (marker missing)')
            output.append(f'  FN:  {fn_text}')
            output.append('')
            continue
        for sci, sbi, sb, m_pg, m_callout in src_entries:
            s_page = get_page(sb)
            raw_txt = get_text(sb)
            pos = raw_txt.find(marker)
            if pos == -1:
                continue
            start = max(0, pos - 120)
            end = min(len(raw_txt), pos + len(marker) + 120)
            ctx_raw = raw_txt[start:end]
            ctx_display = ctx_raw.replace(marker, f'[*{callout}]')
            ctx_display = MARKER_RE.sub(lambda mm: f'[*{mm.group(2)}]', ctx_display)
            ctx_display = re.sub(r'<[^>]+>', '', ctx_display)
            ctx_display = re.sub(r'\s+', ' ', ctx_display).strip()
            output.append(f'[ch{ci} b{bi}] p{fn_page} [{callout}] -> source b{sbi} (ch{sci}) p{s_page}')
            output.append(f'  FN:  {fn_text}')
            output.append(f'  CTX: {ctx_display}')
            output.append('')
            if fn_page is not None and s_page is not None:
                dist = abs(fn_page - s_page)
                if dist > 3:
                    large_page_cases.append((ci, bi, fn_page, callout, sci, sbi, s_page, fn_text, ctx_display, dist))

# --- Step 4: Large page distance ---
output.append('')
output.append('=== LARGE PAGE DISTANCE PAIRINGS (>3 pages) ===')
if not large_page_cases:
    output.append('(none found)')
else:
    for ci, bi, fn_page, callout, sci, sbi, s_page, fn_text, ctx_display, dist in large_page_cases:
        output.append(f'[ch{ci} b{bi}] p{fn_page} [{callout}] -> source b{sbi} (ch{sci}) p{s_page} | dist={dist}')
        output.append(f'  FN:  {fn_text}')
        output.append(f'  CTX: {ctx_display}')
        output.append('')

print('\n'.join(output))
PYEOF
```

This overwrites `work/zxgb/footnote_audit_report.txt` with a fresh run. The script does not modify
any data — it is read-only.

---

## 2. How to Interpret Each Section

### 2.1 `=== DUPLICATE MARKERS ===`

A `\x02fn-PAGE-CALLOUT\x03` marker string appears in more than one block within the same chapter.
This is an assembler bug: the pairing code inserted the marker into the wrong block in addition to
(or instead of) the correct block.

Each entry shows:
- Which chapter and block index holds the first occurrence.
- All additional blocks that also carry the same marker.
- ±80 chars of context around the marker in each occurrence (HTML tags stripped).

**Action required**: determine which occurrence is correct (check context semantics vs. FN body),
then manually remove the duplicate marker(s) from the wrong block(s).

### 2.2 `=== UNPAIRED FNs (not orphan) ===`

Footnote blocks where `paired=False` and `orphan=False`. These are genuine misses: the assembler
failed to find a callout symbol in any source block, and the FN was not flagged as an orphan by
the proofreader.

Each entry shows: chapter, block index, page, callout symbol, first 60 chars of FN text.

**Action required**: locate the correct source paragraph (search for the raw callout symbol on the
same page) and manually insert the marker + set `paired=True`.

### 2.3 `=== PAIRED FN CONTEXT (all, for manual review) ===`

All 146 paired footnotes listed with:
- `[ch{N} b{M}]` — FN block location in the IR
- `p{PAGE} [{CALLOUT}]` — FN page and callout symbol
- `source b{K} (ch{N}) p{SPAGE}` — the block that received the marker
- `FN:` — first 80 chars of the footnote body text
- `CTX:` — ±120 chars around the `[★CALLOUT]` marker in the source block

Use this section for semantic verification. Read the CTX and confirm the FN body makes sense as a
note on that passage. Typical correct pairs have the FN body elaborating on a term, name, citation,
or statistic that appears immediately before or after the `[★CALLOUT]` token.

Suspicious signs:
- The callout appears in a table cell with no semantic relation to the FN body.
- The callout is in a header row or figure caption, but the FN body refers to body text.
- The FN body is about a completely different topic than the surrounding context.

### 2.4 `=== LARGE PAGE DISTANCE PAIRINGS (>3 pages) ===`

Paired FNs where `|fn_page - source_block_page| > 3`. These are not necessarily wrong — they can
arise from legitimate cross-chapter same-page layouts — but they warrant extra scrutiny.

In the current run of `zxgb`, no such cases were found.

---

## 3. How to Manually Fix Issues in the JSON

`07_footnote_verified.json` is a serialized `Book` Pydantic model. The simplest approach is to
edit it as JSON using a text editor or `jq`. All fixes must preserve valid JSON.

### 3.1 Removing a duplicate marker

Find the wrong block's `"text"` or `"html"` field and remove the `\u0002fn-PAGE-CALLOUT\u0003`
substring. The `\x02` byte is JSON-encoded as `\u0002`; `\x03` as `\u0003`.

Example (using Python for safety):

```python
from pathlib import Path
import json

data = json.loads(Path('work/zxgb/07_footnote_verified.json').read_text())

# Navigate to the wrong block and strip the duplicate marker
ch = data['chapters'][10]   # chapter index
block = ch['blocks'][109]   # block index
marker = '\u0002fn-34-\u2460\u0003'   # \u2460 = circled 1 ①
block['text'] = block['text'].replace(marker, '')

Path('work/zxgb/07_footnote_verified.json').write_text(
    json.dumps(data, ensure_ascii=False, indent=2)
)
```

### 3.2 Inserting a missing marker for an unpaired FN

Find the source paragraph on the FN's page that contains the raw callout symbol (e.g., `①`).
Insert the marker at the correct position and set `paired=True` on the Footnote block:

```python
from pathlib import Path
import json

data = json.loads(Path('work/zxgb/07_footnote_verified.json').read_text())

# Fix the FN block
fn_block = data['chapters'][12]['blocks'][107]
fn_block['paired'] = True

# Fix the source paragraph: replace raw callout with marker
src_block = data['chapters'][12]['blocks'][105]   # example
src_block['text'] = src_block['text'].replace('①', '\u0002fn-84-\u2460\u0003', 1)

Path('work/zxgb/07_footnote_verified.json').write_text(
    json.dumps(data, ensure_ascii=False, indent=2)
)
```

Note: circled numbers are Unicode characters (`①` = U+2460, `②` = U+2461, … `⑨` = U+2468).

### 3.3 Fixing a semantic mismatch (wrong-block pairing)

A semantic mismatch means the marker ended up in the wrong source block. The fix requires two steps:

1. Remove the marker from the wrong source block (see 3.1).
2. Insert the marker into the correct source block at the correct callout position (see 3.2).

The Footnote block itself does not need to change — `paired=True` stays because the FN is correctly
paired after the fix; only the source block changes.

---

## 4. How to Re-run Stage 7 After Manual Fixes

Stage 7 (`build`) reads `06_proofread.json` and produces the EPUB. The footnote pairing step is
actually embedded inside the assembler (stage 4) and the `verify` sub-step that produces
`07_footnote_verified.json`. After manually patching `07_footnote_verified.json`, you do NOT need
to re-run stage 4 — the EPUB builder reads `07_footnote_verified.json` directly if it exists.

Rebuild the EPUB:

```bash
uv run epubforge run work/zxgb --from 7 --book zxgb
# or, using the main.py entry point:
uv run python3 main.py run work/zxgb --from 7
```

If you need to re-run the full footnote verification step from scratch (discarding manual edits):

```bash
# Delete the verified file and re-run from stage 4
rm work/zxgb/07_footnote_verified.json
uv run epubforge run work/zxgb --from 4 --book zxgb
```

To re-run only the verification pass (stage 4's pairing logic) without re-extracting blocks,
use `--force-rerun` if the assembler supports it, or delete `05_semantic_raw.json` selectively.

---

## 5. Known Assembler Bug Patterns

The pairing algorithm in `src/epubforge/assembler.py` uses a four-priority stack (P0–P3).
The following structural patterns are known to cause incorrect pairings:

### Pattern A: Duplicate marker from same-page multi-entry not retired

**Trigger**: A cross-page (`cross_page=True`) paragraph that spans page N and page N+1 appears as
a P2 candidate (same-page, multi) for a FN on page N. If the assembler then also finds a regular
P3 source on page N, it inserts the marker into the P3 source and "retires" the P2 stale entry.
However, if the retirement logic fires too late (race between adjacent blocks), the P2 entry may
also have received the marker in a prior iteration, producing a duplicate.

**Symptom**: Same `fn-PAGE-①` marker in two consecutive blocks (e.g., `b107` and `b109`).

**Fix**: Remove the marker from the earlier (cross-page) block; keep it in the regular paragraph.

### Pattern B: Cross-chapter same-page FN pairing

**Trigger**: A chapter heading lands on the same physical page as a preceding chapter's last
footnote. The FN body is on page N; the heading starts on page N; the source paragraph that
contains the callout is at the end of the prior chapter, also on page N.

**Symptom**: A paired FN whose source block is in a different chapter than the FN block itself,
but on the same page.

**Fix**: Usually correct — verify semantics. If wrong, remove the cross-chapter marker and insert
the marker in the correct same-chapter block.

### Pattern C: P0 distance limit fallback

**Trigger**: The assembler falls back to P0 (layout anomaly) when no P1/P2/P3 candidate is found.
P0 is restricted to adjacent pages (`fn_page - src_page == 1`) but can still produce a wrong pair
if the callout symbol reappears in an unrelated paragraph nearby.

**Symptom**: A paired FN where the CTX context is unrelated to the FN body text. The source block
is one page earlier than the FN.

**Fix**: Remove the wrong marker, mark the FN as `paired=False`, and insert the correct marker
manually (or add the FN to the unpaired list for the proofreader to handle).

### Pattern D: Salvage pass false positive

**Trigger**: The salvage pass scans raw callout symbols (un-marked occurrences of `①`, `②`, etc.)
in paragraphs near an already-paired FN and inserts the same marker a second time. This can happen
when the same callout symbol appears in a table header and a body paragraph on the same page.

**Symptom**: Duplicate marker, one occurrence in a Table block and one in a Paragraph block.

**Fix**: Identify which occurrence is the true callout position (usually the prose paragraph, not
the table header). Remove the marker from the table HTML.

### Pattern E: Marker missing despite `paired=True`

**Trigger**: The assembler sets `paired=True` on the Footnote but the source block's text was
later modified (e.g., by the proofread stage) in a way that deleted the marker. Or the FN's page
number differs from the block's actual page in the IR.

**Symptom**: `[ch{N} b{M}] pPAGE [CALLOUT] -> NO SOURCE BLOCK FOUND (marker missing)` in the
paired FN context section.

**Fix**: Treat as an unpaired FN (section 3.2) and re-insert the marker manually.
