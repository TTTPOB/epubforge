# epubforge — Usage Guide

## Prerequisites

Install dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

Configure API keys (see [Configuration](#configuration) below).

---

## Quick Start

Convert a PDF to EPUB in one command:

```bash
uv run epubforge run fixtures/mybook.pdf
```

Output is written to `out/mybook.epub`. Intermediate files land in `work/mybook/`.

---

## Pipeline Overview

The pipeline runs seven stages in sequence:

| Stage | Command | Output |
|-------|---------|--------|
| 1 — Parse | `epubforge parse` | `work/<name>/01_raw.json` |
| 2 — Classify | `epubforge classify` | `work/<name>/02_pages.json` |
| 3 — Extract | `epubforge extract` | `work/<name>/03_extract/unit_*.json` |
| 4 — Assemble | `epubforge assemble` | `work/<name>/05_semantic_raw.json` |
| 5 — Refine TOC | `epubforge refine-toc` | `work/<name>/05_semantic.json` |
| 6 — Proofread | `epubforge proofread` | `work/<name>/06_proofread.json` |
| 7 — Build | `epubforge build` | `out/<name>.epub` |

Stage 3 (extract) is the expensive step — it calls the VLM for every page. All other stages are cheap and can be re-run freely.

---

## Running Individual Stages

Each stage can be run in isolation. Stages skip if their output already exists — pass `--force-rerun` (or `-f`) to override.

```bash
uv run epubforge parse   fixtures/mybook.pdf
uv run epubforge classify fixtures/mybook.pdf
uv run epubforge extract  fixtures/mybook.pdf
uv run epubforge assemble work/mybook
uv run epubforge refine-toc work/mybook
uv run epubforge proofread  work/mybook
uv run epubforge build   fixtures/mybook.pdf
```

Note: `parse`, `classify`, `extract`, `build` take the PDF path; `assemble`, `refine-toc`, `proofread` take the work directory.

---

## Re-running from a Specific Stage

Use `epubforge run --from N` to skip stages 1 through N−1:

```bash
# Re-run from stage 4 (assemble) onwards, forcing re-run of all stages >= 4
uv run epubforge run fixtures/mybook.pdf --from 4 --force-rerun

# Re-run only proofread and build (stages 6-7)
uv run epubforge run fixtures/mybook.pdf --from 6 --force-rerun
```

Without `--force-rerun`, stages whose outputs already exist are still skipped even when `--from` includes them.

---

## Resuming a Partial Extract

Stage 3 is resumable: unit files that already exist are reused automatically. If a run was interrupted partway through, just re-run extract:

```bash
uv run epubforge extract fixtures/mybook.pdf
```

It will pick up where it left off.

### Re-extracting Specific Pages

If you need specific pages re-extracted (e.g., after fixing a grouping bug), delete the corresponding unit files and re-run extract:

```bash
# Find which unit files cover pages 45-50
python3 -c "
import json
from pathlib import Path
for u in sorted(Path('work/mybook/03_extract').glob('unit_*.json')):
    d = json.loads(u.read_text())
    if any(45 <= p <= 50 for p in d['unit']['pages']):
        print(u.name, d['unit']['pages'])
"

# Delete those unit files
rm work/mybook/03_extract/unit_XXXX.json ...

# Re-run extract (reuses all other units, re-runs deleted ones)
uv run epubforge extract fixtures/mybook.pdf

# Then re-run downstream stages
uv run epubforge assemble work/mybook --force-rerun
uv run epubforge refine-toc work/mybook --force-rerun
uv run epubforge proofread work/mybook --force-rerun
uv run epubforge build fixtures/mybook.pdf --force-rerun
```

> **Important**: always delete unit files using the full-book page numbering — do not use `--pages` for this, because `--pages` renumbers the unit index from 0 and will conflict with existing unit files.

---

## Work Directory Layout

```
work/
└── mybook/
    ├── 01_raw.json            # Docling parse output
    ├── 02_pages.json          # Per-page classification (simple/complex/toc)
    ├── 03_extract/
    │   ├── unit_0000.json     # VLM extract output, one file per unit (batch of pages)
    │   ├── unit_0001.json
    │   ├── ...
    │   ├── book_memory.json   # Rolling per-book facts (footnote symbols, etc.)
    │   └── audit_notes.json   # VLM-flagged suspicious items
    ├── 05_semantic_raw.json   # Assembled Semantic IR (pre-proofread)
    ├── 05_semantic.json       # After TOC refinement
    ├── 06_proofread.json      # After proofreading
    ├── images/                # Extracted page images (figures)
    ├── style_registry.json    # Block style registry (created by proofread)
    └── logs/
        └── run-<timestamp>.log
out/
└── mybook.epub
```

---

## Configuration

Settings are loaded in this priority order (highest wins):

1. Environment variables
2. `config.local.toml` (git-ignored, for personal secrets)
3. `config.toml` (committed defaults)
4. Built-in defaults

### Minimal config.toml

```toml
[llm]
api_key = "sk-or-..."        # OpenRouter key (used for LLM stages)
model   = "anthropic/claude-haiku-4.5"

[vlm]
# api_key and base_url default to [llm] values if omitted
model = "google/gemini-2.5-flash-preview"
```

### Full reference

```toml
[llm]
base_url        = "https://openrouter.ai/api/v1"
api_key         = "sk-or-..."
model           = "anthropic/claude-haiku-4.5"
timeout_seconds = 300
max_tokens      = 8192        # optional hard cap
prompt_caching  = true        # Anthropic cache_control headers

[vlm]
base_url        = "https://openrouter.ai/api/v1"  # falls back to [llm]
api_key         = "sk-or-..."                      # falls back to [llm]
model           = "google/gemini-2.5-flash-preview"
timeout_seconds = 300
max_tokens      = 8192
prompt_caching  = true

[runtime]
concurrency = 4               # parallel VLM requests during extract
cache_dir   = "work/.cache"   # disk cache for LLM/VLM responses
work_dir    = "work"
out_dir     = "out"

[extract]
vlm_dpi                = 200  # PDF render DPI (200 recommended for footnote readability)
max_simple_batch_pages = 8    # max pages per simple-page VLM batch
max_complex_batch_pages = 12  # max pages per complex-page VLM batch
enable_book_memory     = true # rolling per-book facts injected into VLM context

[proofread]
phase1_thinking_budget_tokens = 2000
phase2_thinking_budget_tokens = 2000
max_chunk_tokens              = 100000
```

### Environment variables

All config keys are available as env vars (highest priority):

```bash
EPUBFORGE_LLM_API_KEY=sk-or-...
EPUBFORGE_LLM_MODEL=anthropic/claude-haiku-4.5
EPUBFORGE_VLM_MODEL=google/gemini-2.5-flash-preview
EPUBFORGE_VLM_DPI=200
EPUBFORGE_CONCURRENCY=4
EPUBFORGE_ENABLE_BOOK_MEMORY=true
EPUBFORGE_MAX_SIMPLE_BATCH_PAGES=8
EPUBFORGE_MAX_COMPLEX_BATCH_PAGES=12
EPUBFORGE_LOG_LEVEL=INFO      # DEBUG / INFO / WARNING
```

---

## Logging

Logs are written to `work/<name>/logs/run-<timestamp>.log` and also printed to stdout. Adjust verbosity:

```bash
# Verbose (shows VLM prompts and responses)
uv run epubforge -L DEBUG run fixtures/mybook.pdf

# Quiet
uv run epubforge -L WARNING run fixtures/mybook.pdf
```

---

## Common Workflows

### First run on a new book

```bash
uv run epubforge run fixtures/mybook.pdf
```

### Re-run only proofread (after tuning prompts)

```bash
uv run epubforge proofread work/mybook --force-rerun
uv run epubforge build fixtures/mybook.pdf --force-rerun
```

### Re-run assemble through EPUB after fixing assembler code

```bash
uv run epubforge run fixtures/mybook.pdf --from 4 --force-rerun
```

### Extend an existing extract run (book was truncated at page N, now run full book)

```bash
# Find and delete unit files for the last few pages (they need regrouping in full-book context)
python3 -c "
import json
from pathlib import Path
for u in sorted(Path('work/mybook/03_extract').glob('unit_*.json')):
    d = json.loads(u.read_text())
    if any(p >= 45 for p in d['unit']['pages']):  # adjust threshold
        print(u)
" | xargs rm

# Resume extract (pages 1–44 reused, 45–end re-run with correct grouping)
uv run epubforge extract fixtures/mybook.pdf

# Rebuild downstream
uv run epubforge run fixtures/mybook.pdf --from 4 --force-rerun
```

### Disable book memory (for debugging or cost reduction)

```bash
EPUBFORGE_ENABLE_BOOK_MEMORY=false uv run epubforge extract fixtures/mybook.pdf --force-rerun
```
