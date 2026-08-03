# Isolated OpenCode Book-Editing Workflow

## Design

epubforge keeps the ingestion path linear:

```text
PDF
  -> MinerU archive
  -> normalized content and page geometry
  -> isolated OpenCode chapter boundaries
  -> deterministic chapter HTML and annotated page JPEGs
  -> isolated OpenCode corrected HTML
```

Python owns PDF access, deterministic rendering, validation, and publication.
The packaged `book-editor` agent uses `openai/gpt-5.6-luna` with variant
`medium`. One prompt handles two modes through a mode-specific `TASK.md`.

Each run receives a small file workspace rather than repository access or a
large mutation schema. HTML remains the editable representation: tags express
semantic type, DOM order expresses reading order, and stable source attributes
connect the result to normalized content. The agent edits ordinary files;
Python retains hashes, geometry contracts, locks, and publication records.

## Contracts

Stage 1 stores the untouched MinerU response at `01_raw.zip` and records the
source PDF hash, page count, and publication metadata in `source/source_meta.json`.
Stage 2 reads the archive with strict ZIP and JSON validation. It writes
`02_content/content.json`, whose `items` retain MinerU content order and whose
`page_geometry` uses top-left coordinates in displayed page orientation.

Stage 3 writes `TASK.md` and `content-projection.json` into an isolated
workspace. The projection contains only content index, zero-based page index,
type, optional text level, and text. It contains no PDF or source asset path.
The agent writes `boundaries.json` matching `ChapterSegmentationResponse`.
Python checks source-addressed indices, exact title text, ordering, and
frontmatter/chapter/backmatter phases before atomically writing
`03_chapters/chapters.json`. The artifact records the agent identity, agent
fingerprint, prompt fingerprint, contract fingerprint, and session ID when
OpenCode emits one. Version 1 artifacts are stale by definition.

Stage 4 maps each boundary range to a deterministic chapter workspace. The
HTML preserves `data-content-idx`, `data-page-idx`, and `data-bbox` attributes.
The JPEG files show the corresponding page evidence and labels. The manifest
records chapter ranges, source hashes, and file hashes.

Stage 5 validates the Stage 4 workspace under the chapter lock. It snapshots
`chapter.html`, `chapter.json`, referenced assets, and ordered annotated JPEGs,
then copies those files with `TASK.md` into a new isolated workspace. Python
pre-seeds `corrected.html` from `chapter.html`; the agent edits that file in
place. No structured notes handoff is required.

Python reads only `corrected.html` back. Existing validators reject unknown or
missing substantive content IDs, changed bbox values, invented coordinates,
unsafe tags and attributes, malformed splits, reordered merged IDs, changed
asset references, malformed tables, and oversized output. Python publishes
`corrected.html` and `revision.json` as one recoverable atomic pair. Revision
metadata records source hashes, agent identity and fingerprint, prompt and
contract fingerprints, and the OpenCode session ID when present.

## Runner Boundary

`OpenCodeAgentRunner` creates mode-0700 directories under the system temporary
root and rejects locations inside the repository or book work directory. The
caller supplies byte snapshots, so no symlink or source path crosses the
boundary. The runner rejects PDF filenames.

The subprocess uses an argv list with `shell=False`, `stdin=DEVNULL`, a new
process session, `--pure`, `--agent book-editor`, `--dir`, `--format json`, a
bounded title, and a fixed prompt. Before switching to a private
`XDG_CONFIG_HOME`, the runner resolves the user's configuration with
`opencode debug config --pure` in a neutral temporary cwd. It copies only
`provider`, `enabled_providers`, and `disabled_providers` into a mode-0600
private config file beside the packaged agent. The runner preserves the
inherited `XDG_DATA_HOME` so OpenCode can find its auth data. The runner sets
`OPENCODE_DISABLE_PROJECT_CONFIG`, `OPENCODE_DISABLE_EXTERNAL_SKILLS`, and
`OPENCODE_DISABLE_CLAUDE_CODE_SKILLS`.

The effective permission policy denies all tools, then allows read, list,
glob, grep, and edit inside the isolated cwd. It explicitly denies bash, task,
external directories, web fetch/search, skills, questions, LSP, and todo tools.
The wildcard denial also covers MCP tools, and project config disabling keeps
the repository PDF MCP out of the session.

The runner drains stdout and stderr concurrently while enforcing total and
per-line byte limits. It enforces a wall-clock timeout, terminates the complete
process group, parses bounded JSON events for a session ID, snapshots declared
regular output files without following symlinks, and removes the workspace in
`finally`.

## Freshness And Failure Handling

Stage freshness derives from source hashes, the packaged agent fingerprint,
the mode prompt fingerprint, and Python's validation contract. The pipeline
checks freshness before constructing `OpenCodeAgentRunner`. A fresh run needs
no model credentials or OpenCode process.

A failed segmentation run leaves the previous chapter plan untouched. A failed
chapter run leaves the last complete HTML and revision record in place. Stage 5
processes chapters in ordinal order and stops after the first failure unless
the caller passes `--continue-on-error`; successful publications remain in
place. Chapter locks preserve one coherent output pair under concurrent calls.

## Paddle Evidence Tool

The standalone `scripts/paddle_layout_tune.py` script remains a supported
layout-evidence tool. It can render selected pages, cache Paddle layout and
text detections, generate stable candidate IDs, and apply deterministic
postprocessing. Its Luna prompt lives in
`docs/prompts/luna-paddle-layout-tuning.md`.

Paddle evidence does not change the chapter contracts by itself. A later
pipeline stage must consume any accepted candidate output explicitly.
