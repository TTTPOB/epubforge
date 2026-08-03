# epubforge Agent Instructions

## Scope

epubforge converts a PDF through MinerU and an isolated OpenCode book-editor
agent into corrected chapter HTML.
The repository contains one five-stage pipeline and ends at corrected chapter
HTML.

The book-editor agent has segmentation and revision modes selected by TASK.md.
Segmentation receives a normalized content projection. Revision receives one
chapter's HTML, manifest, referenced assets, clean and annotated page JPEGs,
and per-page geometry contracts. Agents must never open, render, or otherwise
access the source PDF. Python owns PDF access and page rendering.

## Five Stages

Each stage writes a stable contract under `work/<book_name>/` and can reuse a
fresh result. Use `--force-rerun` to rebuild the selected stage.

| Stage | Command | Input | Output |
| --- | --- | --- | --- |
| 1 parse | `parse` / `run --from 1` | PDF | `source/source.pdf`, `source/source_meta.json`, `01_raw.zip` |
| 2 normalize | `normalize` / `run --from 2` | Stage 1 archive and source PDF | `02_content/content.json`, `02_content/assets/` |
| 3 segment | `segment` / `run --from 3` | normalized content | `03_chapters/chapters.json` |
| 4 prepare | `prepare` / `run --from 4` | normalized content, chapter plan, source PDF | `04_edit/manifest.json`, chapter HTML, clean and annotated page JPEGs, per-page geometry |
| 5 revise | `revise` / `run --from 5` | chapter workspaces | `corrected.html`, `revision.json`, applied bbox repairs in each completed chapter |

`run` executes all five stages in order. `--from N` reuses earlier outputs and
allows `--force-rerun` to affect stage N and later stages. Stage 1 accepts at
most 1000 PDF pages and uploads at most 200 pages per MinerU request. It keeps
the original MinerU response archive unchanged; segmented requests use an
outer archive with `manifest.json` and ordered response ZIP members.

Stage 2 validates ZIP paths, JSON syntax, source-PDF identity, page count,
page geometry, and asset hashes. Stage 3 runs the packaged OpenCode agent in an
isolated workspace and validates every returned boundary against the content
contract. Stage 4 writes deterministic HTML with content IDs, page IDs, bboxes,
one clean JPEG and one annotated JPEG per page, and a per-page geometry
contract with the raster/canonical conversion. Stage 5 lets the same agent
edit a pre-seeded `corrected.html`; the agent may call the local Paddle
page-image CLI on clean page images and writes `bbox-repairs.json` referencing
candidate IDs only. Python converts candidates to canonical coordinates,
applies every `data-bbox` change itself, validates the result, and publishes
the corrected pair atomically. `revision.json` records the repair input
hashes and every applied repair.

Agent workspaces use mode 0700 system temporary directories outside the
repository and book workspace. The runner disables project config and external
skills. It always denies subagents, external directories, web tools, skills,
questions, and MCP tools, and allows file reads, listing, globbing, grep, and
edits only inside the isolated directory. `[runtime] workspace_retention`
(default `on-failure`) decides where the workspace goes after each run:
`never` removes it on every outcome (the direct-runner default),
`on-failure` keeps it after runner failures and semantic rejections and
removes it only after an accepted publication (a failed deletion keeps the
workspace, records the truthful `retained` disposition with a bounded reason,
and the run stays accepted), and `always` keeps it for every outcome.
Retained workspaces are verified without following symlinks, restored to mode
0700, logged with a bounded reason (also for journal-less direct runs), and
may be removed manually after debugging. `AgentCleanupError` retains every
path a possibly-live process may still reach under every policy.

Execution profiles widen the permission baseline: `[runtime]
execution_profile` selects `development` (default) or `restricted`. Both
Stage 3 segmentation and Stage 5 revision run the configured profile:
`development` (the default workflow for both agent stages) opens local bash
inside the workspace so the agent can call `epubforge-paddle-page`, while
`restricted` keeps bash denied as an explicit opt-in that the default
workflow does not require. Development bash is an intentionally trusted
mode, not an OS sandbox: the agent is expected to stay inside the workspace
and rely on prompt constraints, and a stronger sandbox may replace it later.
Never place a PDF in an agent workspace.

## Configuration

Pass a TOML file explicitly with `--config`. Without it, epubforge loads
defaults and the listed environment variables. `config.example.toml` contains
the complete section shape:

- `[mineru]`: API key, endpoint, polling, archive limits, and extraction options
- `[runtime]`: concurrency, work directory, log level, execution profile, and
  agent workspace retention (`never` | `on-failure` default | `always`)
- `[chapters]`: page JPEG DPI and quality

The epubforge credential is `EPUBFORGE_MINERU_API_KEY`. Leaf overrides include
the documented `EPUBFORGE_MINERU_*` fields,
`EPUBFORGE_RUNTIME_CONCURRENCY`, `EPUBFORGE_RUNTIME_WORK_DIR`,
`EPUBFORGE_RUNTIME_LOG_LEVEL`, `EPUBFORGE_RUNTIME_EXECUTION_PROFILE`,
`EPUBFORGE_RUNTIME_WORKSPACE_RETENTION`, and the
chapter rendering fields. OpenCode owns
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
