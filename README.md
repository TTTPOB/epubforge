# epubforge

epubforge is a MinerU-Luna pipeline that turns a PDF into corrected chapter
HTML. MinerU supplies the raw document archive. Luna detects chapter boundaries
and revises chapter HTML after the pipeline prepares annotated page evidence.

## Quick Start

Set `EPUBFORGE_MINERU_API_KEY` and `EPUBFORGE_LLM_API_KEY`, then run:

```bash
uv sync
uv run epubforge --config config.example.toml run input.pdf
```

The six CLI commands are `run`, `parse`, `normalize`, `segment`, `prepare`,
and `revise`. Each stage command accepts `--force-rerun`; `run` also accepts
`--from 1` through `--from 5` and `--continue-on-error` for chapter revision.

## Contracts

| Stage | Command | Output |
| --- | --- | --- |
| 1 | `parse` | `source/source.pdf`, `source/source_meta.json`, `01_raw.zip` |
| 2 | `normalize` | `02_content/content.json`, `02_content/assets/` |
| 3 | `segment` | `03_chapters/chapters.json` |
| 4 | `prepare` | `04_edit/manifest.json`, chapter HTML, annotated page JPEGs |
| 5 | `revise` | `04_edit/chapters/<ordinal>/corrected.html`, `revision.json` |

For a book named `input`, the workspace looks like:

```text
work/input/
├── source/
│   ├── source.pdf
│   └── source_meta.json
├── 01_raw.zip
├── 02_content/
│   ├── content.json
│   └── assets/
├── 03_chapters/chapters.json
└── 04_edit/
    ├── manifest.json
    └── chapters/0001/
        ├── chapter.json
        ├── chapter.html
        ├── corrected.html
        ├── revision.json
        └── pages/page-0000.jpg
```

Stage 1 stores the untouched MinerU ZIP. PDFs up to 200 pages use the original
response ZIP directly. Larger accepted PDFs use an outer archive containing
`manifest.json` and ordered response ZIPs, with no flattened members.

Stage 1 requires POSIX directory `fsync` and atomic rename support. It rejects
unsupported filesystems before calling MinerU or replacing published files.

## Configuration

Pass configuration explicitly:

```bash
uv run epubforge --config config.example.toml parse input.pdf
```

The configuration sections are `[llm]`, `[mineru]`, `[runtime]`, and
`[chapters]`. Environment variables override individual fields. The main
credentials are `EPUBFORGE_LLM_API_KEY` and `EPUBFORGE_MINERU_API_KEY`.

## Tests

```bash
uv run pytest
uv run pytest -n 0
uv run pyrefly check
uv run python -m compileall -q src tests scripts
uv lock --check
```

The optional Paddle layout tuning script remains at
`scripts/paddle_layout_tune.py`. It reads selected PDF pages in its own
explicit run, caches raw inference evidence, and supports deterministic
postprocessing and trusted page patches.
