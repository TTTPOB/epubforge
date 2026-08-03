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

The `[runtime] execution_profile` setting selects the agent permission
profile for both Stage 3 segmentation and Stage 5 revision. The default
workflow runs both stages under `development`; `restricted` is an explicit
opt-in reserved path that the default workflow does not require:

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
regular output files without following symlinks, and removes the workspace
under the configured retention policy.

Termination is a guarantee, not a best effort: `_terminate_process_group`
returns only after the leader is reaped and no process remains in the group,
and otherwise raises a bounded `AgentExecutionError`. When a group cannot be
cleared, the runner surfaces an `AgentCleanupError` carrying the original run
error as its cause and the retained workspace/config-home/probe paths that the
possibly-live process can still access; those directories are never deleted
and are logged for manual cleanup, and the journal keeps the truthful running
process entry instead of recording a finish that never happened. Cleanup
failures for the config home, the config probe directory, or the main
workspace never replace an active timeout, parse, journal, or semantic error:
they add a bounded note and a log line, and raise normally only when they are
the sole failure. Under deferred retention policies the runner no longer
removes the main workspace on its own failure path, so the config home and
probe directory keep these rules under every policy while the workspace
follows the retention resolution described below. The config probe runs under
its own fixed timeout bound, independent of the main task timeout, so a tiny
task timeout cannot fail the probe first.

Raw main-agent stdout/stderr are never journaled by default: the development
bash profile can echo inherited environment or provider credentials into
either stream, so persisting them without an explicit caller decision risks
credential or content exposure. `OpenCodeAgentRunner` exposes a constructor-only
debug opt-in, `capture_raw_main_streams=True`, that persists the bounded raw
streams under `logs/agent-runs/<run-id>/`; the production pipeline stages and
direct runners keep the safe default, where the manifest records the main
agent process with `capture: "disabled"` and no stream files. The config probe
always runs capture-disabled with no opt-in. Session IDs, output hashes,
process outcomes, and the semantic accepted/rejected status stay durable in
both modes; accepted/rejected marks require complete stream records only when
streams are captured, and otherwise rely on strict session parsing. Every
atomically persisted running manifest passes strict validation: stream-capture
processes carry zero-byte stream records from start and keep
`capture_complete` pending until the process finishes, so a host crash between
start and finish leaves a readable incomplete/running capture.

Every terminal transition (accepted, rejected, failed) is two-phase. The
journal first persists a pending `finalization` intent — target status,
attempt timestamp, and the bounded validation or runner-error payload — and
only then atomically writes the terminal manifest and marks the intent
completed. If the terminal write fails or the host crashes between the two
writes, the last durable manifest stays non-terminal (`final_status:
"started"` with pending validation) while carrying the pending intent, so it
never reads like an ordinary running record and a strict reader can see
exactly which terminal target the run was transitioning to. A plain running
manifest carries `finalization: null`; a terminal manifest carries the
completed intent (or none, for records predating the intent). This matters
when publication already succeeded but the accepted marking failed: the
published artifact stays in place, the manifest never claims acceptance, and
the pending intent documents the attempted accepted transition.

## Workspace Retention

`[runtime] workspace_retention` (env `EPUBFORGE_RUNTIME_WORKSPACE_RETENTION`)
selects where the isolated agent workspace goes after each run. The values are
strict: `never`, `on-failure`, `always`.

- `never` removes the workspace after runner success or failure, exactly as
  the legacy runner did. This is the direct-runner default
  (`OpenCodeAgentRunner` without an explicit policy), preserving the cleanup
  expectations of scripts and tests that construct the runner directly.
- `on-failure` is the production pipeline default. The runner keeps the
  workspace after its own failures; the stages keep it after semantic
  validation or publication rejection. Only an accepted publication
  (Stage 3's `chapters.json` or Stage 5's `corrected.html`/`revision.json`
  pair, atomically published and durably marked accepted) removes it.
- `always` keeps the workspace for accepted, rejected, and runner-failed runs.

The semantic accepted/rejected decision happens after `OpenCodeAgentRunner`
returns, so the deferred policies cannot delete the workspace inside the
runner call. Instead the runner returns an `AgentRunLifecycleHandle` as
`AgentRunResult.journal`, wrapping the journal handle and owning the deferred
cleanup: the existing Stage 3/5 `record_accepted` / `record_rejected` calls
resolve the workspace disposition first and then delegate the mark. A handle
that is never finalized leaves the workspace and a pending manifest
disposition behind, so a caller crash conservatively keeps evidence rather
than deleting it. Test fakes stay unaffected: they return their own journals
or `None` and never see the handle.

Every manifest carries a strict `workspace_disposition`
(`pending`/`deleted`/`retained`) with bounded reason and timestamps, resolved
before the terminal manifest is written. Terminal manifests never carry a
`pending` disposition once a workspace was recorded; running and
finalization-pending manifests may. The disposition resolution runs even
without a durable journal: retained paths are always logged at INFO/WARNING
with a bounded reason so direct callers can locate them, and a caller crash
still leaves the workspace and its logged path behind.

Retention is never claimed for a path that is not there. Before recording
`retained`, the resolver opens the path with `O_RDONLY|O_DIRECTORY|O_NOFOLLOW`
and verifies the pinned descriptor: a vanished workspace (the agent deleted
its cwd, or cleanup partially removed it) is recorded and logged `deleted`; a
symlink or plain file at the workspace path is not the original directory and
is treated the same way — a symlink target is never followed and never
chmodded. A real directory must be owned by the effective user with root mode
0700; a mutated mode is restored through `fchmod` on the descriptor. After the
mode restore the current path is re-opened no-follow and its `(st_dev,
st_ino)` identity is compared with the pinned directory: if the path
disappeared, became a symlink or non-directory, or now holds a different
directory inode, the *current* path is classified truthfully and the original
is never recorded as a verified retained workspace. A workspace that cannot
be kept securely is removed instead (preferring safe deletion over accessible
evidence), except paths not owned by the effective user and replacement
directories not proven — by inode identity — to be the original workspace:
they may hold another user's data and are never deleted, only recorded
`retained` with a truthful bounded reason naming the unsafe state. When the
secure-delete fallback raises after partial work, the resolver re-inspects
the path and classifies its actual final state — missing, a retained
directory, or a symlink/non-directory replacement — before recording, so
`retained` is never recorded solely because removal raised; when re-securing
and removal both fail, a clear security error is logged and the truthful
existing state is recorded `retained` with the bounded reason. Under
`AgentCleanupError` the workspace may still be reachable by a possibly-live
process, so it is verified and re-secured but never removed, and the retained
state is recorded. Cleanup failures never misreport deletion: an accepted run
whose workspace deletion fails keeps the workspace, records `retained` with a
bounded reason, logs an error, and stays accepted — the durable record carries
exactly what happened. Retained workspaces stay mode 0700, keep their original
path (so a resumed OpenCode session still finds its cwd), contain no source
PDF, are logged, and may be removed manually after debugging. The config probe
and private config home directories keep their unconditional cleanup rules
under every policy.

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
