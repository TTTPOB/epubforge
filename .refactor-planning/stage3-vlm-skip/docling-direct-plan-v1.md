# Docling-Direct Extraction Plan — v1

## User Context & Direction
- Docling OCR/structure extraction is good enough for most books, including footnotes
- VLM should become an optional tool, not mandatory for extraction
- The agentic workflow (editor) can fix remaining issues iteratively
- VLM can be called selectively later when the editor detects problems

## Goal
Add a "docling-direct" extraction path that converts docling's `DoclingDocument` directly into unit JSON files, completely bypassing VLM API calls in Stage 3.

## Architecture Decisions

1. **New module**: `src/epubforge/extract_docling.py` (~200-250 lines)
   - Self-contained, does NOT modify `extract.py`
   - Reads `01_raw.json` + `02_pages.json`, writes `03_extract/unit_XXXX.json`
   - Same output schema as VLM path — assembler works identically

2. **Config/CLI**: New `extract_mode` setting
   - Values: `"vlm"` (default, backward compat) or `"docling"`
   - Config: `[extract] extract_mode = "docling"`
   - CLI: `--extract-mode=docling` on `run` and `extract` commands
   - Env: `EPUBFORGE_EXTRACT_MODE=docling`
   - When `extract_mode=docling`, skip `require_vlm()` check

3. **Routing**: `pipeline.run_extract()` dispatches based on `cfg.extract.extract_mode`

## File Changes

### NEW: `src/epubforge/extract_docling.py`

Main function signature (mirrors extract.extract):
```python
def extract_docling(
    pdf_path: Path,
    raw_path: Path,
    pages_path: Path,
    out_dir: Path,
    cfg: Config,
    *,
    force: bool = False,
    page_filter: set[int] | None = None,
) -> None:
```

Internal functions:
- `_convert_page_items(doc, page_no) -> list[dict]` — converts docling items on one page to block dicts
- `_map_item_to_block(item, doc, page_no) -> dict | None` — single item to block
- `_heading_level(item) -> int` — heading level heuristic
- `_extract_callout(text) -> tuple[str, str]` — extract footnote callout from text
- `_ends_mid_sentence(text) -> bool` — cross-page continuation detection
- `_write_unit(out_path, unit, blocks, flag, fn_flag)` — write unit JSON

### Label-to-Block Mapping

| DocItemLabel | Block kind | Notes |
|---|---|---|
| TITLE | heading (level=1) | Always h1 |
| SECTION_HEADER | heading | Use docling's `SectionHeaderItem.level`, clamp 1-6 |
| TEXT | paragraph | Standard body text |
| FOOTNOTE | footnote | Extract callout via regex |
| LIST_ITEM | paragraph | Prefix with marker text |
| TABLE | table | Use `TableItem.export_to_html(doc)` |
| PICTURE | figure | Set image_ref from saved crop path |
| FORMULA | equation | Text to `latex` field |
| CAPTION | (skip — attached to parent) | |
| PAGE_HEADER/PAGE_FOOTER | (skip) | Running headers/footers are noise |
| DOCUMENT_INDEX | (skip) | TOC pages already filtered |

### Unit Batching: One Page Per Unit
- Simple, no API cost concern
- Continuation flags compare adjacent units
- TOC pages skipped (matching current behavior)

### Heading Level Heuristic
- Trust docling's `SectionHeaderItem.level`, clamp to 1-6
- TITLE label → always level 1
- Fallback for SECTION_HEADER without level → default 2

### Cross-Page Continuation
Paragraph (`first_block_continues_prev_tail`):
- Previous page's last non-footnote block ends mid-sentence (no terminal punct)
- AND current page's first block is a paragraph (not heading/table/figure)

Footnote (`first_footnote_continues_prev_footnote`):
- Previous page's last footnote ends mid-sentence
- AND current page's first footnote has no detectable callout

### Callout Extraction
```python
_CALLOUT_RE = re.compile(r'^(\d+|[①-⑳]|[*†‡§‖¶#]|[a-z])\s+')
```

### MODIFY: `src/epubforge/config.py`
- Add `extract_mode: Literal["vlm", "docling"] = "vlm"` to `ExtractSettings`
- Add env var mapping: `("EPUBFORGE_EXTRACT_MODE", "extract", "extract_mode", str)`

### MODIFY: `src/epubforge/pipeline.py`
- `run_extract()`: dispatch based on `cfg.extract.extract_mode`

### MODIFY: `src/epubforge/cli.py`
- Add `--extract-mode` option to `run` and `extract` commands
- Skip `require_vlm()` when mode is `"docling"`

### NO CHANGES to:
- `assembler.py` — unit files are schema-identical
- `extract.py` — VLM path untouched
- `ir/semantic.py` — IR models untouched

## Decisions (no user input needed)

1. **Default mode stays "vlm"** — docling-direct is opt-in, avoids breaking existing workflows
2. **One page per unit** — simplest, no downsides for docling-direct
3. **Trust docling heading levels** — avoid over-engineering font-size heuristics
4. **No cross-page table continuation** — simple books don't need it; editor can fix later
5. **No BookMemory** — it's a VLM feedback mechanism
6. **No audit_notes** — always empty for docling-direct (no VLM analysis)
7. **Picture image_ref** — derive from docling's saved crop paths in images/ directory

## Limitations (acceptable for simple books)
- Multi-column reading order may be wrong (docling handles this but not perfectly)
- Cross-page table continuation not detected (flag always False)
- Nested list depth lost (flat list items as paragraphs)
- No visual-context audit notes

## Test Plan
- NEW: `tests/test_extract_docling.py` — unit tests for mapping, heading heuristic, continuation
- Integration: run full pipeline with `--extract-mode=docling` on a test PDF
