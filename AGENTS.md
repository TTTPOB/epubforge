# epubforge Agent Instructions

## Scope

epubforge converts a PDF through MinerU and a restricted OpenCode book-editor
agent into corrected chapter HTML.
The repository contains one five-stage pipeline and ends at corrected chapter
HTML.

The book-editor agent has segmentation and revision modes selected by TASK.md.
Segmentation receives a normalized content projection. Revision receives one
chapter's HTML, manifest, referenced assets, and annotated page JPEGs. Agents
must never open, render, or otherwise access the source PDF. Python owns PDF
access and page rendering.

## Five Stages

Each stage writes a stable contract under `work/<book_name>/` and can reuse a
fresh result. Use `--force-rerun` to rebuild the selected stage.

| Stage | Command | Input | Output |
| --- | --- | --- | --- |
| 1 parse | `parse` / `run --from 1` | PDF | `source/source.pdf`, `source/source_meta.json`, `01_raw.zip` |
| 2 normalize | `normalize` / `run --from 2` | Stage 1 archive and source PDF | `02_content/content.json`, `02_content/assets/` |
| 3 segment | `segment` / `run --from 3` | normalized content | `03_chapters/chapters.json` |
| 4 prepare | `prepare` / `run --from 4` | normalized content, chapter plan, source PDF | `04_edit/manifest.json`, chapter HTML, annotated page JPEGs |
| 5 revise | `revise` / `run --from 5` | chapter workspaces | `corrected.html`, `revision.json` in each completed chapter |

`run` executes all five stages in order. `--from N` reuses earlier outputs and
allows `--force-rerun` to affect stage N and later stages. Stage 1 accepts at
most 1000 PDF pages and uploads at most 200 pages per MinerU request. It keeps
the original MinerU response archive unchanged; segmented requests use an
outer archive with `manifest.json` and ordered response ZIP members.

Stage 2 validates ZIP paths, JSON syntax, source-PDF identity, page count,
page geometry, and asset hashes. Stage 3 runs the packaged OpenCode agent in an
isolated workspace and validates every returned boundary against the content
contract. Stage 4 writes deterministic HTML with content IDs, page IDs, bboxes,
and annotated JPEG evidence. Stage 5 lets the same agent edit a pre-seeded
`corrected.html`, validates the result, and publishes the corrected pair
atomically.

Agent workspaces use mode 0700 system temporary directories outside the
repository and book workspace. The runner disables project config and external
skills. It denies shell commands, subagents, external directories, web tools,
skills, questions, and MCP tools. It allows file reads, listing, globbing,
grep, and edits only inside the isolated directory. Never place a PDF in an
agent workspace.

## Configuration

Pass a TOML file explicitly with `--config`. Without it, epubforge loads
defaults and the listed environment variables. `config.example.toml` contains
the complete section shape:

- `[mineru]`: API key, endpoint, polling, archive limits, and extraction options
- `[runtime]`: concurrency, work directory, and log level
- `[chapters]`: page JPEG DPI and quality

The epubforge credential is `EPUBFORGE_MINERU_API_KEY`. Leaf overrides include
the documented `EPUBFORGE_MINERU_*` fields,
`EPUBFORGE_RUNTIME_CONCURRENCY`, `EPUBFORGE_RUNTIME_WORK_DIR`,
`EPUBFORGE_RUNTIME_LOG_LEVEL`, and the chapter rendering fields. OpenCode owns
model authentication and provider configuration. Logs go to
`work/<book_name>/logs/` and stderr.

## Tests and Quality Gates

```bash
uv sync
uv run pytest
uv run pytest -n 0
uv run pyrefly check
uv run python -m compileall -q src tests scripts
uv lock --check
uv run epubforge --help
```

Tests must use temporary work and agent directories. MinerU smoke tests skip
without its API key; real agent tests also require a usable OpenCode
environment. Smoke tests must never write to a shared repository work
directory.

Keep code comments in English. Do not add compatibility modules for removed
workflow stages.
