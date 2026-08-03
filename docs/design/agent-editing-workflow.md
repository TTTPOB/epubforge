# Isolated OpenCode Book-Editing Workflow

## Design

epubforge keeps the ingestion path linear:

```text
PDF
  -> MinerU archive
  -> normalized content and page geometry
  -> isolated OpenCode chapter boundaries
  -> deterministic chapter HTML, clean and annotated page JPEGs, page geometry
  -> isolated OpenCode corrected HTML with Python-applied bbox repairs
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
Each page renders once and produces three linked files under `pages/`:

- `page-NNNN.jpg` — the clean page image the Paddle page CLI consumes;
- `page-NNNN.annotated.jpg` — the same page with stable-ID bbox labels;
- `page-NNNN.meta.json` — the page geometry contract (page index, canonical
  page_geometry width and height, raster pixel width and height, and the
  per-axis raster/canonical conversion scale).

The manifest records chapter ranges, source hashes, and file hashes.
`chapter.json` lists the three page groups as `pages`, `annotated_pages`, and
`page_metadata`.

Stage 5 validates the Stage 4 workspace under the chapter lock. It snapshots
`chapter.html`, `chapter.json`, referenced assets, the clean and annotated
JPEGs, and the page metadata, then copies those files with `TASK.md` into a new
isolated workspace. Python pre-seeds `corrected.html` from `chapter.html`; the
agent edits that file in place. No structured notes handoff is required.

Python reads `corrected.html`, `bbox-repairs.json`, and `paddle-evidence.json`
back. Existing validators reject unknown or missing substantive content IDs,
changed bbox values, invented coordinates, unsafe tags and attributes,
malformed splits, reordered merged IDs, changed asset references, malformed
tables, and oversized output. Python publishes `corrected.html` and
`revision.json` as one recoverable atomic pair. Revision metadata records
source hashes, agent identity and fingerprint, prompt and contract
fingerprints, the OpenCode session ID when present, the repair input hashes,
and every applied repair with its final canonical bbox.

## Bounding Box Repairs

The revision agent reviews each `data-bbox` against the clean image, the
annotated image, and the page geometry contract. When a rectangle is
unreliable it may call the local Paddle page-image CLI
(`epubforge-paddle-page`) on the clean workspace images only, batch several
pages per invocation, and merge per-page evidence into `paddle-evidence.json`.
The CLI never touches a PDF: it validates JPEG input, caches raw model output,
and supports `--reuse-raw` postprocessing reruns and trusted `--page-patch`
hooks that the agent writes into the workspace.

The repair contract is Python-owned to remove ambiguity and keep an audit
trail. The agent never writes coordinates. `bbox-repairs.json` references
stable candidate IDs only:

```json
{
  "schema_version": 1,
  "repairs": [
    {
      "data_content_idx": 5,
      "data_content_part": null,
      "page_idx": 1,
      "candidate_ids": ["p0001-figure-3f2a9c1d4e"],
      "operation": "replace",
      "reason": "Paddle layout places the figure box over the caption."
    }
  ]
}
```

Python validates every repair: the target element is unique, carries exactly
one content ID, matches the declared page index, is not flagged
`needs-repair`, and already carries `data-bbox`. Candidate IDs must exist in
the collected `paddle-evidence.json` for that page, whose `source_image` must
be the clean workspace page, whose recorded source image SHA-256 must equal
the chapter's clean page snapshot, and whose raster dimensions must match the
page metadata. Every candidate ID is recomputed from page/type/bbox and
rejected when it does not derive from the geometry. `replace` takes exactly
one candidate; `union` takes one or more.
Python converts the raster candidate boxes into canonical page geometry with
the metadata scale, checks that the result is finite, has positive area, and
stays inside the page, applies every `data-bbox` change to `corrected.html`
with exact attribute surgery, and revalidates the final HTML so the only bbox
differences from the source are exactly the applied repairs. Without a
reliable candidate the agent keeps the original bbox and adds
`data-bbox-status="needs-repair"` instead.

Freshness revalidates the published `corrected.html` against the recorded
applied repairs, so tampering with a repaired bbox forces a rerun.

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
glob, grep, and edit inside the isolated cwd. It always denies task,
external directories, web fetch/search, skills, questions, LSP, and todo
tools. The wildcard denial also covers MCP tools, and project config disabling
keeps the repository PDF MCP out of the session.

The `[runtime] execution_profile` setting selects the Stage 5 revision
permission profile; Stage 3 segmentation always runs `restricted` regardless
of the setting:

- `development` (default) opens local bash inside the isolated workspace, so
  the agent can run `epubforge-paddle-page` and workspace-local patch
  commands. This is an intentionally trusted mode, not an OS sandbox: the
  agent is expected to stay inside the workspace and rely on prompt
  constraints, and a stronger sandbox may replace it later;
- `restricted` keeps the legacy contract with bash denied.

The packaged `book-editor.md` frontmatter declares the restricted baseline;
the runner widens permissions per profile and includes the profile and the
effective permission map in the agent fingerprint, so a profile switch
invalidates freshness.

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

## Paddle Tools

Two CLIs share one detection and postprocessing pipeline in
`epubforge.paddle_common`:

- `scripts/paddle_layout_tune.py` is the offline tuning tool. It renders
  selected PDF pages, caches Paddle layout and text detections, generates
  stable candidate IDs, and applies deterministic postprocessing. Its Luna
  prompt lives in `docs/prompts/luna-paddle-layout-tuning.md`.
- `epubforge-paddle-page` is the agent-facing page-image CLI installed as a
  console script. It runs the same models on clean Stage 4 page JPEGs, writes
  `evidence.json`, `candidate.json`, `layout_raw.jpg`, `text_raw.jpg`, and
  `annotated.jpg` under `.paddle/page-NNNN/`, caches the raw inference for
  `--reuse-raw` reruns, enforces per-run page, pixel, output, and aggregate
  evidence limits, and merges per-page evidence and candidates (with the
  source image SHA-256) into `paddle-evidence.json` so Stage 5 can validate
  `bbox-repairs.json` against exactly the detections the agent used.
