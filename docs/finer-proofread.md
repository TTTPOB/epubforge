# Finer Proofread — Footnote Pairing Error Patterns

This document catalogues the footnote pairing failure modes discovered during analysis of `zxgb` and the fixes (algorithmic or prompt-level) applied or planned.

---

## Pattern 1 — VLM Heading Level Misclassification Clears LIFO Stack

**Symptom**: A footnote body is marked ORPHANED (not paired), even though its callout appears clearly in a paragraph or table earlier on the same page.

**Root cause**: The `_pair_footnotes` forward scan clears all per-callout stacks whenever it encounters a level-1 heading (`Heading.level == 1`). If the VLM incorrectly classifies a subsection heading as H1 (e.g., `五、单位人员构成` → H1 instead of H2), the stack is wiped mid-page and the callout registered before that heading is lost.

`refine-toc` later demotes the heading to the correct level, but `_pair_footnotes` had already run during `assemble` — the pairing result was baked into the semantic IR.

**Fix (implemented)**: Made `_pair_footnotes` idempotent (marker-aware helpers `_has_raw_callout`, `_replace_first_raw`). Called it again inside `toc_refiner.refine_toc()` after heading levels are corrected, so the second pass can pair the previously orphaned footnotes.

**Evidence**: 4 cases fixed (p63, p74, p81, p161 in `zxgb`). Source: `unit_0039.json` contained `五、单位人员构成` as `level: 1`.

---

## Pattern 2 — False-Positive `first_footnote_continues_prev_footnote`

**Symptom**: The first footnote body of a unit is erroneously merged into the last footnote body of the previous unit. The target callout's footnote text disappears from its correct page and instead bloats an unrelated footnote.

**Root cause**: The VLM sets `first_footnote_continues_prev_footnote: true` even when it is logically impossible. Two known impossible configurations:

1. **Different callout symbols**: Previous unit's last FN was `⑧`, current unit's first FN is `①`. Cross-unit footnote continuations only make sense for the *same* callout symbol.
2. **Previous FN ends with sentence-terminating punctuation**: A FN ending with `。` `！` `？` or `.` `!` `?` is a complete sentence — it cannot continue into the next page.

**Fix (manually + algorithmically implemented)**:

- Manually corrected 4 unit files in `work/zxgb/03_extract/`:
  - `unit_0013`: prev=`⑧`, curr=`①` → impossible by rule 1
  - `unit_0024`: prev FN semantically complete; unrelated to curr
  - `unit_0055`: prev FN ends with complete statement; merged result is two different topics
  - `unit_0065`: prev FN ends with `。`; curr FN unrelated content → rule 2

- Added `_is_continuation_plausible()` in `assembler.py`: hard rejects when both prev and cont callouts are non-empty and differ (VLM self-contradiction per prompt contract — true continuations must have `callout=""`). Semantic completeness (ending punctuation) is intentionally NOT hard-coded — left to VLM prompt judgment to avoid false negatives on abbreviation periods (e.g., "Dr.", "U.S.").

- Updated `_PENDING_FOOTNOTE_RULES` SEPARATE branch in `prompts.py` with three VLM self-check signals: callout conflict, semantic completeness (VLM judges), and topic discontinuity.

**Scope**: Only callout mismatch is a code-level hard filter. The other two are prompt-level guidance — they will take effect on the next VLM extract re-run, not on existing `03_extract/` JSON.

---

## Pattern 3 — Cross-Page Paragraph Steals Earlier-Page Footnote

**Symptom**: A footnote body on page N (e.g., FN ① p64) is paired not with the paragraph on page N, but with a `cross_page=True` paragraph whose *source* page is N-1. The original page-N paragraph's callout goes unmatched.

**Root cause**: `_pair_footnotes` treats `cross_page=True` paragraphs the same as tables — no page constraint. This is correct for tables (which can span many pages), but wrong for paragraphs. A `cross_page=True` paragraph was *started* on page P and *continued* on page P+1. The paragraph text contains content from both pages; the callout `①` in it belongs to the continuation (page P+1) portion.

When the LIFO stack sees FN(① p64), it pops the most recent `①` entry, which is the cross_page para from p63 — stealing the match before the p64 para gets a chance.

**Affected cases**: p64 (b495, `cross_page=True` paragraph p63 stole FN ① p64), p149 (b1188, `cross_page=True` paragraph p148 stole FN ① p149).

**Fix (implemented)**: Introduced 4-level priority in `_pair_footnotes` (P3 > P2 > P1 > P0). Cross-page paragraphs (`cross_page=True`) are pushed onto the stack with `multi_page=True`, giving them priority P2 (same-page) or P1 (prev-page). A regular same-page paragraph is P3 and always beats P2, so the theft is prevented. The cross-page paragraph can still pair with the *next* page FN via P1.

---

## Pattern 4 — Merged Continuation Table Steals Earlier-Page Footnote

**Symptom**: Same as Pattern 3 but for tables. A table that was merged across pages (via `_merge_continued_tables`) has `cross_page=False` and `provenance.page` = the page where the table *started*. Its HTML may contain content (and callouts) from the continuation pages. FN on page N+k is incorrectly matched with the merged table at page N.

**Root cause**: `_merge_continued_tables` absorbs continuation rows but does not flag the result as cross-page. The merged table's page = start page. LIFO stack has no page constraint for tables, so a much later FN can still match.

This is different from a legitimate 3-page table span (which should match freely): the problem occurs when the merged table has the callout from the continuation portion, and there is *also* a correct same-page match candidate that arrives in the stack later and would win if the merged table entry weren't there.

**Affected cases**: p83 (b659, merged table p83 stole FN ① p84), p119 (b942, merged table p119 stole FN ① p120).

**Fix (implemented)**: Added `Table.multi_page: bool = False` to the semantic IR. `_merge_continued_tables` sets `multi_page=True` on the resulting merged table. In `_pair_footnotes`, merged tables are pushed with `multi_page=True` → priority P2 when on the same page as a FN, losing to regular same-page paragraphs (P3). Legitimate cross-page table spans still work via P1 (prev-page multi_page).

---

## Pattern 5 — Book Layout Anomaly: Callout Precedes Footnote Body by One Page

**Symptom**: A callout `①` appears in a paragraph on page N, but the footnote body is placed at the bottom of page N+1 (the book's own layout quirk). The FN on page N+1 is unpaired because the para on page N has `cross_page=False` and `_pair_footnotes` only allows same-page or cross-page matching.

**Root cause**: The book itself has an unusual typesetting — the footnote body was pushed to the next page due to space constraints. This is a book-level issue, not an extraction error.

**Affected case**: p89 (b708, paragraph p89 `cross_page=False`; FN ① is on p90 by the book's own layout).

**Fix (implemented via P0 fallback)**: The 4-level priority system includes P0 — prev-page, not multi_page. When no P3/P2/P1 candidate exists, a regular paragraph from the previous page is matched as a last resort. For p89, the `cross_page=False` paragraph on p89 becomes the only candidate for FN ① p90, and P0 fires correctly. P0 only activates when no same-page or multi_page source is available, keeping false-positive risk low.

---

## Pattern 6 — VLM Omits Footnote Body Entirely

**Symptom**: A callout `①` appears in a paragraph, but no `Footnote` block with that callout ever appears in the IR. The callout marker is embedded in the text but the footnote detail is lost.

**Root cause**: VLM failed to extract the footnote from the page image. This can happen when:
- Footnote text is too small (< 5pt) and rendered at low DPI
- Footnote is in a visually complex region (overlapping table borders, tight margins)
- Multi-column layout confuses the VLM's reading order

**Mitigation**: Increasing `vlm_dpi` to 200 reduces but does not eliminate this. The `audit_notes` mechanism (Pattern-level hint from VLM) can flag suspected missing footnotes for manual review.

---

## Summary Table

| # | Pattern | Cases in zxgb | Status |
|---|---------|---------------|--------|
| 1 | H1 misclassification clears stack | 4 (p63, p74, p81, p161) | **Fixed** — re-run `_pair_footnotes` in `toc_refiner` |
| 2 | False-positive `first_footnote_continues_prev_footnote` | 4 (unit_0013/0024/0055/0065) | **Fixed** — hard filter (callout mismatch) + prompt update |
| 3 | Cross-page para steals earlier FN | 2 (p64, p149) | **Fixed** — 4-level priority; P3 beats P2 |
| 4 | Merged continuation table steals FN | 2 (p83, p119) | **Fixed** — `Table.multi_page=True`; P3 beats P2 |
| 5 | Book layout: callout precedes FN by 1 page | 1 (p89) | **Fixed** — P0 fallback when no same-page candidate |
| 6 | VLM omits footnote body | varies | **Mitigated** by DPI=200 + audit_notes |

Pairing rate history:
- After Pattern 1 fix: **145/154 = 94.2%**
- After Patterns 2–5 fixes (algorithmic): target **≥ 150/154 = 97.4%**

Remaining unfixed: p34 (book mis-print: two orphan ① in prose, table FN takes the ① correctly, expected behaviour).

---

## Post-Pipeline Footnote Review Checklist

After running `assemble → refine-toc → proofread`, use the following checklist to audit footnote pairing quality. This is a manual + AI-assisted step — it does **not** require re-running expensive VLM extraction.

### Step 1 — Compute pairing rate

```bash
uv run python -c "
import json
data = json.loads(open('work/zxgb/06_proofread.json').read())
fns = [b for ch in data['chapters'] for b in ch['blocks'] if b['kind']=='footnote']
paired = sum(1 for f in fns if f.get('paired'))
print(f'paired {paired}/{len(fns)} = {paired/len(fns)*100:.1f}%')
"
```

Acceptable threshold: ≥ 97%. Below 90% indicates a systemic issue (e.g., wrong callout symbols, heading-level misclassification at chapter scale).

### Step 2 — List all unpaired footnotes

```bash
uv run python -c "
import json
data = json.loads(open('work/zxgb/06_proofread.json').read())
for ci, ch in enumerate(data['chapters']):
    for bi, b in enumerate(ch['blocks']):
        if b['kind']=='footnote' and not b.get('paired'):
            print(f'ch{ci} b{bi} p{b[\"provenance\"][\"page\"]} callout={b[\"callout\"]!r} text={b[\"text\"][:60]!r}')
"
```

For each orphan: look up the page in the original PDF and classify by pattern (1–6 above).

### Step 3 — Classify each orphan and decide action

For each unpaired footnote, share the page context with Claude Code and ask:

> "This footnote (callout=`①`, page=83) is unpaired. Here is the surrounding text: `[paste paragraph text]`. Classify this as Pattern 1–6 and suggest the minimum fix."

Expected answers and actions:

| Classification | Action |
|---|---|
| Pattern 1 (H1 misclassification) | Check `06_proofread.json` for heading level; if already corrected by refine-toc, re-run `assemble --force-rerun`. |
| Pattern 2 (false continuation) | Open the relevant `03_extract/unit_NNNN.json`, set `first_footnote_continues_prev_footnote: false`, re-run `assemble --force-rerun`. |
| Pattern 3/4 (cross-page steal) | These should now be fixed algorithmically. If still occurring, check `cross_page` and `multi_page` flags on the suspect block in the IR. |
| Pattern 5 (layout anomaly) | Verify via P0 fallback. If still unpaired, the paragraph may be on a page even further back — check ±2 pages. |
| Pattern 6 (VLM omission) | Accept or manually add a footnote block to the unit JSON. |
| Book mis-print | Document as expected behaviour; accept orphan. |

### Step 4 — Verify specific problem pages

After any fix, re-run and spot-check the targeted pages:

```bash
uv run python -c "
import json
pages = [64, 83, 89, 119, 149]  # adjust to your book
data = json.loads(open('work/zxgb/06_proofread.json').read())
for ch in data['chapters']:
    for b in ch['blocks']:
        p = b['provenance']['page']
        if p in pages:
            print(f'p{p} {b[\"kind\"]:12} paired={b.get(\"paired\",\"n/a\")} text={str(b.get(\"text\") or b.get(\"html\",\"\"))[:80]!r}')
"
```

Confirm: paragraphs on those pages contain `\x02fn-...\x03` markers (not raw ① symbols).

### Step 5 — Build EPUB and visually verify

```bash
uv run epubforge build fixtures/zxgb.pdf --force-rerun
```

Open the EPUB in Calibre or iBooks. On previously-failing pages, confirm:
- The callout renders as a clickable superscript link.
- Clicking it jumps to the correct footnote in the backmatter.
- The footnote text is complete (not truncated by a false continuation merge).
