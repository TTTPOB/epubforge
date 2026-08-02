# MinerU-Luna Chapter Workflow

## Design

epubforge keeps the ingestion path linear:

```text
PDF
  -> MinerU archive
  -> normalized content and page geometry
  -> Luna chapter boundaries
  -> deterministic chapter HTML and annotated page JPEGs
  -> Luna corrected HTML
```

The pipeline owns PDF access. An agent starts after workspace preparation and
receives one chapter's `chapter.html`, `chapter.json`, annotated page JPEGs,
and the root manifest. The agent never opens or renders the PDF.

## Contracts

Stage 1 stores the untouched MinerU response at `01_raw.zip` and records the
source PDF hash, page count, and publication metadata in `source/source_meta.json`.
Stage 2 reads the archive with strict ZIP and JSON validation. It writes
`02_content/content.json`, whose `items` retain MinerU content order and whose
`page_geometry` uses top-left coordinates in displayed page orientation.

Stage 3 sends a compact content projection to the configured Luna model. The
model returns typed boundary records with content index, page index, title,
kind, confidence, and evidence. The program checks each record against the
normalized content before writing `03_chapters/chapters.json`.

Stage 4 maps each boundary range to a deterministic chapter workspace. The
HTML preserves `data-content-idx`, `data-page-idx`, and `data-bbox` attributes.
The JPEG files show the corresponding page evidence and labels. The manifest
records chapter ranges, source hashes, and file hashes.

Stage 5 sends the complete chapter HTML and its page JPEGs to Luna. The model
may repair text, tags, DOM order, tables, captions, and footnotes while
retaining source IDs and valid bboxes. It must return complete HTML. The
program rejects unknown content IDs, changed bbox values, unsafe tags,
malformed splits, and invalid references. It then publishes `corrected.html`
and `revision.json` as one pair.

## Caching And Failure Handling

Luna requests use the shared on-disk cache keyed by model, prompt, response
schema, and request settings. The client validates cached structured responses
before use and writes replacements atomically. Stale stage outputs rebuild
from their source contract. A failed chapter revision leaves the last complete
HTML and revision record in place; a batch stops conservatively unless the
caller passes `--continue-on-error`.

## Paddle Evidence Tool

The standalone `scripts/paddle_layout_tune.py` script remains a supported
layout-evidence tool. It can render selected pages, cache Paddle layout and
text detections, generate stable candidate IDs, and apply deterministic
postprocessing. Its Luna prompt lives in
`docs/prompts/luna-paddle-layout-tuning.md`.

Paddle evidence does not change the chapter contracts by itself. A later
pipeline stage must consume any accepted candidate output explicitly.
