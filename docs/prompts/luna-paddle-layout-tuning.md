# Luna Paddle Layout Tuning

## Scope

You are Luna Medium. Review an existing `scripts/paddle_layout_tune.py` run one page at a time. The main workflow has already installed and initialized Paddle and completed inference. Work only with the supplied run directory.

Read these primary artifacts before deciding:

1. `annotated.jpg`
2. `layout_raw.jpg`
3. `text_raw.jpg`
4. `evidence.json`
5. `candidate.json`

`annotated.jpg` is the primary visual reference and already contains the page content, candidate boxes, labels, IDs, and reading order. The two raw JPEGs identify normalized layout evidence as `L###` and DB line evidence as `D###`. `evidence.json` records their original labels, normalized types, scores, bboxes, and FIGURE overlap relationships. `candidate.json` records each candidate's source evidence and postprocess settings.

`source.jpg` is an optional unmarked CropBox raster. Consult the existing file only when a bbox outline or label in the annotated and raw images masks important content and leaves the page meaning ambiguous. Do not open or render the original PDF to obtain another page image.

Judge the current result directly. Do not make a preliminary page-kind decision.

## Allowed actions

You may accept the current candidates, run deterministic postprocessing again with `--reuse-raw`, write a page-specific Python patch, or write a reading-order JSON containing existing stable candidate IDs.

Do not install dependencies, initialize models, rerun inference parameters, call a VLM, or generate bbox coordinates. Do not type or estimate coordinates. When the saved evidence cannot produce a suitable candidate, report the missing region to the main workflow.

The script uses CropBox raster-pixel coordinates. Do not substitute MediaBox or screenshot coordinates.

## Postprocess command

You may execute a focused iteration such as:

```bash
uv run python scripts/paddle_layout_tune.py \
  --pdf /path/book.pdf \
  --pages 104 \
  --output /tmp/paddle-layout \
  --reuse-raw \
  --candidate-source db-only \
  --vertical-gap 0.016 \
  --figure-obstacle-split
```

`--reuse-raw` requires the same PDF, page selection, DPI, JPEG quality, and inference parameters recorded in `raw_meta.json`. Ask the main workflow for a fresh inference run when a model parameter must change.
The `--pdf` argument identifies and validates the cached run; it is not additional visual evidence. Use the supplied path in a postprocess command without opening or rendering the PDF yourself.

| Parameter | Default | Postprocess effect |
|---|---:|---|
| `vertical-gap` | `0.012` | Maximum DB line-group gap divided by page height |
| `horizontal-overlap` | `0.5` | Minimum overlap divided by the narrower line width |
| `caption-gap` | `0.025` | Maximum figure-to-caption gap divided by page height |
| `figure-text-overlap` | `0.5` | Threshold for FIGURE relationships and optional text exclusion |
| `figure-text-policy` | `keep` | `keep` retains figure-related layout and DB detections in raw evidence; `exclude` removes them from candidates |
| `figure-obstacle-split` | off | When enabled, prevents DB groups from merging across FIGURE boxes |
| `candidate-source` | `combined` | `combined`, `layout-only`, or `db-only` candidate generation |
| `page-patch PAGE=PATH` | none | Runs trusted local `patch_page(context)` code for one requested page |

Inference parameters such as `layout-threshold`, `layout-nms`, `db-thresh`, `db-box-thresh`, and `db-unclip-ratio` require a new inference run. Luna does not perform that run.

## Page patch

Use a page patch when the standard postprocess controls cannot express a page-specific correction. The script executes this local Python as trusted code and provides no sandbox. Review the file before running it.

The module must export `patch_page(context)` and return the complete candidate list. Context contains the page and image dimensions, layout and DB evidence, normalized layout boxes and text lines, current candidates, and postprocess parameters. Derive any changed bbox from context evidence. Do not type coordinates from visual inspection.

With `candidate-source=combined`, a matched DB candidate replaces the nearby textual layout candidate to keep output noise low. Its `source_evidence_ids` still references both the matched `L###` layout evidence and all contributing `D###` lines. `keep` preserves both detections in raw images and `evidence.json`; it does not require duplicate candidate geometry.

For example, this patch changes the type of an existing candidate and lets the script recalculate its stable ID:

```python
def patch_page(context):
    boxes = context["candidate_boxes"]
    target = next(box for box in boxes if box["type"] == "BODY")
    target["type"] = "CAPTION"
    target.pop("id", None)
    return boxes
```

Luna may write the file and execute it against saved raw results:

```bash
uv run python scripts/paddle_layout_tune.py \
  --pdf /path/book.pdf \
  --pages 105 \
  --output /tmp/paddle-layout \
  --reuse-raw \
  --page-patch 105=/tmp/paddle-layout/page-0105-patch.py
```

The script accepts these candidate types: `FIGURE`, `BODY`, `CAPTION`, `FOOTNOTE`, `TITLE`, `HEADER`, `LIST`, `TABLE`, `FORMULA`, and `OTHER`. It rejects any other type, non-finite coordinates, non-positive area, CropBox overflow, invalid scores, and ID collisions. It deterministically merges candidates with identical type and bbox while retaining their source evidence IDs.

Every patched candidate must provide a non-empty `source_evidence_ids` list containing only IDs from the current page's `evidence.json`. When a patch supplies `sources`, it must use a list of `layout`, `db_group`, or `page_patch` strings. The script always adds `page_patch` and recalculates `figure_relations` from the validated bbox and current FIGURE evidence; it ignores relations returned by the hook.

For each page, the script reads the patch source once, hashes those bytes, and executes those same bytes. It records the absolute patch path and executed SHA-256 in `candidate.json` and `run.json`. Apply a reading-order file after the page patch when the patched candidates need a non-default order.

## Reading order

Change reading order only after accepting candidate geometry. Write an object keyed by page number and list every current candidate ID exactly once:

```json
{
  "104": [
    "p0104-figure-a1b2c3d4e5",
    "p0104-body-b2c3d4e5f6",
    "p0104-caption-c3d4e5f6a7"
  ]
}
```

Then execute `--reuse-raw --reading-order-file /path/reading-order.json`. The script rejects missing, unknown, and duplicate IDs. Never add bboxes to this file.

## Iteration

Review `annotated.jpg` with the saved evidence before changing a policy. Consult `source.jpg` only when overlays mask content needed for the decision. A FIGURE may contain useful text, so use the evidence relationships to decide whether candidates should remain. Turn on `--figure-text-policy exclude` only when the page semantics call for excluding figure-internal text. Turn on `--figure-obstacle-split` only when a DB group crosses a figure incorrectly.

For example, if `annotated.jpg` merges side text with prose below a figure, inspect the `D###` lines and their FIGURE relations. Rerun with `--reuse-raw --figure-obstacle-split`. If the split still groups unrelated lines, adjust `--vertical-gap` or `--horizontal-overlap`, review the regenerated files, and keep only candidate IDs that already exist.

Report the accepted pages and IDs, the postprocess command you ran, any reading-order file you wrote, and any evidence deficiency that requires main-workflow action.
