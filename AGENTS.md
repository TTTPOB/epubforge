# epubforge Agent Instructions

## Scope

epubforge converts a PDF through MinerU and Luna into corrected chapter HTML.
The repository contains one five-stage pipeline and ends at corrected chapter
HTML.

Agents work from the chapter workspace produced by Stage 4. They receive the
chapter HTML, its annotated page JPEGs, and the chapter manifest. They must
never open, render, or otherwise access the source PDF. The pipeline owns PDF
access and page rendering before an agent starts.

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
page geometry, and asset hashes. Stage 3 asks the configured Luna model for
ordered chapter boundaries and validates every boundary against the content
contract. Stage 4 writes deterministic HTML with content IDs, page IDs, bboxes,
and annotated JPEG evidence. Stage 5 sends only that chapter evidence and HTML
to Luna, validates the returned HTML, and publishes the corrected pair
atomically.

## Configuration

Pass a TOML file explicitly with `--config`. Without it, epubforge loads
defaults and the listed environment variables. `config.example.toml` contains
the complete section shape:

- `[llm]`: endpoint, API key, model, token budget, prompt caching, and provider extras
- `[mineru]`: API key, endpoint, polling, archive limits, and extraction options
- `[runtime]`: concurrency, cache directory, work directory, and log level
- `[chapters]`: page JPEG DPI and quality

The main credentials are `EPUBFORGE_LLM_API_KEY` and
`EPUBFORGE_MINERU_API_KEY`. Leaf overrides also include
`EPUBFORGE_LLM_MODEL`, `EPUBFORGE_LLM_TIMEOUT`,
`EPUBFORGE_LLM_MAX_TOKENS`, `EPUBFORGE_LLM_PROMPT_CACHING`, all
`EPUBFORGE_MINERU_*` fields, all `EPUBFORGE_RUNTIME_*` fields, and
`EPUBFORGE_CHAPTERS_RENDER_DPI` / `EPUBFORGE_CHAPTERS_JPEG_QUALITY`.

LLM requests use `work/.cache/` by default. Logs go to
`work/<book_name>/logs/` and stderr. The CLI accepts `--log-level` and
`--log-file`.

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

Tests must use temporary work and cache directories. Credential smoke tests
must skip without both API keys and must never write to a shared repository
work directory.

Keep code comments in English. Do not add compatibility modules for removed
workflow stages.
