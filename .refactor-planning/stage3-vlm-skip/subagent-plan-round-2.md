# Stage 3 可跳过 VLM 实施计划 - Round 2

## 目标与边界

本计划替代 round 1 计划中的可执行部分，但不修改 round 1 文件。目标是让 Stage 3 的 VLM extract 可显式跳过，同时让 editor/agent 在后续发现复杂页时可以选择：

- 使用自身多模态能力查看页面图像并提出编辑 op。
- 显式调用新的 editor 级 VLM 工具生成页面级建议，再由 agent 提出编辑 op。
- 不再依赖 pipeline 强制执行 VLM。

Docling 仍然是 Stage 1 parse 的固定入口。本轮不是新增 Docling，而是新增 Stage 3 的 `skip_vlm` 运行模式。

新增用户约束优先级高于 round 1 review：pipeline 不得使用内置启发式规则来决定段落续接、页面续接、章节/标题识别、列表/脚注/表格等语义。pipeline 只暴露上下文、证据、候选和编辑接口；语义判断交给 agentic workflow 或人工确认。

另一个高优先级约束是无需向后兼容。因此本计划不保留旧 JSON、旧字段、旧目录布局或旧兼容分支。

## 当前代码事实

已核对代码：

- `src/epubforge/cli.py::run()` 和 `extract()` 当前无条件调用 `cfg.require_llm()` 与 `cfg.require_vlm()`。
- `src/epubforge/pipeline.py::run_extract()` 当前无条件导入并调用 `epubforge.extract.extract()`。
- `src/epubforge/extract.py::extract()` 当前直接写 `03_extract/unit_*.json`、`audit_notes.json`，并在启用 book memory 时写 `book_memory.json`。
- `src/epubforge/assembler.py::assemble()` 当前扫描 `03_extract/unit_*.json`，未隔离 mode、`--pages` 或失败半成品。
- `assembler.py` 当前对非 `llm_group` unit 一律标为 `Provenance.source="vlm"`。
- `assembler.py` 当前自动执行 `_merge_empty_callout_footnotes()`、`_merge_continued_tables()`、`_pair_footnotes()`，并按 level-1 heading 切 chapter。
- `src/epubforge/ir/semantic.py::Provenance.source` 当前只允许 `"llm" | "vlm" | "passthrough"`。
- `src/epubforge/epub_builder.py::_map_figures_to_images()` 当前按页内图像排序绑定，不优先使用 `Figure.image_ref`。
- editor prompt 当前不携带 Stage 3 模式、复杂页、原 PDF 或整页图像渲染入口。
- parser 当前 `generate_page_images=False`，`work/images` 只有 figure crops，不是整页页面图目录。

Docling 资料与本地版本事实：

- 本地依赖包含 `docling-core 2.74.0`、`docling 2.90.0`。
- `RefItem` 有 `cref` 与 `resolve(doc)`。
- `doc.iterate_items(page_no=pno)` 是按文档树读序取页内 item 的入口。
- `TableItem.export_to_html(doc, add_caption=False)` 在锁定版本里不能被当作会移除 caption 的可靠 API。

## 必须删除或替代的 round 1 启发式

以下 round 1 设计全部删除，不进入实现：

- 不实现 `_split_callout()`、脚注 callout 正则提取、页内脚注归并、跨页脚注续接判定。
- 不实现 `_is_incomplete_note_text()` 或任何基于终止标点的脚注/段落完整性判断。
- 不实现 `first_block_continues_prev_tail` 的标点、章节正则、列表 marker 判断。
- 不实现 table continuation 的列宽漂移、标题匹配、首尾 table 邻接等规则。
- 不把 `TITLE` / `SECTION_HEADER` 自动转换成用于切章的 `Heading`。
- 不把 `FOOTNOTE` 自动转换成 `Footnote(callout=...)` 或 `Footnote(callout="")`。
- 不把 Docling `CAPTION` 邻近文本推断成 `table_title`、`caption`、source note。
- 不用 `unit.kind="vlm_group"` 伪装 skip-VLM 产物。

替代方案是：skip-VLM 输出 Docling 证据、机械草稿 block 和候选上下文；editor/agent 通过 op 将候选转换成最终语义。

## 机械处理与语义判断的界线

允许的机械处理：

- 读取显式用户参数、配置和环境变量。
- 按 `02_pages.json` 页序过滤 TOC、应用 `--pages`。
- 按 `doc.iterate_items(page_no=...)` 保存 Docling 给出的 item 顺序。
- 原样传递 Docling 的 `label`、`self_ref`、`text`、`prov.bbox`、`captions`、`footnotes`、`references`、`marker` 等字段。
- 调用 `RefItem.resolve(doc)` 保存显式引用目标文本，作为证据。
- 对 `TABLE` item 调用 `export_to_html(doc)` 保存原始 table HTML，不拆分 title/caption/source。
- 用 parser 已有命名公式从 `self_ref` 和 `prov.page_no` 生成 `image_ref`，因为这是文件定位，不是语义判断。
- 为所有相邻选中页列出 `candidate_edges`，只表示“物理相邻且都被选中”，不表示续接。

禁止的语义判断：

- 基于标点、正则、字体、列宽、邻近关系、章节词形、callout 形态或标题样式决定 block 合并、脚注配对、标题层级、章节拆分、续表、题注归属、列表层级。
- 在 assemble/build/audit 中把候选自动升级成最终语义。

## 已确定的实现设计

### 配置

修改 `src/epubforge/config.py`：

- 在 `ExtractSettings` 增加：

```python
skip_vlm: bool = False
```

- 在 `_ENV_MAP` 增加：

```python
("EPUBFORGE_EXTRACT_SKIP_VLM", "extract", "skip_vlm", _bool_env)
```

优先级固定为：

1. CLI 显式参数
2. env
3. 显式 TOML
4. 默认值

不增加 `extract_mode`。旧计划中的 `extract_mode="docling"` 不实现，也不提供兼容别名。

### CLI

修改 `src/epubforge/cli.py`：

- `run` 增加三态 option：

```python
skip_vlm: bool | None = typer.Option(None, "--skip-vlm/--no-skip-vlm", help="Skip Stage 3 pipeline VLM and use Docling-derived evidence draft")
```

- `extract` 增加同样三态 option。
- `extract` 增加 `--pages`，复用 `_parse_pages()`。
- 只有 `skip_vlm is not None` 时才覆盖 `cfg.extract.skip_vlm`，避免未传 CLI 时覆盖 env/TOML。
- 删除 `run()` 和 `extract()` 入口处的无条件 `require_llm()` / `require_vlm()`。
- provider 校验下沉到 `pipeline.run_extract()`：只有确实要执行 VLM extractor 时才校验。

`_log_startup_banner()` 增加 `skip_vlm=<bool>`、`stage3_mode=<vlm|skip_vlm>`。

### Stage 3 产物隔离

新建 `src/epubforge/stage3_artifacts.py`。

新布局：

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
      evidence_index.json
```

不再读取或写入根目录 `03_extract/unit_*.json`。没有 legacy fallback。

`artifact_id` 使用 canonical JSON 计算：

- `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`
- `page_filter` 排序后序列化；无过滤时为 `null`
- 路径全部使用相对于 work dir 的 POSIX 字符串
- settings 中缺失值显式写 `null`

hash 输入：

- manifest schema version
- mode: `"vlm"` 或 `"skip_vlm"`
- `01_raw.json` sha256
- `02_pages.json` sha256
- selected non-TOC pages
- TOC pages
- page filter
- 对 VLM：model、base_url、`vlm_dpi`、batch sizes、`enable_book_memory`
- 对 skip-VLM：skip-VLM contract version

Manifest schema version 2：

```json
{
  "schema_version": 2,
  "stage": 3,
  "mode": "skip_vlm",
  "artifact_id": "0123456789abcdef",
  "artifact_dir": "03_extract/artifacts/0123456789abcdef",
  "created_at": "2026-04-24T00:00:00Z",
  "raw_sha256": "...",
  "pages_sha256": "...",
  "source_pdf": "book.pdf",
  "selected_pages": [1, 2, 4],
  "toc_pages": [3],
  "complex_pages": [2, 4],
  "page_filter": [1, 2, 3, 4],
  "unit_files": [
    "03_extract/artifacts/0123456789abcdef/unit_0000.json"
  ],
  "sidecars": {
    "audit_notes": "03_extract/artifacts/0123456789abcdef/audit_notes.json",
    "book_memory": "03_extract/artifacts/0123456789abcdef/book_memory.json",
    "evidence_index": "03_extract/artifacts/0123456789abcdef/evidence_index.json"
  },
  "settings": {
    "skip_vlm": true,
    "contract_version": 2
  }
}
```

Helper API：

- `Stage3Manifest`
- `Stage3ExtractionResult`
- `Stage3ContractError`
- `build_desired_stage3_manifest(...)`
- `active_manifest_matches_desired(work_dir, desired) -> bool`
- `validate_stage3_artifact(work_dir, manifest) -> None`
- `write_artifact_manifest_atomic(...)`
- `activate_manifest_atomic(...)`
- `load_active_stage3_manifest(work_dir)`
- `resolve_manifest_paths(work_dir, manifest)`

复用规则：

- `force=False` 且 active manifest 与 desired manifest hash 完全一致，并且 artifact manifest 和所有 listed files 校验通过，直接复用。
- 复用必须发生在导入 extractor、构造 `LLMClient`、打开 PDF 或校验 provider key 之前。
- 半成品 artifact 没有 `manifest.json` 或校验失败时不可复用。
- extractor 成功返回并通过校验后，先写 artifact `manifest.json`，再原子替换 `active_manifest.json`。
- extractor 失败时旧 active manifest 保持不变。

所有模式都必须写 `audit_notes.json`、`book_memory.json`、`evidence_index.json`。VLM 模式即使 `enable_book_memory=false` 也写空 `BookMemory` sidecar，并在 manifest settings 中记录 disabled。

### Stage 3 返回结构

VLM 与 skip-VLM extractor 都改为返回 `Stage3ExtractionResult`：

```python
class Stage3ExtractionResult(BaseModel):
    mode: Literal["vlm", "skip_vlm"]
    unit_files: list[Path]
    audit_notes_path: Path
    book_memory_path: Path
    evidence_index_path: Path
    selected_pages: list[int]
    toc_pages: list[int]
    complex_pages: list[int]
    warnings: list[Stage3Warning] = []
```

路径由 pipeline 归一化进 manifest。extractor 不负责激活 manifest。

### Skip-VLM extractor

新建 `src/epubforge/extract_skip_vlm.py`：

```python
def extract_skip_vlm(
    raw_path: Path,
    pages_path: Path,
    out_dir: Path,
    *,
    force: bool = False,
    page_filter: set[int] | None = None,
) -> Stage3ExtractionResult:
    ...
```

输入：

- `01_raw.json`
- `02_pages.json`
- artifact dir
- optional page filter

输出：

- 每个 selected non-TOC page 一个 `docling_page` unit。
- 一个 artifact-level `evidence_index.json`，按 page 和 `self_ref` 索引证据。
- 空或非空 `audit_notes.json`，只记录机械 extraction warning，不写语义判断。
- 空 `BookMemory`。

Skip unit schema：

```json
{
  "unit": {
    "kind": "docling_page",
    "pages": [42],
    "page_kinds": ["complex"],
    "extractor": "skip_vlm",
    "contract_version": 2
  },
  "draft_blocks": [],
  "evidence_items": [],
  "candidate_edges": {
    "previous_selected_page": 41,
    "next_selected_page": 43,
    "leading_item_refs": ["#/texts/10", "#/tables/2"],
    "trailing_item_refs": ["#/texts/18", "#/texts/19"]
  },
  "audit_notes": []
}
```

`candidate_edges` 只保存相邻页和页首/页尾 item ref，不判断续接。

#### Evidence item

每个 Docling item 写成 evidence：

```json
{
  "ref": "#/texts/15",
  "page": 42,
  "label": "footnote",
  "text": "...",
  "html": null,
  "bbox": [0.0, 0.0, 0.0, 0.0],
  "image_ref": null,
  "marker": null,
  "caption_refs": [],
  "footnote_refs": [],
  "reference_refs": [],
  "resolved_refs": []
}
```

`resolved_refs` 只来自 Docling 显式 `RefItem.resolve(doc)`；不得通过邻近关系补引用。

#### Draft blocks

draft block 是可构建、可编辑的粗稿，不承诺最终语义。所有 draft block 都保留 `provenance.source="docling"`、`raw_ref`、`raw_label`。

机械 mapping：

| DocItemLabel | Draft block | 说明 |
|---|---|---|
| `TEXT`, `PARAGRAPH`, `REFERENCE` | `Paragraph(role="body")` | 原样文本 |
| `TITLE` | `Paragraph(role="docling_title_candidate")` | 不转 Heading，不切章 |
| `SECTION_HEADER` | `Paragraph(role="docling_heading_candidate")` | 不使用 level 做语义 |
| `FOOTNOTE` | `Paragraph(role="docling_footnote_candidate")` | 不抽 callout，不建 Footnote |
| `LIST_ITEM` | `Paragraph(role="docling_list_item_candidate")` | 不推断列表层级；marker 只在 evidence 保存 |
| `CAPTION` | `Paragraph(role="docling_caption_candidate")` | 不归属 table/figure |
| `CODE` | `Paragraph(role="code")` | 原样文本 |
| `HANDWRITTEN_TEXT` | `Paragraph(role="docling_handwritten_candidate")` | 原样文本，并写 warning |
| `FIELD_HEADING`, `FIELD_ITEM`, `FIELD_KEY`, `FIELD_VALUE`, `FIELD_HINT` | `Paragraph(role="docling_field_candidate")` | 有 text 才写 |
| `CHECKBOX_SELECTED`, `CHECKBOX_UNSELECTED` | `Paragraph(role="docling_checkbox_candidate")` | 有 text 才写；checked 状态在 evidence |
| `GRADING_SCALE` | `Paragraph(role="docling_field_candidate")` | 原样文本，并写 warning |
| `FORMULA` | `Equation` | Docling 明确 formula；不解析语义 |
| `TABLE` | `Table(html=<exported html>)` | `table_title=""`、`caption=""`、`continuation=false`、`multi_page=false` |
| `PICTURE`, `CHART` | `Figure(image_ref=<mechanical path>)` | caption refs 只放 evidence；不做邻近推断 |
| `MARKER` | evidence only | 不生成正文 block，除非有非空 text 则 candidate paragraph |
| `PAGE_HEADER`, `PAGE_FOOTER` | evidence only | 默认不进入 draft body |
| `DOCUMENT_INDEX` | evidence only | TOC 页已过滤；若遇到只写 evidence |
| `FORM`, `KEY_VALUE_REGION`, `FIELD_REGION` | evidence only | 容器不直接成 block |
| `EMPTY_VALUE` | evidence only | 不进入 draft body |
| unknown label with text | `Paragraph(role="docling_unknown_candidate")` | 写 warning |
| unknown label without text | evidence only | debug log |

新增 roles 到 `ALLOWED_ROLES`：

- `docling_title_candidate`
- `docling_heading_candidate`
- `docling_footnote_candidate`
- `docling_list_item_candidate`
- `docling_caption_candidate`
- `docling_handwritten_candidate`
- `docling_field_candidate`
- `docling_checkbox_candidate`
- `docling_unknown_candidate`

这些 roles 是候选标签，不是最终语义。

### VLM extractor 调整

`src/epubforge/extract.py::extract()` 改为：

- 写入 pipeline 传入的 artifact dir。
- 返回 `Stage3ExtractionResult`。
- 始终写 `book_memory.json`、`audit_notes.json`、`evidence_index.json`。
- 不读旧 root-level sidecar。
- 不负责 manifest 激活。

VLM 路径可以继续让模型输出 semantic blocks；该路径的语义判断来自模型，不来自新增 skip-VLM 规则。实现时不得新增 deterministic 语义推断。已有依赖内置规则的后处理要么删除，要么改成只执行模型或 agent 明确写入的字段。

### Pipeline 行为

修改 `src/epubforge/pipeline.py`：

- `run_extract()` 先读取 `01_raw.json`、`02_pages.json`，计算 desired manifest。
- 如果可复用 active artifact，直接 log reuse 并返回，不校验 provider key。
- 如果不可复用且 `cfg.extract.skip_vlm=false`，在调用 VLM extractor 前校验 provider key。
- 如果不可复用且 `cfg.extract.skip_vlm=true`，调用 `extract_skip_vlm()`，不校验 provider key。
- `run --from 4` 走到 Stage 3 时只允许复用 active artifact；没有可复用 artifact 时失败并提示先运行 `extract` 或 `run --from 3`。
- Stage 3 成功后写 artifact manifest 并激活。
- Stage 4 assemble 只读取 active manifest。

日志：

- `Stage 3: extracting (VLM)...`
- `Stage 3: extracting (skip-VLM evidence draft)...`
- `Stage 3: reusing active artifact mode=<mode> artifact_id=<id>`

### Assemble

修改 `src/epubforge/assembler.py`：

- 删除 root `03_extract/unit_*.json` 扫描。
- 没有 `active_manifest.json` 时直接失败，提示清理旧 workdir 或重新运行 Stage 3。
- 未知 manifest schema version 或 unit kind 直接失败。
- `docling_page` 使用 skip-VLM draft assembler。
- `vlm_group` / `llm_group` 使用 manifest-listed unit files。
- `Provenance.source` 映射为：

```python
UNIT_SOURCE = {
    "llm_group": "llm",
    "vlm_group": "vlm",
    "docling_page": "docling",
}
```

skip-VLM assemble 行为：

- 只按 manifest unit order 拼接 `draft_blocks`。
- 写一个机械容器 chapter，例如 title=`"Draft extraction"`，不按 heading 切章。
- 不调用 `_merge_empty_callout_footnotes()`。
- 不调用 `_merge_continued_tables()`。
- 不调用 `_pair_footnotes()`。
- 不根据 level-1 heading 调 `_build_book()` 切章。
- 保留 page、bbox、raw_ref、raw_label、artifact_id/evidence_path。

VLM assemble 行为：

- 从 manifest-listed files 读取。
- 不使用 `_merge_empty_callout_footnotes()`。
- 不使用 `_pair_footnotes()`。
- 不按 level-1 heading 自动切章；如果未来 VLM schema 显式提供 chapter boundary 字段，assemble 只执行该显式字段。当前实现没有该字段时生成单个 draft chapter，后续由 editor `split_chapter` / `merge_chapters` 修正。
- `_merge_continued_tables()` 改名为 `_apply_explicit_table_continuations()`，只在 block 已有 `continuation=true` 时执行合并；该 flag 必须来自 VLM 输出或 editor op，不由 assemble 检测。
- 这会改变现有 VLM 输出质量，但符合“不用内置启发式做语义判断”的新约束，且本项目不需要向后兼容。

### IR / Schema

修改 `src/epubforge/ir/semantic.py`：

- `Provenance.source` 扩展为：

```python
Literal["llm", "vlm", "docling", "passthrough"]
```

- `Provenance` 增加：

```python
raw_label: str | None = None
artifact_id: str | None = None
evidence_ref: str | None = None
```

- `Book` 增加 extraction metadata：

```python
class ExtractionMetadata(BaseModel):
    stage3_mode: Literal["vlm", "skip_vlm", "unknown"] = "unknown"
    stage3_manifest_path: str | None = None
    artifact_id: str | None = None
    selected_pages: list[int] = Field(default_factory=list)
    complex_pages: list[int] = Field(default_factory=list)
    source_pdf: str | None = None
    evidence_index_path: str | None = None

class Book(BaseModel):
    extraction: ExtractionMetadata = Field(default_factory=ExtractionMetadata)
```

不保留旧 IR 兼容迁移。测试 fixture 直接更新到新 schema。

### Build

修改 `src/epubforge/epub_builder.py`：

- `_map_figures_to_images()` 改为只使用 `Figure.image_ref`。
- 删除页内 ordinal fallback。缺少或找不到 `image_ref` 时 log warning 并不注册图片。
- borrowed footnote pre-scan 扫描 `Table.caption`。
- `_render_chapter()` 对 `Table.caption` 使用 `_render_inline()`，使 agent 已显式插入的 footnote marker 可以渲染。
- build 不把 `docling_*_candidate` roles 自动转换为 heading/footnote/list/table 语义，只按 paragraph 样式渲染。

### Editor metadata and tools

修改 `src/epubforge/editor/state.py`：

```python
class Stage3EditorMeta(BaseModel):
    mode: Literal["vlm", "skip_vlm", "unknown"]
    skipped_vlm: bool
    manifest_path: str
    artifact_id: str
    selected_pages: list[int]
    complex_pages: list[int]
    source_pdf: str | None
    evidence_index_path: str | None
    extraction_warnings_path: str | None

class EditorMeta(BaseModel):
    initialized_at: str
    uid_seed: str
    stage3: Stage3EditorMeta
```

不保留旧 meta 缺少 `stage3` 的默认兼容。

`editor init`：

- 必须读取 active Stage 3 manifest。
- 将 manifest context 写入 `edit_state/meta.json`。
- 将 artifact `audit_notes.json` 复制或引用到 `edit_state/audit/extraction_notes.json`。
- 如果 active manifest 缺失，失败并提示先运行 Stage 3。

新增 editor command：

```text
epubforge editor render-page <work> --page N [--dpi 200] [--out PATH]
```

- 使用原 PDF 渲染整页图像。
- 默认输出到 `edit_state/audit/page_images/page_NNNN.jpg`。
- 不调用 LLM/VLM。
- 输出 JSON 包含 image path、page、dpi。

新增 editor command：

```text
epubforge editor vlm-page <work> --page N [--dpi 200] [--out PATH]
```

- 显式、按需调用 VLM。
- 读取 `evidence_index.json` 中该页证据，加上页面图像，调用现有 VLM client。
- 输出到 `edit_state/audit/vlm_pages/page_NNNN.json`。
- 不自动修改 `book.json`，不自动 propose/apply op。
- 缺少 provider key 时只该 command 失败，不影响 skip-VLM pipeline。

`render_prompt()`：

- 加入 extraction context block：
  - mode / skipped_vlm
  - active manifest
  - evidence index
  - selected pages
  - current chapter 覆盖 pages
  - complex pages in current chapter
  - `render-page` 命令示例
  - `vlm-page` 命令示例
- 明确提示 `docling_*_candidate` 是候选，不是最终语义。

### Editor ops

为 agent 修正 skip-VLM 草稿增加操作接口。

新增 `replace_block`：

```json
{
  "op": "replace_block",
  "block_uid": "...",
  "block_kind": "footnote",
  "block_data": {}
}
```

- 用于把 candidate paragraph 转换成 Heading / Footnote / Table / Figure / Equation / Paragraph。
- 保留原 uid，除非显式给 `new_block_uid`。
- `block_data` 必须通过对应 payload model 验证。

新增 `set_paragraph_cross_page`：

```json
{
  "op": "set_paragraph_cross_page",
  "block_uid": "...",
  "value": true
}
```

新增 `set_table_metadata`：

```json
{
  "op": "set_table_metadata",
  "block_uid": "...",
  "table_title": "...",
  "caption": "...",
  "continuation": false,
  "multi_page": false,
  "merge_record": null
}
```

这些 op 让 agent 做语义决定；pipeline 不代做。

既有 `split_chapter`、`merge_chapters`、`relocate_block`、`merge_blocks`、`pair_footnote` 继续作为人工/agent 修正接口使用。

### Audit / Doctor

新增或扩展 audit detector：

- `detect_candidate_issues()`：发现 `docling_*_candidate` roles，输出 audit notes/hints，提示需要 scanner/fixer 复核。
- `detect_footnote_issues()`：如果出现 `Footnote(callout="")`，输出 `footnote.empty_callout_body`。
- `detect_table_issues()` 保持 HTML 结构检查；不推断续表。
- `detect_table_merge_issues()` 只检查已经由 VLM/agent 设置的 `multi_page` / `merge_record`，不自动识别续表。

`doctor`：

- 如果 `meta.stage3.skipped_vlm=true`，对覆盖 complex pages 的 chapter 追加 `needs_scan` hint。
- 对 `docling_*_candidate` roles 追加 `needs_scan` 或 fixer hint。
- 这些 hint 不表示错误，只表示需要 agent 审阅。

### Cache

- skip-VLM Stage 3 不构造 `LLMClient`，不读写 LLM/VLM request cache。
- Stage 3 artifact manifest 不是 LLM cache。
- `editor vlm-page` 使用现有 request cache 机制，并在日志里标为 editor VLM request。
- prompt caching 设置只影响 VLM/LLM 调用，不影响 skip-VLM。

### Logging and observability

必须新增日志字段：

- startup: `stage3_mode`, `skip_vlm`
- Stage 3 start/reuse: mode, artifact_id, selected page count, complex page count
- skip-VLM unit: page, page kind, evidence item count, draft block count, warning count
- manifest activation: previous artifact id, new artifact id, active manifest path
- provider gating: when VLM is required, log before validation; skip-VLM path log `provider_required=false`
- editor render-page / vlm-page: page, output path, cache HIT/MISS for VLM page command

### Failure modes

Fail fast with `Stage3ContractError` or `CommandError` for:

- missing `01_raw.json`
- missing `02_pages.json`
- selected pages empty after TOC/page filtering
- active manifest missing when assembling
- manifest schema version unsupported
- artifact manifest missing or listed file missing
- active artifact hash mismatch when `run --from 4` expects reuse
- unknown unit kind
- Docling JSON cannot load
- `TABLE` item export throws or returns non-string
- editor `render-page` cannot find source PDF
- editor `vlm-page` lacks provider key

Do not fail for:

- Docling footnote text lacking callout
- possible paragraph continuation
- possible table continuation
- possible heading/chapter boundary
- possible caption/source attribution

Those become evidence/hints for agent review.

User-visible error for strict mechanical failures must include:

- page if known
- item ref if known
- failing condition
- suggested command, e.g. rerun Stage 3, rerun without `--skip-vlm`, or render page for manual inspection

### 用户可见行为

- 默认 `epubforge run book.pdf` 仍走 VLM Stage 3。
- `epubforge run book.pdf --skip-vlm` 不需要 provider key 即可执行 Stage 1-4。
- `epubforge extract book.pdf --skip-vlm --pages 10-12` 只生成 selected pages 的 skip-VLM artifact。
- `epubforge assemble book.pdf` 只读取 active manifest，不扫描旧 root units。
- skip-VLM 生成的是 evidence draft，不承诺章节、脚注、续表、题注等最终语义正确。
- `editor doctor` 会把 complex pages 和候选 role 暴露给 scanner/fixer，而不是静默认为可发布。
- `editor render-page` 和 `editor vlm-page` 是后续 agent 的显式工具，不是 pipeline 隐式依赖。

## 不做向后兼容

明确不实现：

- 不支持旧 `03_extract/unit_*.json` root scanning。
- 不支持旧 root-level `book_memory.json` / `audit_notes.json` sidecar。
- 不支持 `extract_mode=docling` 或 `EPUBFORGE_EXTRACT_MODE`。
- 不支持 `unit.kind="vlm_group"` 伪装 skip-VLM。
- 不支持旧 `EditorMeta` 缺少 `stage3`。
- 不支持旧 IR 缺少 `Book.extraction` 的迁移。
- 不保留 build 的页内 ordinal image fallback。
- 不写旧 workdir 自动迁移。用户清理 `work/<book>/03_extract/` 或重新跑 pipeline。

测试 fixture、docs、AGENTS.md 全部按新 schema 更新。

## 测试计划

### Config / CLI / Provider

- TOML `[extract] skip_vlm = true` 生效。
- `EPUBFORGE_EXTRACT_SKIP_VLM=1` 覆盖 TOML。
- 未传 CLI 时保留 env/TOML。
- `--skip-vlm` 覆盖 false。
- `--no-skip-vlm` 覆盖 true。
- `run --skip-vlm` 无 provider key 可到 Stage 4。
- `extract --skip-vlm` 无 provider key 可运行。
- 默认 VLM 且无 reusable artifact 时才要求 provider key。
- `run --from 4` 有 matching active artifact 时不构造 `LLMClient`。
- `run --from 4` 无 matching active artifact 时清晰失败。

### Artifact isolation

- VLM 与 skip-VLM 写入不同 artifact dir。
- mode 切换不串读旧 units。
- `--pages` 改变 artifact id。
- 失败 extraction 不替换旧 active manifest。
- active manifest listed file 缺失时报错。
- root `03_extract/unit_*.json` 存在但无 manifest 时 assemble 失败，不 fallback。

### Skip-VLM no-heuristic tests

- 跨页相邻且上一段无终止标点时，也不设置 paragraph continuation。
- `DocItemLabel.FOOTNOTE` 文本以 `①` 或 `1.` 开头时，也不生成 Footnote、不抽 callout。
- `TITLE` / `SECTION_HEADER` 不生成 Heading，不触发 chapter split。
- 相邻两页 table 列数相同，也不设置 continuation。
- `LIST_ITEM` 不推断嵌套或编号层级，只保留 candidate role 和 evidence marker。
- `CAPTION` 不归属 table/figure，除非 Docling 显式 ref 只作为 evidence 保存。

### Skip-VLM extraction

- 使用真实 `DoclingDocument` fixture 覆盖 `iterate_items(page_no=...)`。
- label coverage table 中每类 label 均有测试。
- bbox/raw_ref/raw_label/evidence_ref 写入 provenance。
- explicit `RefItem.resolve(doc)` 结果进入 evidence。
- table HTML 原样保存；不依赖 `add_caption=False`。
- figure `image_ref` 与 parser 命名一致。
- `evidence_index.json` 可按 page/ref 查询。

### Assemble / IR

- `docling_page` 映射 `Provenance.source="docling"`。
- skip-VLM assemble 生成单个 draft chapter。
- skip-VLM assemble 不调用 footnote pairing、empty-callout merge、continued-table merge、H1 chapter split。
- VLM assemble 也不调用 footnote pairing、empty-callout merge、H1 chapter split。
- explicit `continuation=true` table 才触发表格合并；无 flag 时不合并。
- unknown unit kind fail fast。
- `Book.extraction` 写入 manifest metadata。

### Build

- `Figure.image_ref` 存在时正确注册图片。
- `Figure.image_ref` 缺失时不使用 ordinal fallback。
- table caption 中已有 explicit marker 时能渲染链接。
- borrowed footnote pre-scan 覆盖 `Table.caption`。

### Editor / Audit

- `editor init` 写 `meta.stage3`。
- `render-prompt` 包含 manifest、evidence index、complex pages、render-page/vlm-page 命令。
- `editor render-page` 生成整页图。
- `editor vlm-page` 在 mock VLM client 下写 page-level JSON，不修改 book。
- `doctor` 对 skip-VLM complex pages 发 `needs_scan` hint。
- `detect_candidate_issues()` 能发现 `docling_*_candidate`。
- `Footnote(callout="")` 被 audit 标为 `footnote.empty_callout_body`。

### End-to-end

- 无 API key：`run --skip-vlm` 可完成 Stage 1-4。
- 输出 `05_semantic_raw.json` 含 `Book.extraction.stage3_mode="skip_vlm"`。
- `editor init` 可初始化 skip-VLM draft。
- `editor render-page` 可渲染 complex page。
- `build` 可从 editor book 生成 EPUB，候选语义不被自动升级。

## 文档更新

更新：

- `docs/usage.md`
- `docs/agentic-editing-howto.md`
- `config.example.toml`
- `AGENTS.md`

必须写清：

- Stage 3 有 VLM 与 skip-VLM 两种模式。
- skip-VLM 产物是 evidence draft，不做章节、脚注、续表、题注等语义判断。
- 新 `03_extract/artifacts/<id>/` 与 active manifest。
- `EPUBFORGE_EXTRACT_SKIP_VLM` env。
- `Provenance.source="docling"`。
- editor `render-page` / `vlm-page` 的用途。
- 旧 workdir 不迁移；需要重跑。

## 实现顺序

1. 添加 config/env/CLI 三态 override，并把 provider 校验下沉到 pipeline。
2. 添加 `stage3_artifacts.py`、manifest schema、artifact reuse/activation tests。
3. 改 VLM extractor 返回 `Stage3ExtractionResult` 并写 artifact sidecars。
4. 实现 `extract_skip_vlm.py` evidence draft，不含启发式语义判断。
5. 修改 assemble 读取 active manifest，并添加 skip-VLM draft assembler。
6. 修改 IR metadata/provenance/roles。
7. 修改 build image binding 与 table caption marker rendering。
8. 添加 editor meta、render-page、vlm-page、prompt context。
9. 添加 editor ops：`replace_block`、`set_paragraph_cross_page`、`set_table_metadata`。
10. 添加 audit candidate detector 与 skip-VLM doctor hints。
11. 更新 docs、AGENTS、config example。
12. 跑 unit、integration、无 API key skip-VLM e2e。

## 必须由人类决定的问题

无。

本轮范围内的工程细节已定：skip-VLM 产物是 evidence draft；pipeline 不做启发式语义判断；旧格式不兼容；VLM 变成默认 pipeline 模式和显式 editor 工具，而不是 skip-VLM 的隐式依赖。
