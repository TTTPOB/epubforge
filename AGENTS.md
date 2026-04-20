# epubforge — Agent Instructions

## Project Overview

LLM/VLM-assisted PDF → EPUB pipeline for books and academic theses.
Six-stage pipeline: parse → classify → clean_simple → read_complex → assemble → build_epub.

Each stage writes to `work/<book_name>/0N_*.json` and is independently re-runnable.
LLM/VLM calls are cached in `work/.cache/` (key = sha256 of model+prompt+image).

## Pipeline Stages

| # | Command | Input | Output | Issue |
|---|---------|-------|--------|-------|
| 1 | `parse` | PDF | `01_raw.json` (Docling JSON) | epubforge-5k2 |
| 2 | `classify` | `01_raw.json` | `02_pages.json` (simple/complex labels) | epubforge-51n |
| 3 | `clean` | simple pages | `03_simple/*.json` (LLM cleaned blocks) | epubforge-2u9 |
| 4 | `vlm` | complex pages | `04_complex/*.json` (VLM structured JSON) | epubforge-2om |
| 5 | `assemble` | stages 3+4 | `05_semantic.json` (Semantic IR) | epubforge-d81 |
| 6 | `build` | `05_semantic.json` | `out/<name>.epub` | epubforge-cjc |

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

- All stages accept `--force` to re-run even if output exists
- Stages 1-4 skip if output already present (idempotent)
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
