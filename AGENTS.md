# epubforge — Agent Instructions

## Project Overview

LLM/VLM-assisted PDF → EPUB pipeline for books and academic theses.
Seven-stage pipeline: parse → classify → extract → assemble → refine-toc → proofread → build.

Each stage writes to `work/<book_name>/0N_*.json` and is independently re-runnable.
LLM/VLM calls are cached in `work/.cache/` (key = sha256 of model+prompt+image).

## Pipeline Stages

| # | CLI flag `--from` | Command | Input | Output |
|---|-------------------|---------|-------|--------|
| 1 | `--from 1` | `parse` | PDF | `01_raw.json` (Docling JSON) |
| 2 | `--from 2` | `classify` | `01_raw.json` | `02_pages.json` (simple/complex/toc labels) |
| 3 | `--from 3` | `extract` | `01_raw.json` + `02_pages.json` | `03_extract/unit_*.json` (LLM+VLM blocks) |
| 4 | `--from 4` | `assemble` | `03_extract/` | `05_semantic_raw.json` (Semantic IR) |
| 5 | `--from 5` | `refine-toc` | `05_semantic_raw.json` | `05_semantic.json` (refined headings) |
| 6 | `--from 6` | `proofread` | `05_semantic.json` | `06_proofread.json` (Phase1+Phase2 edits) |
| 7 | `--from 7` | `build` | `06_proofread.json` | `out/<name>.epub` |

## Observability

All stages and every LLM/VLM request emit INFO-level logs. By default logs are written to
`work/<book>/logs/run-<timestamp>.log` (plain text, grep-friendly) and also to stderr via
RichHandler (coloured, aligned).

CLI flags:
- `--log-level / -L` — set level (DEBUG/INFO/WARNING); env `EPUBFORGE_LOG_LEVEL`
- `--log-file` — override log file path (default: auto-generated per run)

Per-request log lines include: kind (LLM/VLM), req_id (first 8 chars of cache key),
model, format name, message count, char count, image count, cache HIT/MISS, elapsed
time, finish reason, prompt+completion token counts, and `cached=<N>` (provider-side
cached input tokens, non-zero when prompt caching is active). Each stage emits a summary
line on completion showing elapsed time, total requests, cache hit rate, tokens used,
and `cache_read=<N>` (aggregate cached tokens for the stage, omitted when zero).

## Two-Layer IR

- **Raw IR**: Docling `DoclingDocument` JSON (lossless, never modified)
- **Semantic IR**: Pydantic v2 models in `src/epubforge/ir/semantic.py`
  `Book → Chapter → Block[Paragraph|Heading|Footnote|Figure|Table|Equation]`
  Each block carries `provenance: {page, bbox, source: "llm"|"vlm"|"passthrough"}`

## Config (env vars / .env)

```
EPUBFORGE_LLM_BASE_URL   default: https://openrouter.ai/api/v1
EPUBFORGE_LLM_API_KEY    required when using LLM stages
EPUBFORGE_LLM_MODEL      default: anthropic/claude-haiku-4.5
EPUBFORGE_VLM_BASE_URL   default: same as LLM
EPUBFORGE_VLM_API_KEY    required when using VLM stage
EPUBFORGE_VLM_MODEL      default: google/gemini-flash-3
EPUBFORGE_CONCURRENCY    default: 4
EPUBFORGE_CACHE_DIR      default: work/.cache
```

## Key Conventions

- All stages accept `--force-rerun` (`-f`) to re-run even if output exists
- All stages skip if output already present (idempotent); pass `--force-rerun` to override
- VLM output must be structured JSON matching `VLMPageOutput` schema in `ir/semantic.py`
- Never rewrite content — LLM only merges line breaks, removes headers/footers, normalises headings
- `fixtures/` holds test PDFs (gitignored `*.pdf`); run `uv run epubforge run fixtures/<name>.pdf`

# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work atomically
bd close <id>         # Complete work
bd dolt push          # Push beads data to remote
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
