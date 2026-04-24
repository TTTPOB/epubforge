# Stage 3 可跳过 VLM 实施计划 - Round 3

本文件是完整可执行的最终计划，替代 round 1 / round 2 的可执行内容；实现者不需要回读 earlier plan/review 才能理解方案。Earlier 文件只作为审查历史保留，不再作为实现依据。

## 目标与边界

目标：让 Stage 3 的 pipeline VLM 可显式跳过，同时让后续 editor/agent 在发现复杂页时可以选择：

- 使用自身多模态能力查看整页渲染图并提出编辑 op。
- 显式调用 editor 级 VLM 工具获取页面级建议，再由 agent 提出编辑 op。
- 不再为了得到 Stage 4 / editor 输入而强制运行 pipeline VLM。

Docling 仍然是 Stage 1 parse 的固定入口。本计划不是新增 Docling parse，而是新增 Stage 3 的 `skip_vlm` 运行模式，并把 Stage 3 产物、Stage 4 freshness、editor 多模态承接面补成明确合同。

用户硬约束优先：

1. pipeline 禁止使用内置启发式规则决定段落续接、页面续接、章节/标题识别、列表/脚注/表格等语义。pipeline 只能暴露上下文、证据、候选和操作接口；语义判断交给 agentic workflow 或后续人工/agent 修正。
2. 无需向后兼容。旧 JSON、旧字段、旧目录布局、旧兼容分支都不保留。
3. editor 必须提供明确的 `epubforge editor render-page` 工具作为多模态承接面。`work/images` 只保存 figure crops，不得被当作整页图目录。manifest/meta/prompt 必须暴露复杂页和可执行的整页渲染命令。

## 当前代码事实

已核对当前代码：

- `src/epubforge/cli.py::run()` 和 `extract()` 当前无条件调用 `cfg.require_llm()` 与 `cfg.require_vlm()`。
- `src/epubforge/pipeline.py::run_extract()` 当前无条件导入并调用 `epubforge.extract.extract()`。
- `src/epubforge/pipeline.py::run_assemble()` 当前只按 `05_semantic_raw.json` 是否存在决定跳过；它不检查 active Stage 3 artifact，mode/pages 切换后会复用旧 Book。
- `src/epubforge/extract.py::_build_units()` 当前用 page kind、`_page_trailing_element_label()`、bottom-noise 过滤和 `TABLE`/`PICTURE` label 决定复杂页是否跨页成组；这是必须删除的 deterministic batching/context heuristic。
- `src/epubforge/extract.py` 当前传递 `pending_tail` / `pending_footnote` 给下一 VLM unit，并写 `first_block_continues_prev_tail` / `first_footnote_continues_prev_footnote`。
- `src/epubforge/assembler.py` 当前扫描 `03_extract/unit_*.json`，未隔离 mode、`--pages` 或失败半成品。
- `assembler.py` 当前对非 `llm_group` unit 一律标为 `Provenance.source="vlm"`。
- `assembler.py` 当前自动执行 `_merge_empty_callout_footnotes()`、`_merge_continued_tables()`、`_pair_footnotes()`，并按 level-1 heading 切 chapter。
- `src/epubforge/editor/state.py::default_init_source()` 当前只返回 `work/<book>/05_semantic.json`，但 pipeline Stage 4 写的是 `05_semantic_raw.json`。
- `src/epubforge/parser/docling_parser.py` 当前 `generate_page_images=False`，`work/images` 只有 figure crops，不是整页图目录。
- `src/epubforge/ir/semantic.py::Provenance.source` 当前只允许 `"llm" | "vlm" | "passthrough"`。
- `src/epubforge/epub_builder.py::_map_figures_to_images()` 当前按页内图像排序绑定，不优先使用 `Figure.image_ref`。
- editor prompt 当前不携带 Stage 3 模式、复杂页、原 PDF 或整页图像渲染入口。

Docling 资料与本地版本事实：

- 本地依赖包含 `docling-core 2.74.0`、`docling 2.90.0`。
- `RefItem` 有 `cref` 与 `resolve(doc)`。
- `doc.iterate_items(page_no=pno)` 是按文档树读序取页内 item 的入口。
- `TableItem.export_to_html(doc, add_caption=False)` 在锁定版本里不能被当作会移除 caption 的可靠 API。

## 机械处理与语义判断的界线

允许的机械处理：

- 读取显式用户参数、配置和环境变量。
- 按 `02_pages.json` 页序过滤 TOC、应用 `--pages`。
- 保存 `02_pages.json` 中已有的 `simple` / `complex` / `toc` label 作为输入事实；不得从中推断章节、续接、脚注或表格语义。
- 按 `doc.iterate_items(page_no=...)` 保存 Docling 给出的 item 顺序。
- 原样传递 Docling 的 `label`、`self_ref`、`text`、`prov.bbox`、`captions`、`footnotes`、`references`、`marker` 等字段。
- 调用 `RefItem.resolve(doc)` 保存显式引用目标文本，作为证据。
- 对 `TABLE` item 调用 `export_to_html(doc)` 保存原始 table HTML；不得拆分或推断 title/caption/source note。
- 用 parser 已有命名公式从 `self_ref` 和 `prov.page_no` 生成 `image_ref`，因为这是文件定位，不是语义判断。
- 为所有物理相邻且都被选中的页面列出 `candidate_edges`，只表示“相邻且可检查”，不表示续接。
- 将 VLM 或 agent 明确写入的字段原样落入 IR。字段传递不是 pipeline 自行判断。

禁止的语义判断：

- 基于标点、正则、字体、列宽、邻近关系、章节词形、callout 形态、标题样式、bbox 底部区域或 Docling label 组合决定 block 合并、脚注配对、标题层级、章节拆分、续表、题注归属、列表层级。
- 在 assemble/build/audit 中把候选自动升级成最终语义。
- 在 VLM batching 中用 table-like label、bottom-noise、页尾元素等规则决定跨页上下文。
- 用旧 `first_block_continues_prev_tail` / `first_footnote_continues_prev_footnote` 机制自动拼接段落或脚注。

## 必须删除或替代的旧启发式

直接删除或停止调用：

- `_page_trailing_element_label()`、`_TABLE_LIKE_LABELS`、`_BOTTOM_NOISE_LABELS` 及基于它们的复杂页跨页 batching。
- `_extract_pending_context()`、`_prepend_pending()` 及 pending paragraph/footnote prompt 注入。
- `first_block_continues_prev_tail` / `first_footnote_continues_prev_footnote` 对 assemble 的任何效果。
- `_merge_empty_callout_footnotes()` 自动归并。
- `_pair_footnotes()` 自动配对与 marker 注入。
- `_merge_continued_tables()` 自动检测/合并续表。
- 按 level-1 `Heading` 自动切 chapter。
- build 端按页内 ordinal 推断 figure crop。

不实现 round 1 中的以下设计：

- 不实现 `_split_callout()`、脚注 callout 正则提取、页内脚注归并、跨页脚注续接判定。
- 不实现 `_is_incomplete_note_text()` 或任何基于终止标点的脚注/段落完整性判断。
- 不实现 `first_block_continues_prev_tail` 的标点、章节正则、列表 marker 判断。
- 不实现 table continuation 的列宽漂移、标题匹配、首尾 table 邻接等规则。
- 不把 `TITLE` / `SECTION_HEADER` 自动转换成用于切章的 `Heading`。
- 不把 `FOOTNOTE` 自动转换成 `Footnote(callout=...)` 或 `Footnote(callout="")`。
- 不把 Docling `CAPTION` 邻近文本推断成 `table_title`、`caption`、source note。
- 不用 `unit.kind="vlm_group"` 伪装 skip-VLM 产物。

替代方案：skip-VLM 输出 Docling 证据、机械 draft block 和候选上下文；editor/agent 通过 op 将候选转换成最终语义。

## 配置

修改 `src/epubforge/config.py`：

- 在 `ExtractSettings` 增加：

```python
skip_vlm: bool = False
max_vlm_batch_pages: int = 4
```

- 删除旧的 `max_simple_batch_pages` / `max_complex_batch_pages` 字段和对应 env 映射，不提供 alias。VLM batching 改为单一机械 batch size，避免按 simple/complex 或 table-like 线索决定上下文。
- 在 `_ENV_MAP` 增加：

```python
("EPUBFORGE_EXTRACT_SKIP_VLM", "extract", "skip_vlm", _bool_env)
("EPUBFORGE_EXTRACT_MAX_VLM_BATCH_PAGES", "extract", "max_vlm_batch_pages", int)
```

优先级固定为：

1. CLI 显式参数
2. env
3. 显式 TOML
4. 默认值

不增加 `extract_mode`。不支持 `extract_mode="docling"` 或 `EPUBFORGE_EXTRACT_MODE`。

`config.example.toml` 与文档必须同步删除旧 batch 字段，新增：

```toml
[extract]
skip_vlm = false
max_vlm_batch_pages = 4
```

## CLI

修改 `src/epubforge/cli.py`：

- `run` 增加三态 option：

```python
skip_vlm: bool | None = typer.Option(
    None,
    "--skip-vlm/--no-skip-vlm",
    help="Skip Stage 3 pipeline VLM and use a Docling-derived evidence draft",
)
```

- `extract` 增加同样三态 option。
- `extract` 增加 `--pages`，复用 `_parse_pages()`。
- 只有 `skip_vlm is not None` 时才覆盖 `cfg.extract.skip_vlm`，避免未传 CLI 时覆盖 env/TOML。
- 删除 `run()` 和 `extract()` 入口处的无条件 `require_llm()` / `require_vlm()`。
- provider 校验下沉到 `pipeline.run_extract()`：只有确实要执行 VLM extractor 时才校验。
- `_log_startup_banner()` 增加 `skip_vlm=<bool>`、`stage3_mode=<vlm|skip_vlm>`、`max_vlm_batch_pages=<int>`。

`run --from 4` 的闭合规则：

- `run_all(from_stage=4)` 不创建新的 Stage 3 artifact。
- 它只调用 `run_extract(..., reuse_only=True)` 校验 active artifact 存在且完整。
- 如果用户同时传了会改变 Stage 3 desired artifact 的显式 `--skip-vlm/--no-skip-vlm` 或 `--pages`，而 active artifact 不匹配，命令失败并提示先运行 `extract` 或 `run --from 3`。
- 没有 provider key 时，只要 active artifact 可复用，`run --from 4` 不得构造 `LLMClient` 或打开 PDF。

## Stage 1 稳定 PDF 来源

为满足 `editor render-page` 从任意 cwd 调用都可解析整页视觉来源，Stage 1 必须把原 PDF 固化到 workdir：

```text
work/<book>/
  source/
    source.pdf
    source_meta.json
```

实现要求：

- `run_parse()` 在 parse 前把输入 PDF 复制到 `work/<book>/source/source.pdf`。可先尝试 hardlink，再 fallback 到 `shutil.copy2()`；最终必须保证 `source/source.pdf` 是 workdir 内可读文件。
- `source_meta.json` 记录：
  - `source_pdf`: `"source/source.pdf"`
  - `original_pdf_abs`: 原始 PDF 的 resolved absolute path
  - `sha256`
  - `size_bytes`
  - `copied_at`
- `parse_pdf()` 使用 `work/<book>/source/source.pdf` 作为 Docling 输入，保证 parse 与后续 render-page 使用同一 PDF。
- `force=False` 且 `01_raw.json` 已存在时，仍要确认 `source/source.pdf` 与 `source_meta.json` 存在；缺失时失败并提示 rerun parse with `--force-rerun`，不得悄悄依赖当前 cwd 或原始绝对路径。
- Stage 3 manifest、Book.extraction、editor meta、render-prompt 都只使用 workdir-relative `source_pdf="source/source.pdf"` 作为可执行来源。`original_pdf_abs` 只作诊断信息，不作为 render-page 的 silent fallback。

## Stage 3 Artifact 隔离与 Manifest

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

`active_manifest.json` 是小型 pointer，不复制完整 manifest：

```json
{
  "schema_version": 3,
  "active_artifact_id": "0123456789abcdef",
  "manifest_path": "03_extract/artifacts/0123456789abcdef/manifest.json",
  "manifest_sha256": "...",
  "activated_at": "2026-04-24T00:00:00Z"
}
```

`manifest.json` 是 immutable artifact manifest。Manifest schema version 3：

```json
{
  "schema_version": 3,
  "stage": 3,
  "mode": "skip_vlm",
  "artifact_id": "0123456789abcdef",
  "artifact_dir": "03_extract/artifacts/0123456789abcdef",
  "created_at": "2026-04-24T00:00:00Z",
  "raw_sha256": "...",
  "pages_sha256": "...",
  "source_pdf": "source/source.pdf",
  "source_pdf_sha256": "...",
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
    "contract_version": 3,
    "vlm_dpi": null,
    "max_vlm_batch_pages": null,
    "enable_book_memory": false,
    "vlm_model": null,
    "vlm_base_url": null
  }
}
```

`artifact_id` 使用 canonical JSON 计算：

- `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`
- `page_filter` 排序后序列化；无过滤时为 `null`
- 路径全部使用相对于 work dir 的 POSIX 字符串
- settings 中缺失值显式写 `null`

hash 输入：

- manifest schema version
- mode: `"vlm"` 或 `"skip_vlm"`
- `source/source.pdf` sha256
- `01_raw.json` sha256
- `02_pages.json` sha256
- selected non-TOC pages
- TOC pages
- complex pages
- page filter
- 对 VLM：model、base_url、`vlm_dpi`、`max_vlm_batch_pages`、`enable_book_memory`
- 对 skip-VLM：skip-VLM contract version

Helper API：

- `Stage3Manifest`
- `Stage3ActivePointer`
- `Stage3ExtractionResult`
- `Stage3Warning`
- `Stage3ContractError`
- `EvidenceIndex`
- `build_desired_stage3_manifest(...)`
- `active_manifest_matches_desired(work_dir, desired) -> bool`
- `validate_stage3_artifact(work_dir, manifest) -> None`
- `write_artifact_manifest_atomic(...)`
- `activate_manifest_atomic(...)`
- `load_active_stage3_manifest(work_dir) -> tuple[Stage3ActivePointer, Stage3Manifest]`
- `resolve_manifest_paths(work_dir, manifest)`

复用规则：

- `force=False` 且 active pointer 指向的 manifest 与 desired artifact id 完全一致，并且 manifest sha、artifact manifest、所有 listed files 校验通过，直接复用。
- 复用必须发生在导入 extractor、构造 `LLMClient`、打开 PDF 或校验 provider key 之前。
- 半成品 artifact 没有 `manifest.json`、manifest sha 不匹配或 listed file 缺失时不可复用。
- extractor 成功返回并通过校验后，先写 artifact `manifest.json`，再原子替换 `active_manifest.json`。
- extractor 失败时旧 active pointer 保持不变。
- 所有模式都必须写 `audit_notes.json`、`book_memory.json`、`evidence_index.json`。VLM 模式即使 `enable_book_memory=false` 也写空 `BookMemory` sidecar，并在 manifest settings 中记录 disabled。

## Stage 3 返回结构

VLM 与 skip-VLM extractor 都返回 `Stage3ExtractionResult`：

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

## 统一 Evidence Index 合同

所有 Stage 3 artifact 都写同一 schema 的 `evidence_index.json`。VLM 与 skip-VLM 的最低合同都是 Docling evidence；VLM 可以额外索引 VLM output block，但不能缺少 Docling evidence。

Schema：

```json
{
  "schema_version": 3,
  "artifact_id": "0123456789abcdef",
  "mode": "skip_vlm",
  "source_pdf": "source/source.pdf",
  "pages": {
    "42": {
      "page": 42,
      "page_kind": "complex",
      "render_command": "epubforge editor render-page work/mybook --page 42",
      "items": [
        {
          "ref": "#/texts/15",
          "page": 42,
          "source": "docling",
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
      ],
      "vlm_blocks": []
    }
  },
  "refs": {
    "#/texts/15": {
      "page": 42,
      "item_index": 0
    }
  }
}
```

Rules：

- `pages` 只包含 selected non-TOC pages。
- `refs` 必须覆盖所有 item `ref`。
- `resolved_refs` 只来自 Docling 显式 `RefItem.resolve(doc)`；不得通过邻近关系补引用。
- VLM 模式的 `items` 仍来自 `01_raw.json` + `doc.iterate_items(page_no=...)`，与 skip-VLM 同 schema。
- VLM 模式的 `vlm_blocks` 可以记录 `{unit_file, page, block_index, kind, raw}`，作为模型输出索引；editor 工具不能依赖它存在。
- `editor vlm-page --page N`：如果 page N 不在 active artifact selected pages 中，失败并提示当前 active pages；如果 page N 有空 `items`，继续调用 VLM，但在输出 JSON 中写 `evidence_items=[]` 和 warning。
- `render-prompt` 按 page 查询 evidence；缺 ref 只影响该 ref lookup，不影响整页 prompt。

## Skip-VLM Extractor

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
- `audit_notes.json` 只记录机械 extraction warning，不写语义判断。
- 空 `BookMemory`。

Skip unit schema：

```json
{
  "unit": {
    "kind": "docling_page",
    "pages": [42],
    "page_kinds": ["complex"],
    "extractor": "skip_vlm",
    "contract_version": 3
  },
  "draft_blocks": [],
  "evidence_refs": ["#/texts/15", "#/tables/2"],
  "candidate_edges": {
    "previous_selected_page": 41,
    "next_selected_page": 43,
    "leading_item_refs": ["#/texts/10", "#/tables/2"],
    "trailing_item_refs": ["#/texts/18", "#/texts/19"]
  },
  "audit_notes": []
}
```

`candidate_edges` 只保存物理相邻页和页首/页尾 item refs：

- `previous_selected_page` 只有在 `pno - 1` 也被选中且不是 TOC 时存在。
- `next_selected_page` 只有在 `pno + 1` 也被选中且不是 TOC 时存在。
- `leading_item_refs` / `trailing_item_refs` 是按 Docling read order 取固定数量 refs，例如最多 3 个；这只是上下文索引，不表示续接。
- `--pages` 有 gap 时，不跨 gap 生成 edge。

Draft block 是可构建、可编辑的粗稿，不承诺最终语义。所有 draft block 都保留 `provenance.source="docling"`、`raw_ref`、`raw_label`、`artifact_id`、`evidence_ref`。

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

## VLM Extractor 调整

`src/epubforge/extract.py::extract()` 改为：

- 写入 pipeline 传入的 artifact dir。
- 返回 `Stage3ExtractionResult`。
- 始终写 `book_memory.json`、`audit_notes.json`、`evidence_index.json`。
- 不读旧 root-level sidecar。
- 不负责 manifest 激活。

VLM batching 必须改为纯机械分组：

- 输入 pages 是 `02_pages.json` 中 selected non-TOC pages，按页码排序。
- 连续页按 `cfg.extract.max_vlm_batch_pages` 切 chunk。
- 遇到 `--pages` gap 时强制切新 chunk。
- 不按 simple/complex 切换逻辑决定跨页上下文。
- 不读取 bbox、page trailing label、TABLE/PICTURE label 或 bottom-noise 来决定是否把页面放进同一 unit。
- `02_pages.json` 的 `complex_pages` 只写入 manifest/editor hints，不影响 VLM semantic grouping。

VLM prompt 调整：

- 删除 pending tail / pending footnote 注入。
- 删除 “may share a continuing table” 等基于 pipeline 分组暗示特定语义的句子。
- 新 prompt 只说明这些是 selected adjacent pages，并要求模型“根据页面图像和 Docling evidence 自行判断，不要假设续接”。
- 如果 VLM 输出 `Heading`、`Footnote`、`Table.continuation` 等语义字段，Stage 4 只按模型明确输出传递，不做额外检测。

VLM unit kind 改为 `vlm_batch`，不再使用旧 `vlm_group` / `llm_group`。无 legacy fallback。

VLM unit schema：

```json
{
  "unit": {
    "kind": "vlm_batch",
    "pages": [10, 11],
    "page_kinds": ["simple", "complex"],
    "extractor": "vlm",
    "contract_version": 3
  },
  "blocks": [],
  "audit_notes": []
}
```

删除 unit-level `first_block_continues_prev_tail` 与 `first_footnote_continues_prev_footnote`。如果模型需要表达跨页段落，使用 block-level explicit field（例如 `Paragraph.cross_page=true`）或后续 editor op；assemble 不从 unit flag 自动拼接。

## Pipeline 行为

修改 `src/epubforge/pipeline.py`：

- `run_extract()` 先读取 `source/source.pdf`、`01_raw.json`、`02_pages.json`，计算 desired manifest。
- 如果可复用 active artifact，直接 log reuse 并返回，不校验 provider key。
- 如果 `reuse_only=True` 且不可复用，失败并提示先运行 `extract` 或 `run --from 3`。
- 如果不可复用且 `cfg.extract.skip_vlm=false`，在调用 VLM extractor 前校验 provider key。
- 如果不可复用且 `cfg.extract.skip_vlm=true`，调用 `extract_skip_vlm()`，不校验 provider key。
- Stage 3 成功后写 artifact manifest 并激活。
- Stage 4 assemble 只读取 active manifest。

日志：

- `Stage 3: extracting (VLM)...`
- `Stage 3: extracting (skip-VLM evidence draft)...`
- `Stage 3: reusing active artifact mode=<mode> artifact_id=<id> manifest_sha256=<sha>`
- `Stage 3: provider_required=false` for skip-VLM / reusable active artifact.

## Stage 4 Freshness 与 Assemble

修改 `run_assemble()`：

- 先读取 active Stage 3 pointer + manifest，并校验 artifact。
- 如果 `05_semantic_raw.json` 存在且 `force=False`：
  - 尝试读取为 `Book`。
  - 只有当 `Book.extraction.artifact_id == active.artifact_id` 且 `Book.extraction.stage3_manifest_sha256 == active.manifest_sha256` 时才 skip。
  - 不匹配、缺 metadata、旧 schema 或 JSON 损坏时，重跑 assemble；如果 active manifest 缺失则失败。
- mode 从 VLM 切到 skip-VLM、skip-VLM 切到 VLM、`--pages` 改变、source PDF/raw/pages 改变时，active manifest sha 都会变化，Stage 4 不得复用旧 `05_semantic_raw.json`。

修改 `src/epubforge/assembler.py`：

- 删除 root `03_extract/unit_*.json` 扫描。
- 没有 `active_manifest.json` 时直接失败，提示清理旧 workdir 或重新运行 Stage 3。
- 未知 manifest schema version 或 unit kind 直接失败。
- `docling_page` 使用 skip-VLM draft assembler。
- `vlm_batch` 使用 manifest-listed unit files。
- 删除旧 `llm_group` / `vlm_group` 支持。

`Provenance.source` 映射：

```python
UNIT_SOURCE = {
    "vlm_batch": "vlm",
    "docling_page": "docling",
}
```

skip-VLM assemble 行为：

- 只按 manifest unit order 拼接 `draft_blocks`。
- 生成单个机械容器 chapter，例如 title=`"Draft extraction"`，不按 heading 切章。
- 不调用 `_merge_empty_callout_footnotes()`。
- 不调用 `_merge_continued_tables()`。
- 不调用 `_pair_footnotes()`。
- 不根据 level-1 heading 调 `_build_book()` 切章。
- 保留 page、bbox、raw_ref、raw_label、artifact_id、evidence_ref。

VLM assemble 行为：

- 从 manifest-listed files 读取。
- 不调用 `_merge_empty_callout_footnotes()`。
- 不调用 `_pair_footnotes()`。
- 不调用 `_merge_continued_tables()`。
- 不按 level-1 heading 自动切章。
- 如果 VLM 输出 `Heading`，保留为 block，但不把它用于 chapter split。
- 如果 VLM 输出 `Footnote`，保留为 Footnote，但不自动配对或 marker 注入。
- 如果 VLM 输出 `Table.continuation=true`，只保留 flag 并写 audit hint；不自动合并 table HTML。后续由 agent 用 editor op 明确修正。
- 当前实现没有显式 chapter boundary schema，因此 VLM 模式也生成单个 draft chapter。后续由 editor `split_chapter` / `merge_chapters` 修正。

`Book.extraction` 必须写入：

- `stage3_mode`
- `stage3_manifest_path`
- `stage3_manifest_sha256`
- `artifact_id`
- `selected_pages`
- `complex_pages`
- `source_pdf`
- `evidence_index_path`

## IR / Schema

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

- `Paragraph` 已有 `cross_page`，继续作为 agent/VLM 显式语义字段。skip-VLM 不设置为 true。
- `Book` 增加 extraction metadata：

```python
class ExtractionMetadata(BaseModel):
    stage3_mode: Literal["vlm", "skip_vlm", "unknown"] = "unknown"
    stage3_manifest_path: str | None = None
    stage3_manifest_sha256: str | None = None
    artifact_id: str | None = None
    selected_pages: list[int] = Field(default_factory=list)
    complex_pages: list[int] = Field(default_factory=list)
    source_pdf: str | None = None
    evidence_index_path: str | None = None

class Book(BaseModel):
    extraction: ExtractionMetadata = Field(default_factory=ExtractionMetadata)
```

不保留旧 IR 兼容迁移。测试 fixture 直接更新到新 schema。

## Build

修改 `src/epubforge/epub_builder.py`：

- `resolve_build_source(work_dir)` 解析顺序改为：
  1. `edit_state/book.json`
  2. `05_semantic.json`
  3. `05_semantic_raw.json`
- 如果选中的 `05_semantic*.json` 带有 `Book.extraction.stage3_manifest_sha256`，且 active manifest 存在但不匹配，失败而不是构建 stale book。
- `_map_figures_to_images()` 改为只使用 `Figure.image_ref`。
- 删除页内 ordinal fallback。缺少或找不到 `image_ref` 时 log warning 并不注册图片。
- borrowed footnote pre-scan 扫描 `Paragraph.text`、`Table.html`、`Table.table_title`、`Table.caption`。
- `_render_chapter()` 对 `Table.caption` 使用 `_render_inline()`，使 agent 已显式插入的 footnote marker 可以渲染。
- build 不把 `docling_*_candidate` roles 自动转换为 heading/footnote/list/table 语义，只按 paragraph 样式渲染。

## Editor Init、Meta 与多模态工具

修改 `src/epubforge/editor/state.py`：

```python
class Stage3EditorMeta(BaseModel):
    mode: Literal["vlm", "skip_vlm", "unknown"]
    skipped_vlm: bool
    manifest_path: str
    manifest_sha256: str
    artifact_id: str
    selected_pages: list[int]
    complex_pages: list[int]
    source_pdf: str
    evidence_index_path: str
    extraction_warnings_path: str

class EditorMeta(BaseModel):
    initialized_at: str
    uid_seed: str
    stage3: Stage3EditorMeta
```

不保留旧 meta 缺少 `stage3` 的默认兼容。

`default_init_source(paths)`：

- 优先使用 `work/<book>/05_semantic.json`，但必须匹配 active manifest。
- 如果 `05_semantic.json` 不存在，使用 `work/<book>/05_semantic_raw.json`，也必须匹配 active manifest。
- 两者都不存在时失败并提示先运行 `epubforge assemble <pdf>` 或 `epubforge run <pdf> --skip-vlm`。

`editor init`：

- 必须读取 active Stage 3 manifest。
- 必须验证 init source 的 `Book.extraction.artifact_id` / `stage3_manifest_sha256` 与 active manifest 匹配。
- 将 manifest context 写入 `edit_state/meta.json`。
- 将 artifact `audit_notes.json` 复制到 `edit_state/audit/extraction_notes.json`。
- 如果 active manifest 缺失，失败并提示先运行 Stage 3。
- 输出 JSON 增加 `stage3_mode`、`artifact_id`、`source_pdf`、`evidence_index_path`。

新增 editor command：

```text
epubforge editor render-page <work> --page N [--dpi 200] [--out PATH]
```

合同：

- 使用 `edit_state/meta.json` 的 `stage3.source_pdf`，解析为 `work_dir / source_pdf`。
- 渲染整页图像，不调用 LLM/VLM。
- 默认输出到 `edit_state/audit/page_images/page_NNNN.jpg`。
- 输出 JSON 包含 `image_path`、`page`、`dpi`、`source_pdf`。
- 可从非 PDF 所在 cwd 调用；路径解析只依赖 workdir。
- 如果 `source/source.pdf` 缺失，失败并提示 rerun parse with `--force-rerun`。

新增 editor command：

```text
epubforge editor vlm-page <work> --page N [--dpi 200] [--out PATH]
```

合同：

- 显式、按需调用 VLM。
- 先调用与 `render-page` 同一渲染逻辑得到整页图。
- 读取 active `evidence_index.json` 中该页证据，加上页面图像，调用现有 VLM client。
- 输出到 `edit_state/audit/vlm_pages/page_NNNN.json`。
- 不自动修改 `book.json`，不自动 propose/apply op。
- 缺少 provider key 时只该 command 失败，不影响 skip-VLM pipeline。

`render_prompt()`：

- 加入 extraction context block：
  - mode / skipped_vlm
  - active manifest path / sha
  - evidence index path
  - selected pages
  - current chapter 覆盖 pages
  - complex pages in current chapter
  - `render-page` 命令示例，使用 absolute work path
  - `vlm-page` 命令示例，使用 absolute work path
- 明确提示 `docling_*_candidate` 是候选，不是最终语义。
- 不引用 `work/images` 作为整页图来源。

## Editor Ops

为 agent 修正 skip-VLM 草稿增加操作接口。所有新增 op 都必须进入 Pydantic schema、`EditOp` union、`apply.py` dispatcher、lease scope、precondition/effect-precondition/revert 逻辑和测试。

### `replace_block`

Schema：

```json
{
  "op": "replace_block",
  "block_uid": "...",
  "block_kind": "footnote",
  "block_data": {},
  "new_block_uid": null,
  "original_block": {}
}
```

合同：

- 用于把 candidate paragraph 转换成 Heading / Footnote / Table / Figure / Equation / Paragraph。
- `original_block` 是 required `BlockSnapshot`，其 `uid` 必须等于 `block_uid`。
- Apply 时必须校验当前 block 与 `original_block` 完全一致；不一致则 reject，transaction rollback。
- 默认保留原 uid。只有显式给 `new_block_uid` 时才换 uid；新 uid 必须全书不冲突。
- `block_data` 必须通过对应 payload model 验证；payload 不包含 uid，uid 由 apply 注入。
- 允许跨 `Block` union 改 kind。
- Lease scope：intra-chapter op，必须持有目标 chapter lease。
- Revert：通过 `original_block` 构造反向 `replace_block`，因此不是 irreversible。反向 op 的 `original_block` 是当前 replacement snapshot。
- Accepted-log effect preconditions：target block exists、target block kind equals replacement kind、uid equals expected uid；revert 前还要校验当前 block snapshot 等于 replacement snapshot。

### `set_paragraph_cross_page`

Schema：

```json
{
  "op": "set_paragraph_cross_page",
  "block_uid": "...",
  "value": true
}
```

合同：

- 只能作用于 `Paragraph`。
- 不得由 pipeline 自动生成；只能由 VLM explicit output 或 editor/agent op 设置。
- Lease scope：intra-chapter。
- Revert：使用 prior `cross_page` precondition 或 accepted log 中的 previous value。
- `PRECONDITION_FIELDS` 增加 `cross_page`。

### `set_table_metadata`

Schema：

```json
{
  "op": "set_table_metadata",
  "block_uid": "...",
  "table_title": "...",
  "caption": "...",
  "continuation": false,
  "multi_page": false,
  "merge_record": null,
  "original_metadata": {
    "table_title": "...",
    "caption": "...",
    "continuation": false,
    "multi_page": false,
    "merge_record": null
  }
}
```

合同：

- 只能作用于 `Table`。
- Apply 时校验当前 metadata 与 `original_metadata` 完全一致；不一致则 reject，transaction rollback。
- Consistency rules：
  - `merge_record is not None` 必须 `multi_page=true`。
  - `multi_page=true` 必须 `merge_record is not None`，且 `merge_record.segment_html`、`segment_pages`、`segment_order`、`column_widths` 长度一致且至少为 2。
  - `multi_page=true` 时 `continuation` 必须为 false；合并后的 table 自身不是 continuation segment。
  - `continuation=true` 时 `multi_page=false` 且 `merge_record=null`；它只是一个待处理 segment。
  - `multi_page=false` 且 `continuation=false` 时 `merge_record=null`。
- 此 op 只设置 metadata，不自动改 table HTML、不删除 continuation segment。
- 需要改 HTML 时，agent 使用既有 `set_text(field="html")` 或 `replace_block`，并用 preconditions/snapshots 保证可审计。
- Lease scope：intra-chapter。
- Revert：通过 `original_metadata` 构造反向 `set_table_metadata`。

既有 `split_chapter`、`merge_chapters`、`relocate_block`、`merge_blocks`、`pair_footnote` 继续作为人工/agent 修正接口使用。pipeline 不自动调用它们。

## Audit / Doctor

新增或扩展 audit detector：

- `detect_candidate_issues()`：发现 `docling_*_candidate` roles，输出 audit hints，提示需要 scanner/fixer 复核。
- `detect_footnote_issues()`：如果出现 `Footnote(callout="")`，输出 `footnote.empty_callout_body`。
- `detect_table_issues()` 保持 HTML 结构检查；不推断续表。
- `detect_table_merge_issues()` 只检查已经由 VLM/agent 设置的 `multi_page` / `merge_record` 一致性，使用上面的 consistency rules；不自动识别续表。
- `detect_invariant_issues()` 接受单个 `"Draft extraction"` chapter，不把“未切章”当作结构错误；可以作为 hint。

`doctor`：

- 如果 `meta.stage3.skipped_vlm=true`，对覆盖 complex pages 的 chapter 追加 `needs_scan` hint。
- 对 `docling_*_candidate` roles 追加 `needs_scan` 或 fixer hint。
- 对 VLM `Table.continuation=true` 且未 merge 的 table 追加 `needs_table_review` hint。
- 这些 hint 不表示错误，只表示需要 agent 审阅。

## Cache

- skip-VLM Stage 3 不构造 `LLMClient`，不读写 LLM/VLM request cache。
- Stage 3 artifact manifest 不是 LLM cache。
- `editor vlm-page` 使用现有 request cache 机制，并在日志里标为 editor VLM request。
- prompt caching 设置只影响 VLM/LLM 调用，不影响 skip-VLM。

## Logging and Observability

必须新增日志字段：

- startup: `stage3_mode`, `skip_vlm`, `max_vlm_batch_pages`
- Stage 1 source copy: source path, sha256, target path
- Stage 3 start/reuse: mode, artifact_id, manifest_sha256, selected page count, complex page count
- skip-VLM unit: page, page kind, evidence item count, draft block count, warning count
- VLM unit: pages, batch size, evidence item count, cache HIT/MISS
- manifest activation: previous artifact id, new artifact id, active manifest path
- provider gating: when VLM is required, log before validation; skip-VLM/reuse path log `provider_required=false`
- Stage 4 freshness: active artifact id/sha, existing Book artifact id/sha, decision `skip|rerun`
- editor render-page / vlm-page: page, source_pdf, output path, cache HIT/MISS for VLM page command

## Failure Modes

Fail fast with `Stage3ContractError` or `CommandError` for:

- missing `source/source.pdf` or `source_meta.json`
- missing `01_raw.json`
- missing `02_pages.json`
- selected pages empty after TOC/page filtering
- active manifest missing when assembling
- manifest schema version unsupported
- active pointer manifest sha mismatch
- artifact manifest missing or listed file missing
- active artifact hash mismatch when `run --from 4` was asked to reuse a different Stage 3 configuration
- unknown unit kind
- Docling JSON cannot load
- `TABLE` item export throws or returns non-string
- `editor init` source Book extraction metadata does not match active manifest
- editor `render-page` cannot find `work/<book>/source/source.pdf`
- editor `vlm-page` lacks provider key

Do not fail for:

- Docling footnote text lacking callout
- possible paragraph continuation
- possible table continuation
- possible heading/chapter boundary
- possible caption/source attribution
- page classified complex

Those become evidence/hints for agent review.

User-visible error for strict mechanical failures must include:

- page if known
- item ref if known
- failing condition
- suggested command, e.g. rerun Stage 3, rerun without `--skip-vlm`, rerun parse with `--force-rerun`, or render page for manual inspection
- confirmation that old active artifact was preserved when extraction failed

## 用户可见行为

- 默认 `epubforge run book.pdf` 仍走 VLM Stage 3。
- `epubforge run book.pdf --skip-vlm` 不需要 provider key 即可执行 Stage 1-4。
- `epubforge extract book.pdf --skip-vlm --pages 10-12` 只生成 selected pages 的 skip-VLM artifact。
- `epubforge assemble book.pdf` 只读取 active manifest，不扫描旧 root units。
- Stage 4 输出是否复用取决于 `Book.extraction.stage3_manifest_sha256` 是否匹配 active manifest；mode/pages 切换后会自动重组。
- `epubforge editor init work/mybook` 在 `05_semantic.json` 不存在时会直接使用 `05_semantic_raw.json`，无需手工复制。
- skip-VLM 生成的是 evidence draft，不承诺章节、脚注、续表、题注等最终语义正确。
- `editor doctor` 会把 complex pages 和候选 role 暴露给 scanner/fixer，而不是静默认为可发布。
- `editor render-page` 是后续 agent 检查整页布局的明确工具。
- `editor vlm-page` 是后续 agent 显式、按需调用 VLM 的工具，不是 pipeline 隐式依赖。
- `build` 可以从 `edit_state/book.json`、`05_semantic.json` 或 matching `05_semantic_raw.json` 构建；候选语义不会被自动升级。

## 不做向后兼容

明确不实现：

- 不支持旧 `03_extract/unit_*.json` root scanning。
- 不支持旧 root-level `book_memory.json` / `audit_notes.json` sidecar。
- 不支持旧 `unit.kind="vlm_group"` / `"llm_group"`。
- 不支持 skip-VLM 伪装成 `vlm_group`。
- 不支持 `extract_mode=docling` 或 `EPUBFORGE_EXTRACT_MODE`。
- 不支持旧 `max_simple_batch_pages` / `max_complex_batch_pages` 配置或 env alias。
- 不支持旧 `EditorMeta` 缺少 `stage3`。
- 不支持旧 IR 缺少 `Book.extraction` 的迁移。
- 不保留 VLM unit `first_block_continues_prev_tail` / `first_footnote_continues_prev_footnote` 对 assemble 的效果。
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
- `EPUBFORGE_EXTRACT_MAX_VLM_BATCH_PAGES` 生效。
- 旧 `EPUBFORGE_EXTRACT_MAX_SIMPLE_BATCH_PAGES` / `EPUBFORGE_EXTRACT_MAX_COMPLEX_BATCH_PAGES` 不再被读取。
- `run --skip-vlm` 无 provider key 可到 Stage 4。
- `extract --skip-vlm` 无 provider key可运行。
- 默认 VLM 且无 reusable artifact 时才要求 provider key。
- `run --from 4` 有 active artifact 时不构造 `LLMClient`。
- `run --from 4` 无 active artifact 或 requested Stage 3 config 不匹配时清晰失败。

### Source PDF / render-page

- Stage 1 写 `source/source.pdf` 与 `source_meta.json`。
- `source_meta.sha256` 与实际 PDF 匹配。
- `editor render-page` 从非 PDF 所在 cwd 调用仍能渲染。
- 删除 `source/source.pdf` 后 `render-page` 报错并提示 rerun parse。
- manifest、Book.extraction、edit_state/meta、render-prompt 都使用 workdir-relative `source/source.pdf`。

### Artifact isolation

- VLM 与 skip-VLM 写入不同 artifact dir。
- mode 切换不串读旧 units。
- `--pages` 改变 artifact id。
- source PDF/raw/pages sha 改变 artifact id。
- 失败 extraction 不替换旧 active manifest。
- active pointer sha mismatch 报错。
- active manifest listed file 缺失时报错。
- root `03_extract/unit_*.json` 存在但无 manifest 时 assemble 失败，不 fallback。

### Stage 4 freshness

- VLM -> skip-VLM 后，即使 `05_semantic_raw.json` 已存在也重跑 assemble。
- skip-VLM -> VLM 后重跑 assemble。
- `--pages` 改变后重跑 assemble。
- active manifest sha 与 `Book.extraction.stage3_manifest_sha256` 匹配时才 skip。
- 旧 Book 缺 `extraction` metadata 时不被当作 fresh。

### VLM no-heuristic batching

- `_build_units()` 不读取 anchors/bbox/labels。
- table-like page、bottom footnote/list noise、page trailing picture 都不会改变 mechanical chunking。
- `--pages` gap 强制切 chunk。
- VLM prompt 不包含 pending tail / pending footnote，也不包含 “continuing table” 暗示。

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

### Evidence index

- VLM 与 skip-VLM artifact 都写统一 evidence schema。
- VLM evidence index 包含 Docling evidence，即使 `vlm_blocks` 为空也可用于 `vlm-page`。
- page 不在 active selected pages 时 `editor vlm-page` 清晰失败。
- selected page 有空 evidence items 时 `editor vlm-page` 继续并写 warning。

### Assemble / IR

- `docling_page` 映射 `Provenance.source="docling"`。
- `vlm_batch` 映射 `Provenance.source="vlm"`。
- skip-VLM assemble 生成单个 draft chapter。
- VLM assemble 生成单个 draft chapter，不按 Heading 自动切章。
- assemble 不调用 footnote pairing、empty-callout merge、continued-table merge、H1 chapter split。
- VLM `Table.continuation=true` 被保留但不自动合并。
- unknown unit kind fail fast。
- `Book.extraction` 写入 manifest metadata 与 manifest sha。

### Build

- `Figure.image_ref` 存在时正确注册图片。
- `Figure.image_ref` 缺失时不使用 ordinal fallback。
- table caption 中已有 explicit marker 时能渲染链接。
- borrowed footnote pre-scan 覆盖 `Table.caption`。
- candidate roles 只按 paragraph 渲染，不变成 heading/footnote/list。

### Editor / Ops / Audit

- `editor init` 可从 matching `05_semantic_raw.json` 初始化 skip-VLM draft。
- `editor init` 写 `meta.stage3`。
- `render-prompt` 包含 manifest、manifest sha、evidence index、complex pages、render-page/vlm-page 命令。
- `editor render-page` 生成整页图。
- `editor vlm-page` 在 mock VLM client 下写 page-level JSON，不修改 book。
- `replace_block` schema validate、apply、lease enforcement、transaction rollback、revert。
- `set_paragraph_cross_page` 只允许 Paragraph，支持 revert。
- `set_table_metadata` consistency validation、apply、lease enforcement、transaction rollback、revert。
- `doctor` 对 skip-VLM complex pages 发 `needs_scan` hint。
- `detect_candidate_issues()` 能发现 `docling_*_candidate`。
- `Footnote(callout="")` 被 audit 标为 `footnote.empty_callout_body`。

### End-to-end

- 无 API key：`run --skip-vlm` 可完成 Stage 1-4。
- 输出 `05_semantic_raw.json` 含 `Book.extraction.stage3_mode="skip_vlm"` 和 manifest sha。
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
- 新 `03_extract/artifacts/<id>/` 与 active manifest pointer。
- `source/source.pdf` 是 editor render-page 的稳定整页来源。
- `EPUBFORGE_EXTRACT_SKIP_VLM` 与 `EPUBFORGE_EXTRACT_MAX_VLM_BATCH_PAGES` env。
- `Provenance.source="docling"`。
- editor `render-page` / `vlm-page` 的用途。
- 旧 workdir 不迁移；需要重跑。

## 实现顺序

1. 添加 `source/source.pdf` 固化与 source meta，更新 parse/pipeline tests。
2. 添加 config/env/CLI 三态 override，并把 provider 校验下沉到 pipeline。
3. 添加 `stage3_artifacts.py`、manifest pointer/schema、artifact reuse/activation/freshness tests。
4. 改 VLM extractor 为 mechanical batching，返回 `Stage3ExtractionResult`，写 artifact sidecars 与 evidence index。
5. 实现 `extract_skip_vlm.py` evidence draft，不含启发式语义判断。
6. 修改 assemble 读取 active manifest，添加 skip-VLM/VLM draft assembler，并写 `Book.extraction`。
7. 修改 `run_assemble()` freshness，绑定 active manifest sha。
8. 修改 IR metadata/provenance/roles。
9. 修改 build source resolution、image binding 与 table caption marker rendering。
10. 添加 editor meta、`default_init_source()` fallback、render-page、vlm-page、prompt context。
11. 添加 editor ops：`replace_block`、`set_paragraph_cross_page`、`set_table_metadata`。
12. 添加 audit candidate detector 与 skip-VLM doctor hints。
13. 更新 docs、AGENTS、config example。
14. 跑 unit、integration、无 API key skip-VLM e2e。

## 已确定与需人类决定的问题

已确定：

- skip-VLM 产物是 evidence draft。
- pipeline 不做启发式语义判断。
- 旧格式不兼容。
- Stage 4 freshness 必须绑定 active Stage 3 manifest sha。
- `editor init` 必须能从 matching `05_semantic_raw.json` 直接初始化。
- 整页视觉来源是 workdir 内的 `source/source.pdf` 加 `editor render-page` 命令。
- VLM batching 是机械相邻页 chunking，不使用 label/bbox/table-like/page-tail heuristics。
- evidence index 是 VLM 与 skip-VLM 的统一合同。
- 新 editor ops 的 apply/revert/lease 合同如上。

必须由人类决定的问题：无。
