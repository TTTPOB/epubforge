# epubforge — Agent Instructions

## Project Overview

Docling-based PDF → EPUB pipeline for books and academic theses.
The system has two main subsystems: a **5-stage Docling-based ingestion pipeline** that converts a PDF
into a Semantic IR `Book`, and an **agent-driven editor subsystem** that applies structured operations
to a `Book` stored in `edit_state/`.

Each pipeline stage writes to `work/<book_name>/0N_*.json` and is independently
re-runnable. LLM/VLM calls are cached in `work/.cache/` (key = sha256 of model +
prompt + image).

## Pipeline Stages

| Stage | Name | CLI / `--from` | Input | Output |
|-------|------|----------------|-------|--------|
| 1 | parse | `parse` / `--from 1` | PDF | `01_raw.json` (Docling JSON, internally batched by `extract.page_batch_size`, default 20 pages, to bound peak memory under OCR; when `extract.segment_size` is set the parse additionally runs in subprocess-isolated segments — see below); `source/source.pdf` (hardlinked/copied); `01_raw_granite.json` + `01_raw_granite.manifest.json` (when Granite is enabled) |
| 2 | classify | `classify` / `--from 2` | `01_raw.json` | `02_pages.json` (simple/complex/toc labels) |
| 3 | extract | `extract` / `--from 3` | `01_raw.json` + `02_pages.json` + `source/source.pdf` | `03_extract/artifacts/<id>/` + `03_extract/active_manifest.json` |
| 4 | assemble | `assemble` / `--from 4` | `03_extract/active_manifest.json` | `05_semantic_raw.json` (Semantic IR) |
| 5 | build | `build` | `edit_state/book.json` or `05_semantic.json` | `out/<name>.epub` |

Stage 1 uses Docling mechanical parsing as the **primary pipeline** (source of truth for BookIR).
When `extract.granite.enabled = true`, Stage 1 also runs a **secondary Granite-Docling VLM
pipeline** via a local llama-server (OpenAI-compatible) endpoint and writes
`01_raw_granite.json` alongside `01_raw.json`. Granite failures are caught as warnings and
never abort Stage 1 — the secondary result is cross-validation evidence only, never the
BookIR source. See [Granite CLI flags](#granite-cli-flags) and
`docs/rules/ocr-cross-validation.md`.

When `extract.segment_size` is set, the PDF is parsed in N-page segments dispatched to
short-lived `python -m epubforge.parser._segment_worker` subprocesses (one per segment,
serial). Process exit is the only reliable way to release onnxruntime/torch shape-cache
mmap regions accumulated by the per-batch `convert()` loop, so segmented mode bounds peak
RSS at one segment's worth. The parent process merges segment JSONs into `01_raw.json`
via the same `_merge_batch_into` logic used by the in-process page-batch loop; the final
output is byte-equivalent to a single-process run with the same `page_batch_size`. The
secondary Granite pipeline applies the same segmentation when enabled. Backward
compatibility: `segment_size = None` (the default) preserves the single-process path.
See `docs/explorations/stage1-pdf-parser-memory.md`.

VLM analysis via the **editor evidence tools** (`vlm-page` / `vlm-range`) remains available
for on-demand page inspection independent of the Granite secondary pipeline.

Stage 3 artifacts are **manifest-addressed**: the `artifact_id` is derived from a SHA-256 of
`(source_pdf_sha256, mode, selected_pages)`. A new mode or page selection produces a new
`artifact_id`; Stage 4 detects stale output by comparing `active_manifest.json` sha with
`Book.extraction.stage3_manifest_sha256`.

**Old workdirs** (layout: `03_extract/unit_*.json` at root level, no `source/source.pdf`) are
**not migrated**. Rerun the full pipeline to generate the new format.

> CLI `--from` accepts `max=4` (build is not re-runnable via `--from`).

All stages accept `--force-rerun` (`-f`) to re-run even when output exists.

### Granite CLI flags

The `parse` subcommand has three flags that override `extract.granite.enabled` at runtime:

| Flag | Short | Effect |
|------|-------|--------|
| `--with-granite` | `-g` | Enable Granite secondary pipeline (sets `extract.granite.enabled = true`) |
| `--no-granite` | — | Force-disable Granite (overrides config `enabled = true`) |
| `--force-granite` | — | Re-run Granite even if `01_raw_granite.json` already exists; implies `--with-granite` |

`--with-granite` and `--no-granite` are mutually exclusive (raises an error if both are given).
Overrides are applied via `model_copy(update=...)` — the config file on disk is not modified.
When Granite is enabled (config or flag), progress is logged every 10 pages or on the last page.

## Observability

All stages and every LLM/VLM request emit INFO-level logs. Logs are written to
`work/<book>/logs/run-<timestamp>.log` and to stderr via RichHandler.

CLI flags: `--log-level / -L` (DEBUG/INFO/WARNING); `--log-file` (override log file path).

Per-request log lines include: kind (LLM/VLM), req_id, model, cache HIT/MISS, elapsed,
tokens, and `cached=<N>` (provider-side cached tokens). Each stage emits a summary line
on completion.

System prompts use `cache_control: ephemeral` for Anthropic prompt caching; other
providers use implicit caching. Disable per-model with `[llm] prompt_caching = false` or
`EPUBFORGE_LLM_PROMPT_CACHING=0`.

## Editor Subsystem

The editor subsystem provides agent-driven, auditable mutations to a `Book`.
All state lives under `edit_state/` inside the book's work directory.

### `edit_state/` layout

```
edit_state/
  book.json          # current Book (Semantic IR)
  edit_log.jsonl     # append-only audit log for AgentOutput submissions/staging
  memory.json        # BookMemory (rolling per-book facts)
  meta.json          # init metadata
  vlm_observation_index.json  # index of VLM observations
  audit/             # doctor report + context JSON
  agent_outputs/     # in-progress AgentOutput JSON plus archives/
  scratch/           # temporary scripts allocated by run-script
  projections/       # read-only Markdown-ish projection files for agent context
    index.md
    chapters/<chapter_uid>.md
  vlm_observations/  # VLM observation evidence files (one JSON per observation)
```

### AgentOutput / BookPatch workflow

Editor agents write structured `AgentOutput` files under `edit_state/agent_outputs/`.
An `AgentOutput` contains:
- `patches`: low-level `BookPatch` objects made of UID-addressed IR changes
- `commands`: high-level `PatchCommand` macros compiled to `BookPatch`
- `memory_patches`, `open_questions`, `notes`, and optional `evidence_refs`

`agent-output submit --apply` validates the whole output, compiles commands,
applies patches transactionally to `book.json`, applies memory patches, archives the
submitted output, and appends an audit event. `agent-output submit --stage` validates
and archives without mutating `book.json` or `memory.json`.

Full JSON schema: see `editor/agent_output.py`, `editor/patches.py`,
`editor/patch_commands.py`, `editor/memory.py`, and `editor/_validators.py`.

### `epubforge editor <cmd>` commands

The editor command surface is available via `epubforge editor <cmd>`. Each command receives
effective config from `ctx.find_root().obj.config` (injected by the root Typer callback).

| Command | Purpose |
|---------|---------|
| `init` | Initialize `edit_state/` from `05_semantic.json`; reads Stage 3 active manifest to populate `meta.json` stage3 context |
| `doctor` | Run audit detectors and print readiness report |
| `agent-output begin` | Create an in-progress AgentOutput |
| `agent-output add-patch` | Append a BookPatch from a JSON file |
| `agent-output add-command` | Append a PatchCommand from a JSON file |
| `agent-output add-memory-patch` | Append a MemoryPatch from a JSON file |
| `agent-output validate` | Validate an AgentOutput without mutation |
| `agent-output submit` | Dry-run, stage, or apply an AgentOutput |
| `run-script` | Allocate or execute scratch scripts in `edit_state/scratch/` |
| `compact` | Compact accepted edit log into an archive record |
| `render-prompt` | Render a subagent prompt with current memory and patch workflow instructions |
| `render-page` | Render a page from `source/source.pdf` to JPEG; **no LLM/VLM calls** |
| `vlm-page` | Re-analyze a selected page via VLM; writes observation to `edit_state/vlm_observations/`; never mutates `book.json` |
| `vlm-range` | Analyze a range of pages with VLM, creating one observation per page |
| `diff-books` | Diff two Book JSON files and print a schema-valid BookPatch JSON |
| `projection export` | Export book IR to Markdown-ish read-only projection files |
| `workspace create` | Create a new Git worktree for agent use |
| `workspace list` | List all Git worktrees (optionally filtered to agent/* branches) |
| `workspace merge` | Merge an agent branch and validate semantically |
| `workspace remove` | Remove a Git worktree and optionally its branch |
| `workspace gc` | Garbage-collect orphaned agent worktrees older than max-age-days |

Example usage:
```bash
epubforge --config config.example.toml editor init work/mybook
epubforge --config config.example.toml editor doctor work/mybook
epubforge --config config.example.toml editor agent-output begin work/mybook --kind fixer --agent fixer-1 --chapter <chapter_uid>
epubforge --config config.example.toml editor agent-output add-patch work/mybook <output_id> --patch-file patch.json
epubforge --config config.example.toml editor agent-output submit work/mybook <output_id> --apply

# Render page 5 of source PDF (no VLM):
epubforge --config config.example.toml editor render-page work/mybook --page 5
# Re-analyze page 5 with VLM (result in vlm_observations/):
epubforge --config config.example.toml editor vlm-page work/mybook --page 5
# Analyze pages 5-10 with VLM:
epubforge --config config.example.toml editor vlm-range work/mybook --start-page 5 --end-page 10

# Export projections for agent context:
epubforge --config config.example.toml editor projection export work/mybook

# Workspace management:
epubforge --config config.example.toml editor workspace create work/mybook --branch agent/fixer-1
epubforge --config config.example.toml editor workspace merge work/mybook --branch agent/fixer-1
```

### Agent Workspace Workflow

Agents work concurrently using Git worktrees for isolation:

1. **Create**: `workspace create --branch agent/<kind>-<id>` creates a new worktree
   branched from HEAD (or `--base-ref`). Each agent gets its own branch and working
   directory.
2. **Work**: The agent reads projections for context, edits `book.json` via
   `agent-output` commands, and commits changes to its branch.
3. **Merge**: The supervisor calls `workspace merge --branch agent/<kind>-<id>` to
   merge the agent branch back. This performs a Git merge and validates the result
   semantically by diffing the merged Book against the base and verifying the generated
   patch applies cleanly.
4. **Cleanup**: `workspace remove` deletes the worktree and branch after merge.
   `workspace gc` removes stale agent worktrees older than `--max-age-days`.

Projections (`projection export`) provide read-only Markdown-ish views of the Book IR,
giving agents efficient context without loading the full JSON. When `01_raw_granite.json`
exists in the workdir, each chapter projection automatically appends a
`<!-- granite cross-reference for pages N, ... -->` block containing per-page Granite
markdown tagged with `[[granite-ref page=N]]` markers. These are **secondary evidence
only** (per `docs/rules/ocr-cross-validation.md`); the Standard pipeline text above the
block remains the primary source. The Granite block is omitted when `01_raw_granite.json`
is absent (fully backward-compatible).

### Stage 3 context in `edit_state/meta.json`

After `editor init`, `meta.json` includes a `stage3` object:

```json
{
  "stage3": {
    "mode": "docling | unknown",
    "artifact_id": "...",
    "manifest_sha256": "...",
    "selected_pages": [1, 2, ...],
    "complex_pages": [5, 12, ...],
    "source_pdf": "source/source.pdf",
    "evidence_index_path": "03_extract/artifacts/<id>/evidence_index.json",
    "extraction_warnings_path": "..."
  }
}
```

`mode` can be used by agents to determine whether `docling_*_candidate` roles need
semantic repair before the book is considered complete.

### Docling evidence draft and candidate repair ops

When extraction uses Docling mechanical parsing, blocks have `Provenance.source="docling"` and
`docling_*_candidate` roles. These are mechanical Docling labels — **not semantic decisions**.

The Docling pipeline does not decide: chapter boundaries, footnote pairing, cross-page
continuations, caption attribution, list hierarchy, or cross-page table merges.

Agent repair primitives for semantic repair:

| Primitive | Purpose |
|----|---------|
| `BookPatch.replace_node` | Replace block content, role, or type |
| `BookPatch.set_field` | Mark paragraph cross-page state; repair table title/caption/metadata fields |
| `PatchCommand` macros | Perform topology changes such as split/merge/relocate safely |

`vlm-page` / `vlm-range` can be used to gather VLM evidence for specific pages before issuing repair ops.
`render-page` can be used to inspect a page visually without consuming VLM tokens.

## Audit Subsystem

Audit detectors are in `src/epubforge/audit/`. Each returns an `AuditBundle`.

| Function | Module | Detects |
|----------|--------|---------|
| `detect_structure_issues` | `audit/structure.py` | Structural anomalies (heading levels, empty chapters, etc.) |
| `detect_table_merge_issues` | `audit/table_merge.py` | Problems in cross-page table merges |
| `detect_footnote_issues` | `audit/footnotes.py` | Orphan / unpaired footnotes |
| `detect_dash_inventory` | `audit/punctuation.py` | Dash / punctuation inventory |
| `detect_table_issues` | `audit/tables.py` | Malformed table HTML |
| `detect_invariant_issues` | `audit/invariants.py` | Book-level invariant violations |

## Semantic IR

Core classes are in `src/epubforge/ir/semantic.py` (and `ir/book_memory.py`):

- `Book` — root; holds `title`, `authors`, `chapters`, extraction metadata, and editor init metadata
- `Chapter` — holds `blocks: list[Block]`
- `Block` — discriminated union: `Paragraph | Heading | Footnote | Figure | Table | Equation`
- `Heading` — heading block with `level` and `text`
- `Footnote` — callout + text; `paired` and `orphan` flags
- `Figure` — caption + optional image ref
- `Table` — `html`, `table_title`, `caption`; `multi_page: bool` (True when merged from
  cross-page continuations); `merge_record: TableMergeRecord | None`
- `TableMergeRecord` — provenance for merged tables: `segment_html`, `segment_pages`,
  `segment_order`, `column_widths` (recorded at assemble time before uid init)
- `Provenance` — `{page, bbox, source: "llm"|"vlm"|"docling"|"passthrough"}`; `source="docling"` indicates mechanical parse output; `source="vlm"` is set by VLM pipeline (legacy); `source="llm"` is retained for backward compatibility
- `BookMemory` — rolling per-book facts: `footnote_callouts`, `attribution_templates`,
  `epigraph_chapters`, `punctuation_quirks`, `running_headers`, `chapter_heading_style`,
  `notes` (in `ir/book_memory.py`)
- `VLMPageOutput` — VLM response per page; `VLMGroupOutput.updated_book_memory` carries
  accumulated `BookMemory` increments from a multi-page VLM batch

### VLM Evidence Models

VLM observations from `vlm-page` / `vlm-range` are defined in `editor/vlm_evidence.py`:

- `VLMFinding` — single structured finding: `finding_type` (one of `missing_block`, `extra_block`,
  `text_mismatch`, `role_mismatch`, `layout_issue`, `table_error`, `footnote_error`, `figure_issue`,
  `heading_issue`, `quality_ok`, `other`), `severity` (`info|warning|error`), `block_uids`, `description`,
  optional `suggested_fix`
- `VLMObservation` — stored evidence unit with full provenance: `observation_id` (UUID4), `page`,
  `chapter_uid`, `related_block_uids`, `model`, `image_sha256`, `prompt_sha256`, `findings: list[VLMFinding]`,
  `created_at`, `dpi`, `source_pdf`
- `VLMObservationIndex` — index at `edit_state/vlm_observation_index.json` mapping `observation_id` to
  summary metadata for quick lookup

Observations are referenced by `AgentOutput.evidence_refs` and `BookPatch.evidence_refs`.

### Doctor Tasks

`DoctorTask` (in `editor/doctor.py`) is a machine-schedulable work item derived from a doctor
hint or audit issue. Supervisors use these to dispatch subagents without interpreting raw hints:

- `task_id` (UUID4), `kind` (`scan|fix|review`), `chapter_uid`, `block_uid`
- `source_issue_key` / `source_hint_key` — traceability to the originating audit issue or hint
- `priority` (0=highest, 3=lowest): issues get 0-1, warn hints get 2, info hints get 3
- `recommended_agent` (`scanner|fixer|reviewer`), `message` (human-readable description)

`DoctorReport.tasks` is populated by `generate_doctor_tasks()` after each doctor run.

## Config

Configuration uses `pydantic-settings` with **nested submodels**. The TOML structure
mirrors the Python model structure exactly.

### Loading rules

- TOML config path **must** be explicitly passed via `--config <path>`; there is no
  implicit scan of `config.toml` or `config.local.toml` in the cwd.
- `load_config(None)` uses defaults + env vars only (no TOML).
- `load_config(Path(...))` reads that single TOML file; fails if it does not exist.
- `EPUBFORGE_CONFIG_PATH` and automatic `.env` scanning are **not** supported.
- `resolved_vlm()` on `Config` is the single normalization entry for the effective VLM
  configuration (fallback: inherits `llm.api_key` when `vlm.api_key` is not set).

### Submodels

| Submodel | TOML section | Purpose |
|----------|-------------|---------|
| `ProviderSettings` | `[llm]` / `[vlm]` | Endpoint, API key, model, timeouts, caching |
| `RuntimeSettings` | `[runtime]` | Concurrency, cache/work/out dirs, log level |
| `EditorSettings` | `[editor]` | Compact threshold, max loops |
| `ExtractSettings` | `[extract]` | book memory toggle, Stage 1 page-batch size, OCR settings, Granite VLM settings |

Default VLM model: `google/gemini-flash-3` (max_tokens default: 16384).

### Environment variables (full whitelist)

Env vars use an explicit leaf-level mapping — they override individual fields without
overwriting sibling fields in the same submodel.

**`[llm]` submodel:**
```
EPUBFORGE_LLM_BASE_URL          llm.base_url
EPUBFORGE_LLM_API_KEY           llm.api_key
EPUBFORGE_LLM_MODEL             llm.model
EPUBFORGE_LLM_TIMEOUT           llm.timeout_seconds
EPUBFORGE_LLM_MAX_TOKENS        llm.max_tokens  (empty string → None)
EPUBFORGE_LLM_PROMPT_CACHING    llm.prompt_caching  (1/true/yes/on = True)
```

**`[vlm]` submodel:**
```
EPUBFORGE_VLM_BASE_URL          vlm.base_url
EPUBFORGE_VLM_API_KEY           vlm.api_key
EPUBFORGE_VLM_MODEL             vlm.model
EPUBFORGE_VLM_TIMEOUT           vlm.timeout_seconds
EPUBFORGE_VLM_MAX_TOKENS        vlm.max_tokens
EPUBFORGE_VLM_PROMPT_CACHING    vlm.prompt_caching
```

**`[runtime]` submodel:**
```
EPUBFORGE_RUNTIME_CONCURRENCY   runtime.concurrency
EPUBFORGE_RUNTIME_CACHE_DIR     runtime.cache_dir
EPUBFORGE_RUNTIME_WORK_DIR      runtime.work_dir
EPUBFORGE_RUNTIME_OUT_DIR       runtime.out_dir
EPUBFORGE_RUNTIME_LOG_LEVEL     runtime.log_level
```

**`[editor]` submodel:**
```
EPUBFORGE_EDITOR_COMPACT_THRESHOLD          editor.compact_threshold
EPUBFORGE_EDITOR_MAX_LOOPS                  editor.max_loops
```

**`[extract]` submodel:**
```
EPUBFORGE_ENABLE_BOOK_MEMORY               extract.enable_book_memory
EPUBFORGE_EXTRACT_PAGE_BATCH_SIZE          extract.page_batch_size  (Stage 1 batch size; default 20)
EPUBFORGE_EXTRACT_SEGMENT_SIZE             extract.segment_size  (int; empty string = None; when set, Stage 1 dispatches N-page segments to subprocess workers to bound peak RSS)
EPUBFORGE_EXTRACT_OCR_ENABLED              extract.ocr.enabled  (1/true/yes/on = True)
EPUBFORGE_EXTRACT_GRANITE_ENABLED          extract.granite.enabled  (1/true/yes/on = True)
EPUBFORGE_EXTRACT_GRANITE_API_URL          extract.granite.api_url
EPUBFORGE_EXTRACT_GRANITE_API_MODEL        extract.granite.api_model
EPUBFORGE_EXTRACT_GRANITE_TIMEOUT          extract.granite.timeout_seconds
EPUBFORGE_EXTRACT_PDF_BACKEND              traditional pipeline PDF backend; "docling_parse" | "pypdfium2"; default auto-selects pypdfium2 when OCR is enabled, docling_parse otherwise. See docs/explorations/stage1-pdf-parser-memory.md
EPUBFORGE_EXTRACT_DOCLING_INNER_BATCH      override docling internal page_batch_size (default 4). Lower values force more frequent unloads but increase peak RSS in our tests; provided for debugging only.
```

#### `[extract.granite]` — Granite secondary VLM parser

`GraniteSettings` controls the Granite-Docling-258M secondary pipeline in Stage 1. It is
distinct from `[vlm]`, which governs the editor evidence tools (`vlm-page` / `vlm-range`).

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable the secondary pipeline; also toggled at runtime by `--with-granite` / `--no-granite` / `--force-granite` |
| `api_url` | `http://localhost:8080/v1/chat/completions` | llama-server OpenAI-compatible chat-completions endpoint |
| `api_model` | `granite-docling` | Model name sent in the API request |
| `prompt` | `"Convert this page to docling."` | Per-page instruction prepended to each VLM request |
| `scale` | `2.0` | Rasterisation scale factor for page images sent to the VLM |
| `timeout_seconds` | `180` | Per-page HTTP timeout (int) |
| `max_tokens` | `4096` | Max tokens per page completion |
| `health_check` | `true` | Verify llama-server `/v1/models` is reachable before starting; fails fast on unreachable server |
| `concurrency` | `1` | Parallelism for llama-server requests; must match llama-server `-np` setting |

Output files written by the secondary pipeline:
- `01_raw_granite.json` — standalone `DoclingDocument` (never used as BookIR source)
- `01_raw_granite.manifest.json` — sidecar recording llama-server flags and version

### Test-only / scratch subprocess injection

These vars are injected by the editor subsystem's scratch runner (`editor/scratch.py`)
and are intended for test isolation or subprocess context injection only:

```
EPUBFORGE_EDITOR_NOW       Override current timestamp (scratch.py)
EPUBFORGE_PROJECT_ROOT     Injected into scratch subprocess env
EPUBFORGE_WORK_DIR         Injected into scratch subprocess env
EPUBFORGE_EDIT_STATE_DIR   Injected into scratch subprocess env
```

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
