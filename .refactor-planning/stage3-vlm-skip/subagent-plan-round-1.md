# Stage 3 Skip-VLM Implementation Plan - Round 1

## Scope

本轮目标不是新增 Docling。Docling 已经是 Stage 1 parse 的固定入口。本轮要做的是让 Stage 3 的 VLM extract 变成可显式跳过的路径：

- 默认行为保持现状：`run` / `extract` 继续走 VLM Stage 3。
- 显式开启 skip-VLM 后，Stage 3 只读取 `01_raw.json` 与 `02_pages.json`，把 Docling 输出转换为 Stage 3 unit，不做任何 LLM/VLM API 调用。
- 下游 `assemble`、`build`、`editor`、`audit` 必须能看出该书来自 skip-VLM 路径，并且不能把旧 VLM 产物混入本次结果。
- agentic editor 不是 Stage 3 正确性的兜底。Stage 3 skip-VLM 仍要满足可执行的最小语义合同；editor 只负责后续复核和修正。

## Current Code Facts

已核对的实现点：

- `src/epubforge/pipeline.py::run_extract()` 当前无条件导入并调用 `epubforge.extract.extract()`。
- `src/epubforge/cli.py::run()` 和 `extract()` 当前无条件调用 `cfg.require_llm()` 与 `cfg.require_vlm()`。
- `src/epubforge/extract.py` 当前把 Stage 3 产物写入调用方给定的 `out_dir`，包括 `unit_*.json`、`audit_notes.json`、可选 `book_memory.json`。
- `src/epubforge/assembler.py::assemble()` 当前直接扫描 `work/<book>/03_extract/unit_*.json`，这是 mode 切换和 `--pages` 切换时串读旧产物的主要风险。
- `assembler._parse_block()` 当前把非 `llm_group` unit 全部标记为 `Provenance.source="vlm"`。
- `Provenance.source` 当前只允许 `"llm" | "vlm" | "passthrough"`。
- `epub_builder._map_figures_to_images()` 当前按同页 Figure 顺序绑定排序后的 PNG 文件，不优先使用 `Figure.image_ref`。
- 当前 editor prompt 不携带 Stage 3 提取模式、复杂页列表或 page image 位置。
- 本地依赖为 `docling-core 2.74.0`、`docling 2.90.0`。
- `docling_core.types.doc.document.RefItem` 有 `cref` 字段和 `resolve(doc)` 方法。
- `TableItem.export_to_html(doc, add_caption=False)` 的源码忽略 `add_caption`，所以必须在 epubforge 侧移除或拆分 `<caption>`。
- 当前 `DocItemLabel` 包含：
  `CAPTION, CHART, FOOTNOTE, FORMULA, LIST_ITEM, PAGE_FOOTER, PAGE_HEADER, PICTURE, SECTION_HEADER, TABLE, TEXT, TITLE, DOCUMENT_INDEX, CODE, CHECKBOX_SELECTED, CHECKBOX_UNSELECTED, FORM, KEY_VALUE_REGION, GRADING_SCALE, HANDWRITTEN_TEXT, EMPTY_VALUE, PARAGRAPH, REFERENCE, FIELD_REGION, FIELD_HEADING, FIELD_ITEM, FIELD_KEY, FIELD_VALUE, FIELD_HINT, MARKER`。

## Implementation Details Already Determined

### Naming

Use skip-VLM terminology in user-facing API and logs. Avoid `docling-direct` as the primary name because Docling is already Stage 1.

Code names:

- New extraction module: `src/epubforge/extract_skip_vlm.py`
- Config field: `ExtractSettings.skip_vlm: bool = False`
- Env var: `EPUBFORGE_EXTRACT_SKIP_VLM`
- CLI option: `--skip-vlm / --no-skip-vlm`
- Unit kind for skip-VLM units: `"docling_page"`
- IR provenance source: `"docling"`

### Config

Modify `src/epubforge/config.py`:

- Add `skip_vlm: bool = False` to `ExtractSettings`.
- Add env mapping:

```python
("EPUBFORGE_EXTRACT_SKIP_VLM", "extract", "skip_vlm", _bool_env)
```

Precedence stays consistent with existing config:

1. CLI option
2. env override
3. explicit TOML
4. default

`config.example.toml` and `docs/usage.md` must document that `[extract] skip_vlm = true` skips pipeline VLM calls and therefore does not require provider credentials for ingestion stages 1-4.

### CLI

Modify `src/epubforge/cli.py`:

- Add `--skip-vlm / --no-skip-vlm` to `run`.
- Add `--skip-vlm / --no-skip-vlm` to `extract`.
- Add `--pages` to `extract` for parity with `run`, using the existing `_parse_pages()`.
- Apply CLI override by copying the nested config:

```python
cfg = cfg.model_copy(update={
    "extract": cfg.extract.model_copy(update={"skip_vlm": skip_vlm})
})
```

Provider validation:

- `run`: require provider credentials only if Stage 3 will run and `cfg.extract.skip_vlm` is false.
  - `from_stage <= 3 and not skip_vlm`: keep current conservative behavior and call both `require_llm()` and `require_vlm()`.
  - `from_stage > 3`: do not require provider credentials.
  - `skip_vlm=True`: do not require provider credentials.
- `extract`: require provider credentials only when `skip_vlm=False`.
- `parse`, `classify`, `assemble`, `build`, and editor commands remain provider-key free.

Startup logging should include `skip_vlm=<bool>` so a log file is self-describing.

### Stage 3 Artifact Isolation

Do not keep writing active units directly under `03_extract/`. Introduce an explicit active manifest.

New layout for newly generated Stage 3 output:

```text
work/<book>/03_extract/
  active_manifest.json
  artifacts/
    <artifact_id>/
      manifest.json
      unit_0000.json
      unit_0001.json
      audit_notes.json
      book_memory.json
```

`artifact_id` is deterministic and should be the first 16 hex chars of a sha256 over:

- manifest schema version
- extraction mode: `"vlm"` or `"skip_vlm"`
- sha256 of `01_raw.json`
- sha256 of `02_pages.json`
- selected non-TOC pages after applying `--pages`
- page filter value, or null
- relevant extract settings:
  - for VLM: VLM model, base URL, `vlm_dpi`, simple/complex batch limits, `enable_book_memory`
  - for skip-VLM: skip-VLM contract version

Manifest schema version 1:

```json
{
  "schema_version": 1,
  "stage": 3,
  "mode": "skip_vlm",
  "artifact_id": "0123456789abcdef",
  "artifact_dir": "artifacts/0123456789abcdef",
  "created_at": "2026-04-24T00:00:00Z",
  "raw_sha256": "...",
  "pages_sha256": "...",
  "selected_pages": [1, 2, 4],
  "toc_pages": [3],
  "page_filter": [1, 2, 3, 4],
  "unit_files": [
    "artifacts/0123456789abcdef/unit_0000.json",
    "artifacts/0123456789abcdef/unit_0001.json"
  ],
  "sidecars": {
    "audit_notes": "artifacts/0123456789abcdef/audit_notes.json",
    "book_memory": "artifacts/0123456789abcdef/book_memory.json"
  },
  "settings": {
    "skip_vlm": true,
    "contract_version": 1
  }
}
```

Implementation mechanics:

- Add a small helper module, for example `src/epubforge/stage3_artifacts.py`, with:
  - `Stage3Manifest` Pydantic model
  - `build_stage3_manifest(...)`
  - `write_stage3_manifest_atomic(...)`
  - `load_active_stage3_manifest(work_dir)`
  - `resolve_unit_files(work_dir, manifest)`
- `pipeline.run_extract()` computes the artifact directory before calling the extractor.
- The extractor writes only inside that artifact directory.
- After successful extraction, `pipeline.run_extract()` writes `manifest.json` inside the artifact dir and atomically replaces `03_extract/active_manifest.json`.
- If extraction fails, the previous active manifest remains untouched.
- `assemble()` reads `active_manifest.json` when present and only reads the listed unit files.
- Legacy fallback remains: if no active manifest exists, `assemble()` falls back to current `03_extract/unit_*.json` scanning for existing workdirs.
- If neither manifest units nor legacy root units exist, `assemble()` fails fast instead of silently writing an empty book.

This solves:

- VLM to skip-VLM switching
- skip-VLM to VLM switching
- `--pages` changes
- TOC filtering changes
- sidecar contamination from `book_memory.json` and `audit_notes.json`

### Stage 3 Unit Contract

Skip-VLM units use the existing unit JSON shape plus explicit metadata:

```json
{
  "unit": {
    "kind": "docling_page",
    "pages": [42],
    "page_kinds": ["complex"],
    "extractor": "skip_vlm",
    "contract_version": 1
  },
  "first_block_continues_prev_tail": false,
  "first_footnote_continues_prev_footnote": false,
  "blocks": [],
  "audit_notes": []
}
```

Contract:

- One selected non-TOC page becomes one unit.
- Unit order follows `02_pages.json` order after TOC and `--pages` filtering.
- Continuation signals may only span adjacent selected physical pages.
- If `--pages` creates a gap, continuation must be broken across that gap.
- Ordinary footnotes must have non-empty `callout`.
- `callout=""` is reserved for true continuation footnote text only.
- Raw inline callouts in paragraph text, table HTML, `table_title`, and `caption` must be preserved.
- Tables must include `html`; empty table HTML is a contract error for pages containing a Docling table.
- Table continuation must be signaled with block field `continuation=true` on continuation fragments.
- Unit `audit_notes` may include deterministic extraction warnings, but downstream correctness must not depend on humans reading those notes.

### New Skip-VLM Extractor

Create `src/epubforge/extract_skip_vlm.py`:

```python
def extract_skip_vlm(
    raw_path: Path,
    pages_path: Path,
    out_dir: Path,
    *,
    force: bool = False,
    page_filter: set[int] | None = None,
) -> list[Path]:
    ...
```

The function returns unit paths relative to `out_dir` or absolute paths; the artifact helper can normalize these for the manifest.

Key implementation details:

- Load `DoclingDocument` from `01_raw.json`.
- Load page data from `02_pages.json`.
- Filter TOC pages.
- Apply `page_filter` after classification data is loaded.
- Use `doc.iterate_items(page_no=pno)` for page reading order.
- Dispatch by `item.label`, not by `isinstance`.
- Use `RefItem.resolve(doc)` for caption, footnote, and reference dereferencing.
- Include `bbox` from `item.prov[0].bbox` when available.
- Write `audit_notes.json` for skip-VLM too, even if it is `[]`.
- Write `book_memory.json` as an empty `BookMemory` for skip-VLM, so sidecar state is isolated and explicit.

### Label Coverage

Use this mapping in skip-VLM:

| DocItemLabel | Output | Notes |
|---|---|---|
| `TITLE` | `heading` | `level=1` |
| `SECTION_HEADER` | `heading` | use `item.level` if present, clamp `1..6`, fallback `2` |
| `TEXT` | `paragraph` | preserve text |
| `PARAGRAPH` | `paragraph` | preserve text |
| `REFERENCE` | `paragraph` | preserve text |
| `HANDWRITTEN_TEXT` | `paragraph` | preserve text and emit extraction warning |
| `LIST_ITEM` | `paragraph` | prefix marker when `item.marker` is present |
| `CODE` | `paragraph` | preserve text; IR has no code block |
| `FORMULA` | `equation` | `latex=item.text` |
| `FOOTNOTE` | `footnote` | run callout extraction and page-local merge |
| `TABLE` | `table` | export and normalize HTML, extract title/caption/footnotes |
| `PICTURE` | `figure` | derive `image_ref`, resolve caption |
| `CHART` | `figure` | same as picture |
| `CAPTION` | consumed or `paragraph` | skip only when referenced by a table/figure; unreferenced captions become `paragraph` with `role="caption"` |
| `MARKER` | context helper | use as nearby footnote/list marker when possible; otherwise skip and emit warning |
| `PAGE_HEADER` | skip | running header |
| `PAGE_FOOTER` | skip | running footer |
| `DOCUMENT_INDEX` | skip | TOC pages already filtered; skip if encountered |
| `FORM` | skip container | preserve child/field text separately |
| `KEY_VALUE_REGION` | skip container | preserve child/field text separately |
| `FIELD_REGION` | skip container | preserve child/field text separately |
| `FIELD_HEADING` | `paragraph` if text | preserve text |
| `FIELD_ITEM` | `paragraph` if text | preserve text |
| `FIELD_KEY` | `paragraph` if text | preserve text |
| `FIELD_VALUE` | `paragraph` if text | preserve text |
| `FIELD_HINT` | `paragraph` if text | preserve text |
| `CHECKBOX_SELECTED` | `paragraph` if text | prefix `[x] ` |
| `CHECKBOX_UNSELECTED` | `paragraph` if text | prefix `[ ] ` |
| `GRADING_SCALE` | `paragraph` if text | preserve text and emit warning |
| `EMPTY_VALUE` | skip | no book content |

Unsupported or unknown future labels:

- If `getattr(item, "text", "")` is non-empty, preserve as `paragraph` and emit an `audit_note` with kind `"other"`.
- If no text, skip and emit debug log only.

### Caption Resolution

Use `RefItem.resolve(doc)`:

```python
def _resolve_ref_texts(refs: Sequence[RefItem], doc: DoclingDocument) -> list[str]:
    texts = []
    for ref in refs or []:
        item = ref.resolve(doc)
        text = getattr(item, "text", "")
        if text:
            texts.append(text)
    return texts
```

Track consumed caption refs by `cref` so standalone `CAPTION` items are skipped only when attached to a table or figure.

### Footnote Handling

Implement deterministic helpers inside `extract_skip_vlm.py`:

- `_split_callout(text: str) -> tuple[str, str] | None`
- `_is_incomplete_note_text(text: str) -> bool`
- `_merge_page_footnotes(blocks: list[dict]) -> list[dict]`
- `_detect_footnote_continuation(prev_page_state, current_blocks) -> bool`

Callout regex should cover:

- circled digits: `①` through `⑳`
- Arabic digits with optional punctuation: `1`, `1.`, `1)`, `[1]`
- symbols: `*`, `†`, `‡`, `§`, `¶`, `#`, `‖`
- lowercase alphabetic markers when followed by whitespace or punctuation

Rules:

- A footnote whose text starts with a callout becomes `{"callout": callout, "text": stripped_text}`.
- Consecutive same-page footnote items with no callout are appended to the previous incomplete footnote.
- A first footnote on page N with no callout may become a continuation only when page N-1 is an adjacent selected page and the previous selected page ends with an incomplete footnote.
- Empty callout outside that continuation case is a skip-VLM contract error in strict mode.
- The raw callout in body text is never removed. Only the footnote body block has its leading callout stripped.

Strict mode is the v1 implementation. A lossy mode can be added later only if humans decide that malformed ordinary footnotes should still produce an EPUB.

### Paragraph Continuation

Set `first_block_continues_prev_tail=True` only when all are true:

- Previous selected page and current page are physically adjacent.
- Previous page's last meaningful non-footnote block is a paragraph.
- Current page's first meaningful non-footnote block is a paragraph.
- Previous paragraph text does not end with terminal punctuation:
  `。！？…；.!?;:`
- Current paragraph does not look like a new section start:
  no heading label, no list marker, and does not match a conservative chapter/section prefix such as `第.+章`, `Chapter `, or all-caps short heading.

When true, `assembler._append_to_last_paragraph()` keeps existing behavior and marks the merged paragraph `cross_page=True`.

### Table Handling

For every `TABLE` item:

- Export HTML using `item.export_to_html(doc)`.
- Normalize the exported HTML:
  - remove `<caption>...</caption>` from the table HTML
  - use removed caption text only if Docling `captions` did not already provide a title/caption
  - preserve `<thead>`, `<tbody>`, `colspan`, `rowspan`, and cell text
  - fail contract if no `<table` or no row/cell content is present
- Resolve `item.captions` and classify:
  - first caption matching `^(表|Table)\s*[\dIVXLC一二三四五六七八九十]` becomes `table_title`
  - if no match, first caption becomes `table_title` when it starts with a table-like prefix; otherwise it becomes `caption`
  - remaining caption strings join into `caption`
- Resolve `item.footnotes` and `item.references` when present:
  - append source/provenance notes such as `资料来源` / `Source:` into `caption`
  - preserve raw footnote callouts in those strings
- Compute `continuation` for the first table on a page when previous selected adjacent page ends with a table-like block.

Continuation rule:

- Previous selected page's last meaningful block must be a table.
- Current page's first meaningful block must be a table.
- Column count must be compatible:
  - prefer `TableItem.data.num_cols` when available
  - otherwise use modal logical HTML row width
  - reject continuation when relative drift is greater than 25%
- Current table should have no distinct new `table_title`, or its title matches the previous table title after whitespace normalization.
- If accepted, mark current table block `continuation=true`.
- Stage 4 remains responsible for merging continuation tables into `multi_page=True` and creating `merge_record`.

### Figure Handling

Derive `image_ref` exactly from parser output:

```python
ref_id = item.self_ref.replace("/", "_").replace("#", "_").lstrip("_")
page = item.prov[0].page_no if item.prov else page_no
image_ref = f"images/p{page:04d}_{ref_id}.png"
```

Resolve captions with `item.captions`.

Build impact is required: update `_map_figures_to_images()` so `Figure.image_ref` is the primary binding. The old per-page ordinal fallback remains only for legacy IR without `image_ref`.

### Pipeline Changes

Modify `src/epubforge/pipeline.py`:

- `run_extract()` chooses the mode from `cfg.extract.skip_vlm`.
- It computes an artifact directory and passes that directory as `out_dir`.
- VLM path continues to call `epubforge.extract.extract(...)`.
- Skip path calls `epubforge.extract_skip_vlm.extract_skip_vlm(...)`.
- Stage logs:
  - `Stage 3: extracting (VLM)...`
  - `Stage 3: extracting (skip-VLM from Docling)...`
- After successful extraction, write the active manifest.
- The final log line points at the active manifest, not only the directory.

`run_all()` ordering stays parse → classify → extract → assemble.

### Assemble Changes

Modify `src/epubforge/assembler.py`:

- Load active Stage 3 manifest if present.
- Read only manifest-listed unit files.
- Fall back to legacy `03_extract/unit_*.json` when no manifest exists.
- Add unit source mapping:

```python
UNIT_SOURCE = {
    "llm_group": "llm",
    "vlm_group": "vlm",
    "docling_page": "docling",
}
```

- Unknown unit kind should fail fast with a clear error.
- `_parse_block()` should use `raw.get("bbox")` in `Provenance`.
- `_parse_block()` should propagate paragraph `role`, `display_lines`, and `style_class` when present; this supports unreferenced captions without needing a new IR block type.
- `_pair_footnotes()` should scan `Table.caption` in addition to `Table.html` and `Table.table_title`.
- When replacing markers in tables, update whichever of `html`, `table_title`, or `caption` contains the raw callout.
- Comments and log messages that say empty callout is always a VLM contract should be rewritten to say it is a Stage 3 contract for continuation footnotes.

### IR / Schema Changes

Modify `src/epubforge/ir/semantic.py`:

- Extend `Provenance.source` to:

```python
Literal["llm", "vlm", "docling", "passthrough"]
```

This is backward compatible for existing JSON and removes the provenance debt from pretending skip-VLM output is VLM output.

No new block kind is required.

### Build Changes

Modify `src/epubforge/epub_builder.py`:

- `_map_figures_to_images()` first checks `fig.image_ref`.
  - Strip leading `images/` and resolve under `images_dir`.
  - If the referenced PNG exists, use it.
  - Register each image once.
- If `image_ref` is absent or missing on disk, fall back to current per-page ordinal matching and log a warning.
- `_render_chapter()` should apply footnote markers to `Table.caption` the same way it already does for table HTML and title.

This is required for skip-VLM because same-page multi-figure correctness cannot be proven by filename formula alone while build ignores `image_ref`.

### Editor Changes

Modify editor state metadata so subagents can see extraction context.

Extend `EditorMeta` in `src/epubforge/editor/state.py` with optional defaults:

```python
class Stage3EditorMeta(BaseModel):
    mode: str = "legacy"
    skipped_vlm: bool = False
    manifest_path: str | None = None
    selected_pages: list[int] = []
    complex_pages: list[int] = []
    page_images_dir: str | None = None
    extraction_warnings_path: str | None = None

class EditorMeta(BaseModel):
    initialized_at: str
    uid_seed: str
    stage3: Stage3EditorMeta = Field(default_factory=Stage3EditorMeta)
```

`run_init()`:

- Load `03_extract/active_manifest.json` when available.
- Load `02_pages.json` and derive complex selected pages.
- Write stage3 metadata into `edit_state/meta.json`.
- Copy or reference Stage 3 `audit_notes.json` in `edit_state/audit/extraction_notes.json`.

`render_prompt()`:

- Load `meta.json`.
- Include a compact extraction context block in scanner/fixer/reviewer prompts:
  - extraction mode
  - whether VLM was skipped
  - selected pages
  - complex pages in the current chapter when derivable from block provenance
  - `work/images` path for page/figure assets
  - extraction warnings path

`doctor`:

- If `stage3.skipped_vlm` is true, add `needs_scan` hints for chapters containing pages that Stage 2 classified as complex.
- These hints do not assert an error. They direct scanner/fixer attention to pages that did not receive VLM layout analysis.

No built-in editor VLM command is required for v1. The editor becomes aware enough for a multimodal agent to inspect images/PDF context and propose ops. A first-class selective VLM editor command is listed below as a human design decision.

### Audit Changes

Modify audit detectors:

- `detect_footnote_issues()` should flag leftover `Footnote(callout="")` as an issue unless the block was already merged away before audit. Suggested code: `footnote.empty_callout_body`.
- `detect_footnote_issues()` should keep existing raw-callout residue behavior.
- `detect_table_merge_issues()` already catches orphan continuation tables and can stay as-is.
- `detect_table_issues()` already catches malformed table HTML and can stay as-is.
- `doctor` should surface skip-VLM complex-page scan hints as described above.

Stage 3 `audit_notes.json` remains a sidecar; it is not a replacement for Book-level audit detectors.

### Cache Behavior

- Skip-VLM does not use `LLMClient`, so no request cache entries are read or written.
- Existing VLM cache keys remain unchanged because VLM request messages and response schema do not change.
- Artifact manifests are not LLM cache entries. They are run-output manifests.
- Prompt caching settings remain relevant only for VLM/LLM calls. Logs should show zero requests for skip-VLM Stage 3.

### Logging and Observability

Required log additions:

- Startup banner includes `skip_vlm=<bool>`.
- Stage 3 start line includes mode.
- Extract summary includes:
  - mode
  - unit count
  - selected page range or explicit list summary
  - complex page count
  - artifact id
  - active manifest path
- Skip-VLM per-unit log line:

```text
extract skip-vlm unit 3/20 page=42 kind=complex blocks=17 warnings=1 reused=N
```

- VLM per-unit log line can remain current but should write under isolated artifact dir.
- `stage_timer` for skip-VLM should naturally show no LLM/VLM usage summary.

### Documentation

Update:

- `docs/usage.md`
  - Stage 3 can be VLM or skip-VLM.
  - Show `uv run epubforge run book.pdf --skip-vlm`.
  - Explain that skip-VLM requires existing parse/classify outputs if running `extract` directly.
  - Explain active manifest and new `03_extract/artifacts/` layout.
- `config.example.toml`
  - Add `skip_vlm = false`.
- `AGENTS.md`
  - Update Stage 3 description from mandatory LLM+VLM blocks to VLM or skip-VLM units.
  - Add `EPUBFORGE_EXTRACT_SKIP_VLM` to env whitelist.
  - Add `Provenance.source="docling"`.

### Migration Compatibility

Backward compatible behavior:

- Default config remains VLM.
- Existing workdirs with legacy `03_extract/unit_*.json` still assemble when no `active_manifest.json` exists.
- Existing Semantic IR with `"llm"`, `"vlm"`, or `"passthrough"` provenance still validates.
- Existing editor `meta.json` without `stage3` still validates because the new field has a default.
- Existing EPUB build behavior remains for figures without `image_ref` due to ordinal fallback.

Intentional behavior changes:

- Newly generated Stage 3 outputs no longer write root-level active units under `03_extract/`.
- `assemble()` fails on unknown unit kind instead of silently treating it as VLM.
- `assemble()` fails when no active or legacy unit files exist.
- `run --from 4` no longer requires provider API keys.
- `run/extract --skip-vlm` no longer requires provider API keys.

### Failure Modes

Skip-VLM must fail fast with a clear error for:

- missing `01_raw.json`
- missing `02_pages.json`
- selected pages empty after TOC/page filtering
- `doc.iterate_items(page_no=...)` yields no content for a page that `02_pages.json` says has element refs
- ordinary footnote has no callout after deterministic extraction and is not a valid continuation
- Docling table produces empty or non-table HTML
- manifest unit file listed in `active_manifest.json` does not exist
- active manifest has a different schema version
- unknown unit kind during assemble

Skip-VLM may continue with warning notes for:

- unknown future Docling labels with no text
- handwritten text preserved as paragraph
- unreferenced captions preserved as caption-role paragraphs
- marker labels that cannot be associated with a footnote/list item
- uncertain table continuation that fails the compatibility test

When strict skip-VLM fails, the user-visible message should name the page and suggest either rerunning without `--skip-vlm` or fixing the Docling-derived unit after a future lossy mode exists.

## Test Plan

### Config and CLI

- `load_config()` reads `[extract] skip_vlm = true`.
- `EPUBFORGE_EXTRACT_SKIP_VLM=1` overrides TOML.
- CLI `--skip-vlm` overrides config false.
- CLI `--no-skip-vlm` overrides config true.
- `run --from 4` does not require API keys.
- `run --skip-vlm` does not require API keys.
- default `run` still requires provider keys.
- `extract --pages 1,3-4 --skip-vlm` passes page filter to `pipeline.run_extract()`.

### Artifact Isolation

- VLM extraction writes units under one artifact dir and updates active manifest.
- Skip-VLM extraction writes units under a different artifact dir and updates active manifest.
- Switching VLM → skip-VLM → VLM never causes `assemble()` to read inactive unit files.
- Changing `--pages` changes artifact id and active unit file list.
- Failed extraction does not replace the previous active manifest.
- Legacy `03_extract/unit_*.json` still assembles when no manifest exists.

### Skip-VLM Extract Unit Tests

Use synthetic `DoclingDocument` fixtures that exercise real `body.children` / `iterate_items(page_no=...)`, not only flat `doc.texts`.

Cover:

- label mapping table
- TOC filtering
- page filter
- item order across text/table/picture
- caption ref resolution via `RefItem.resolve(doc)`
- consumed vs unreferenced captions
- image_ref derivation matches parser naming
- ordinary footnote callout extraction
- same-page split footnote merge
- valid cross-page footnote continuation
- invalid empty ordinary footnote fails strict mode
- paragraph continuation only across adjacent selected pages
- table HTML caption removal
- table title/caption/source extraction
- table continuation true/false cases
- unknown label fallback

### Assemble and IR

- `docling_page` maps to `Provenance.source="docling"`.
- unknown unit kind fails.
- raw bbox becomes `Provenance.bbox`.
- paragraph role is preserved.
- table caption participates in footnote pairing.
- empty-callout leftover footnote becomes audit issue.
- table continuation from skip-VLM still produces `multi_page=True` and `merge_record`.

### Build

- `Figure.image_ref` resolves the correct image when multiple images share a page.
- ordinal fallback still works for legacy figures without `image_ref`.
- table caption footnote markers render into links.

### Editor and Audit

- `editor init` writes `meta.stage3` when active manifest exists.
- old meta without `stage3` still loads.
- `render-prompt` includes skip-VLM extraction context.
- `doctor` emits `needs_scan` hints for skip-VLM complex pages.
- Stage 3 extraction warnings are available under `edit_state/audit/`.

### End-to-End

- A no-API-key run with `--skip-vlm` can execute stages 1-4 on a small fixture.
- The resulting `05_semantic_raw.json` contains docling provenance.
- `editor init` works on the skip-VLM semantic output.
- `build` works after editor init.

## Human Design Decisions Remaining

Only these require human/product direction. The implementation above does not block on them.

### 1. Should v1 expose a lossy skip-VLM mode?

Recommended default: no. Implement strict mode only.

Strict mode fails when an ordinary footnote callout or table HTML cannot be recovered, because silently producing broken footnotes/tables undermines existing assemble, audit, and EPUB behavior.

Possible future option:

```text
--skip-vlm-allow-lossy
```

That would write best-effort units and force editor review, but it is a product-quality decision rather than an engineering necessity for v1.

### 2. Should editor get a first-class selective VLM command?

Recommended default for v1: no. Add extraction metadata and prompt context only.

A later design could add a command such as:

```text
epubforge editor vlm-page work/<book> --page 42
```

That command would need its own API-key contract, cache behavior, result schema, and op proposal flow. It should be designed as an editor tool, not as a hidden dependency of Stage 3 skip-VLM.

## Suggested Implementation Order

1. Add config/CLI flag and provider-gating tests.
2. Add Stage 3 artifact manifest helper and migrate VLM path writes into artifact dirs.
3. Update `assemble()` to read active manifest with legacy fallback.
4. Add IR source `"docling"` and source mapping tests.
5. Implement `extract_skip_vlm.py` with strict contract checks.
6. Fix build image binding to prefer `Figure.image_ref`.
7. Add table caption footnote pairing/rendering support.
8. Add editor metadata/prompt/doctor context.
9. Update docs and AGENTS.md.
10. Run unit tests plus one no-API-key skip-VLM end-to-end fixture.
