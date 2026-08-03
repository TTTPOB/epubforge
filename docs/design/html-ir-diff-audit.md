# HTML IR Diff Audit for Agent Revisions

## Status

Proposal for the next Stage 5 design iteration. This document records a
direction for discussion; the current implementation still requires
`bbox-repairs.json`, `asset-claims.json`, and `paddle-evidence.json` alongside
`corrected.html`.

When implemented, this design will replace the bbox-repair and asset-claim
sidecars. It does not define a compatibility path for existing revision
records. A revision contract and schema bump will make old Stage 5 outputs
stale and rerun them from the immutable Stage 4 workspace.

## Motivation

Stage 4 already produces an HTML intermediate representation with canonical
page geometry:

```html
<figure
  data-content-idx="83"
  data-page-idx="12"
  data-bbox="40,120,300,420">
```

The current Stage 5 contract makes the agent express one edit across several
files. It edits semantic HTML, selects Paddle candidates in
`bbox-repairs.json`, collects those candidates in `paddle-evidence.json`, and
uses `asset-claims.json` plus an HTML placeholder for a new image. Python then
applies the coordinate change that the agent intended.

This split preserves detailed candidate provenance, but it makes ordinary
editing indirect. The agent must keep HTML and multiple JSON documents in
sync. It also treats Paddle output as proof submitted to Python rather than a
local visual tool the agent can iterate until it accepts the result.

The proposed design makes `corrected.html` the complete desired state. The
agent edits semantic structure, text, reading order, bbox values, and image
crop requests directly in that IR. Python derives a typed diff from
`chapter.html` to `corrected.html`, audits the resulting state, materializes
generated resources, and writes the provenance record.

## Goals

- Give the revision agent one required editable output: `corrected.html`.
- Let the agent directly add, replace, remove, or retain canonical bboxes.
- Keep Paddle as an iterative workspace tool rather than a publication
  protocol.
- Derive revision evidence from the seed/final HTML diff instead of requiring
  the agent to duplicate edits in sidecar JSON.
- Express page-image crop requests in HTML and materialize them from the
  managed source PDF after validation.
- Reuse one deterministic audit implementation from Stage 5 publication and
  an agent-facing local audit CLI.
- Preserve source identity, page geometry, output bounds, generated-asset
  provenance, freshness, and recoverable publication.

## Non-Goals

- Python will not judge whether a visually plausible bbox is semantically the
  best one.
- The audit will not require a Paddle candidate ID or an agent-written reason
  for every geometry change.
- The agent will not receive, open, render, or otherwise access the source PDF.
- The design will not add a fixed planning checklist or a minimum edit count.
- The first implementation will not preserve revision schema 4 freshness.

## Proposed Stage 5 Contract

The isolated agent workspace keeps the current seed and evidence files:

```text
TASK.md
PADDLE.md
chapter.json
chapter.html
corrected.html
assets/
pages/page-NNNN.jpg
pages/page-NNNN.annotated.jpg
pages/page-NNNN.meta.json
```

The agent may create Paddle output, patch scripts, and other temporary files.
The runner collects only `corrected.html`. Python produces `revision.json` and
any generated image assets.

`chapter.html` remains immutable. `corrected.html` remains a complete HTML
document rather than a patch language. Existing source attributes retain
their meanings:

- `data-content-idx` connects retained content to normalized source items;
- `data-content-part` identifies ordered splits of one source item;
- `data-page-idx` identifies the zero-based source page;
- `data-bbox` stores `x0,y0,x1,y1` in canonical page coordinates.

The agent may change `data-bbox` directly. It may also add a bbox to a new or
existing element when the element identifies its source page. Python records
the change from the HTML diff.

## Direct Bbox Editing

An existing bbox repair becomes an ordinary HTML edit:

```html
<!-- chapter.html -->
<figure data-content-idx="83" data-page-idx="12"
        data-bbox="40,120,300,420">

<!-- corrected.html -->
<figure data-content-idx="83" data-page-idx="12"
        data-bbox="42.5,118,312,426">
```

The audit derives a record equivalent to:

```json
{
  "kind": "bbox-replaced",
  "data_content_idx": "83",
  "data_content_part": null,
  "page_idx": 12,
  "before": ["40", "120", "300", "420"],
  "after": ["42.5", "118", "312", "426"]
}
```

Python generates this record. The agent does not write it.

The diff distinguishes four geometry operations:

- `bbox-added`: the final element has a bbox and the matched seed element did
  not;
- `bbox-replaced`: both elements have bboxes and their normalized coordinates
  differ;
- `bbox-removed`: the seed element has a bbox and the final element does not;
- `bbox-retained`: normalized coordinates match and no revision entry is
  needed.

The first implementation should match source-grounded elements by the same
identity already used for validation: ordered `data-content-idx` tokens plus
optional `data-content-part`. A newly inserted element has no seed match; its
page and bbox are audited as final-state metadata without inventing a source
content identity.

## Bbox Audit

For every final bbox, Python checks:

- exactly four finite numeric coordinates;
- `x1 > x0` and `y1 > y0`;
- one unambiguous `data-page-idx` for the bbox-bearing element;
- a page index inside the chapter and source document;
- coordinates inside that page's canonical width and height;
- page metadata whose raster dimensions and conversion scale remain valid;
- source PDF `page.rect` dimensions matching the canonical page geometry when
  Python later uses the bbox to render an asset.

The audit accepts semantic geometry decisions that satisfy these invariants.
It does not reconstruct which Paddle candidates led to a bbox. Optional Paddle
evidence may appear in retained debugging workspaces, but publication does not
depend on it.

Python normalizes coordinate spelling before comparison so equivalent values
such as `42.5` and `42.5000` do not create a false diff. Publication may retain
the agent's valid spelling or rewrite all changed coordinates through the
existing canonical formatter; this remains an implementation decision.

## Paddle Geometry Output

The agent should not need to calculate raster/canonical scale by hand. Extend
`epubforge-paddle-page` with a repeated `--page-meta` argument paired with each
`--image` and `--page-idx`:

```sh
epubforge-paddle-page \
  --image pages/page-0012.jpg \
  --page-idx 12 \
  --page-meta pages/page-0012.meta.json \
  --output .paddle
```

When page metadata is supplied, every final candidate should expose both
spaces:

```json
{
  "id": "p0012-figure-3f2a9c1d4e",
  "type": "FIGURE",
  "raster_bbox": [120, 360, 950, 1280],
  "canonical_bbox": [42.5, 118, 312, 426]
}
```

The agent can iterate postprocessing and `patch_page(context)`, inspect the
latest annotated image, and copy the accepted `canonical_bbox` into HTML. The
stable candidate ID remains useful inside Paddle artifacts and debugging, but
Stage 5 does not require it in a final output.

The CLI must validate that each supplied page metadata file matches its image
dimensions and page index before emitting canonical coordinates.

## Generated Image Assets

HTML should carry both the resource request and its crop geometry. Put crop
metadata on `img`, because the image crop may exclude a figure caption or
other semantic children:

```html
<figure data-page-idx="12" data-bbox="40,110,320,455">
  <img
    src="claimed-assets/new-diagram.png"
    alt="Architecture diagram"
    data-page-idx="12"
    data-bbox="42.5,118,312,426">
  <figcaption>Architecture overview</figcaption>
</figure>
```

The `figure` bbox describes the semantic block. The `img` bbox describes the
source-PDF crop. Either may exist without the other when the final HTML remains
meaningful, but every `claimed-assets/` image must carry its own single page
index and bbox.

Python recognizes a safe `claimed-assets/<name>.png` placeholder as a crop
request. After auditing the image bbox, Python:

1. verifies the managed `source/source.pdf` snapshot against the Stage 4
   source hash;
2. verifies source page count and displayed page geometry;
3. renders the canonical clip at the configured generated-asset DPI;
4. enforces per-image pixel and byte limits plus a per-chapter aggregate
   limit;
5. writes a content-addressed PNG under `assets/generated/`;
6. replaces the placeholder `src` with the generated path;
7. re-parses and re-audits the materialized HTML;
8. records page, crop bbox, media type, size, digest, placeholder, and final
   path in `revision.json`.

Python installs generated assets before publishing HTML that references them.
A failure may leave an unreferenced content-addressed image, but it must never
publish HTML with a missing referenced asset. Successful publication removes
obsolete generated assets after the new HTML and revision record are durable.

## Diff-Derived Revision Record

Python should derive revision metadata from immutable input snapshots and the
validated final state. The next revision schema should record at least:

- seed and final HTML hashes;
- source workspace, chapter, page, and original asset hashes;
- agent identity, prompt, contract, and session fingerprints;
- source-content removals, splits, merges, and reordered elements where the
  existing source identity permits an unambiguous diff;
- bbox additions, replacements, and removals;
- generated asset provenance and digests;
- the final corrected HTML hash after generated path replacement.

The record may omit prose reasons. The HTML edit is the agent's decision; the
diff states what changed, and the audit states that the final state satisfies
the publication contract.

## Shared Audit Command

Expose the same deterministic audit core through an agent-facing command:

```sh
epubforge-revision-audit .
```

The command reads only files in the isolated workspace. It never accesses the
source PDF. Before publication it can check:

- corrected HTML structure and allowed local paths;
- source content identity, split, and merge rules;
- direct bbox changes and page bounds;
- claimed image placeholder shape and crop metadata;
- page metadata and raster/canonical consistency;
- whether every ordinary image reference names an available input asset.

It should emit short, location-oriented diagnostics suitable for an agent to
act on:

```text
ERROR content=83 data-bbox: x1 must be greater than x0
ERROR claimed-assets/new-diagram.png: missing data-page-idx on img
ERROR content=91 part=2/3: split parts are incomplete
```

Stage 5 calls the same audit library after the agent exits, then adds the
host-only source-PDF and publication checks. The CLI is a convenience for the
primary book-editor agent and remains useful if isolated subagents are enabled
later.

## Publication Sequence

The proposed host-side sequence is:

1. snapshot and validate immutable Stage 4 inputs;
2. invoke the isolated agent with pre-seeded `corrected.html`;
3. parse and audit the seed/final HTML diff;
4. render all valid `claimed-assets/` requests from the managed source PDF;
5. replace placeholders with content-addressed paths;
6. re-parse and audit final HTML plus generated files;
7. build `revision.json` from the diff and materialization records;
8. install generated files;
9. publish `corrected.html` and `revision.json` through the existing
   recoverable pair protocol;
10. remove obsolete generated assets as best-effort cleanup;
11. mark the agent run accepted.

Any failure before pair publication leaves the previous accepted HTML and
revision record untouched. Generated content-addressed orphans are safe to
reuse or remove on the next locked run.

## Migration Plan

1. Extract the current HTML parsing and final-state checks into a shared audit
   module without changing Stage 5 behavior.
2. Add seed/final bbox diff generation and tests for retained, added, replaced,
   removed, split, merged, and newly inserted elements.
3. Add `epubforge-revision-audit` over that shared module.
4. Extend `epubforge-paddle-page` to consume page metadata and emit canonical
   candidate bboxes.
5. Add HTML-native `claimed-assets/` recognition and source-PDF
   materialization from the `img` bbox.
6. Switch the agent contract to collect only `corrected.html`; remove
   `bbox-repairs.json`, `asset-claims.json`, and required
   `paddle-evidence.json` collection.
7. Derive revision records from the HTML diff, bump the revision contract and
   schema, and make schema 4 outputs stale.
8. Remove `data-asset-claim` after the new placeholder contract covers all
   generated asset identity needs.

Tests should keep the current publication, recovery, freshness, source-PDF
identity, symlink, output-bound, timeout, and workspace-retention coverage.
New tests should focus on direct IR edits and audit diagnostics rather than
exact agent reasoning or Paddle candidate selection.

## Tradeoffs

The design removes candidate-level proof from the publication contract.
`revision.json` can prove the seed/final bbox diff and the generated asset's
source page, clip, and digest, but it cannot prove that the agent chose the
same box as a particular Paddle candidate. This is an intentional change: the
model owns visual judgment, while Python owns validity and materialization.

Direct HTML editing also permits a valid in-bounds bbox that is visually poor.
The current sidecar contract cannot prevent a poor candidate choice either;
it only proves which candidate the agent selected. Quality continues to depend
on the agent inspecting the page and iterating its tools.

The shared audit implementation becomes a central contract. Stage 5 and the
local audit CLI must call the same code so an agent does not receive a local
pass followed by a different host-side structural rejection.

## Open Questions

- Should `bbox-removed` always pass, or should elements that still reference a
  source page retain a bbox when one existed in the seed?
- Should publication preserve valid agent coordinate spelling or normalize all
  changed bboxes through one formatter?
- Should generated-asset DPI remain fixed at 300, follow configuration, or be
  selectable through constrained HTML metadata?
- Should `claimed-assets/` support JPEG for photographic regions, or should
  the first contract keep PNG only?
- Should ordinary new local assets ever enter through the agent workspace, or
  should every new bitmap remain a source-PDF crop?
- Which structural changes belong in `revision.json`: every DOM move, only
  source-identity changes, or a bounded summary?
- Should optional retained Paddle evidence contribute hashes to
  `revision.json` for debugging without affecting acceptance?
- Should the audit CLI emit only human-readable diagnostics, JSON diagnostics,
  or both?

## Acceptance Criteria

The proposal is ready to implement after the open questions that affect the
HTML contract are resolved. The implementation is complete when:

- the agent can finish Stage 5 by editing `corrected.html` alone;
- direct existing and new-element bbox edits pass one shared audit path;
- the Paddle CLI exposes validated canonical candidate coordinates;
- a claimed image with an `img` page and bbox becomes a source-PDF crop;
- Python derives revision records without agent-authored repair or claim JSON;
- freshness detects HTML, bbox, and generated-asset tampering;
- publication never exposes HTML that references a missing generated asset;
- the full existing quality gates and new diff-audit tests pass.
