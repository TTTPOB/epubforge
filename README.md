# epubforge

epubforge turns a PDF into corrected chapter HTML. MinerU supplies the raw
document archive. Python prepares bounded file workspaces and invokes an
isolated OpenCode `book-editor` agent for chapter segmentation and revision.
The agent may call the local Paddle page-image CLI during revision to verify
and repair bounding boxes.

## Quick Start

Set `EPUBFORGE_MINERU_API_KEY`, configure a usable `opencode` installation, and
run:

```bash
uv sync
uv run epubforge --config config.example.toml run input.pdf
```

OpenCode owns model authentication and provider configuration. epubforge does
not accept or store a model API key.

The six CLI commands are `run`, `parse`, `normalize`, `segment`, `prepare`,
and `revise`. Each stage command accepts `--force-rerun`; `run` also accepts
`--from 1` through `--from 5` and `--continue-on-error` for chapter revision.

## Contracts

| Stage | Command | Output |
| --- | --- | --- |
| 1 | `parse` | `source/source.pdf`, `source/source_meta.json`, `01_raw.zip` |
| 2 | `normalize` | `02_content/content.json`, `02_content/assets/` |
| 3 | `segment` | `03_chapters/chapters.json` |
| 4 | `prepare` | `04_edit/manifest.json`, chapter HTML, clean and annotated page JPEGs, per-page geometry |
| 5 | `revise` | `04_edit/chapters/<ordinal>/corrected.html`, `revision.json`, applied bbox repairs |

Stage 3 gives the agent only `TASK.md` and `content-projection.json`; the agent
writes `boundaries.json`. Stage 5 gives it `TASK.md`, `chapter.json`, the
complete chapter HTML, referenced assets, clean page images, annotated page
images, and per-page geometry contracts. The agent edits a pre-seeded
`corrected.html` and may run the local Paddle page CLI on the clean page
images. It never receives, opens, or renders the source PDF.

Stage 5 bounding-box repairs follow a Python-owned contract: the agent only
writes `bbox-repairs.json` referencing candidate IDs collected in
`paddle-evidence.json`; Python converts the raster candidate boxes into
canonical page geometry using `pages/page-NNNN.meta.json`, applies every
`data-bbox` change itself, and records the applied repairs in `revision.json`.

Python creates each agent workspace as a mode-0700 system temporary directory
outside the repository and book work directory. It disables project config and
external skills, denies subagents, web tools, MCP, questions, and external
paths, and removes the workspace after collecting bounded outputs. No source
PDF enters an agent workspace.

Execution profiles control bash inside the workspace. Stage 3 segmentation
always runs `restricted` (bash denied). Stage 5 revision runs the configured
profile: `development` (default) opens local bash so the book editor can call
`epubforge-paddle-page`; `restricted` keeps the legacy command-denied profile.
Development bash is an intentionally trusted mode, not an OS sandbox: the
agent is expected to stay inside the workspace and rely on prompt
constraints, and a stronger sandbox may replace it later.

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
        └── pages/
            ├── page-0000.jpg
            ├── page-0000.annotated.jpg
            └── page-0000.meta.json
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

The configuration sections are `[mineru]`, `[runtime]`, and `[chapters]`.
Environment variables override individual fields. The only epubforge API
credential is `EPUBFORGE_MINERU_API_KEY`.

`[runtime] execution_profile` selects the Stage 5 agent permission profile:
`development` (default) opens local bash inside the isolated workspace so the
book editor can call `epubforge-paddle-page`; `restricted` keeps the legacy
command-denied permissions. Stage 3 segmentation always uses `restricted`
regardless of this setting. The profile participates in the agent fingerprint,
so switching profiles reruns segment and revise.

## Tests

```bash
uv run pytest
uv run pytest -n 0
uv run pyrefly check
uv run python -m compileall -q src tests scripts
uv lock --check
uv run epubforge --help
```

Two Paddle CLIs share one detection pipeline in `epubforge.paddle_common`:

- `scripts/paddle_layout_tune.py` is the offline tuning tool. It reads
  selected PDF pages in its own explicit run, caches raw inference evidence,
  and supports deterministic postprocessing and trusted page patches.
- `epubforge-paddle-page` is the agent-facing page-image CLI. It runs
  LayoutDetection and TextDetection on clean Stage 4 page JPEGs, writes
  evidence and candidate JSON plus evidence JPEGs, caches raw output for
  `--reuse-raw` postprocessing reruns, and merges per-page evidence into
  `paddle-evidence.json` for Python-side repair validation. It never touches a
  PDF.
