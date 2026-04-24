# Docling-Direct Extraction Plan — v2

## Design Philosophy Change (from v1)

v1 试图在 extract_docling.py 里用大量启发式（标题层级、跨页续接、脚注callout提取等）来模拟 VLM 的判断。这是错误方向。

**v2 核心思路**：docling 负责提取原始文本和粗粒度标签，直接转为 unit JSON。不做启发式推断。所有需要"判断"的事情（标题层级准不准、脚注配对对不对、段落是否跨页续接）交给后续 agentic workflow 的 subagent 处理。subagent 是多模态的，觉得哪里不对可以自己看页面图像。

**VLM 的新定位**：不再是 pipeline 的必经阶段，而是 agentic workflow 中的一个工具。subagent 觉得某页布局奇怪时可以调用 VLM 或自己看图。

## 具体方案

### 1. 新模块：`src/epubforge/extract_docling.py`

功能很简单：读 `01_raw.json`，按页转为 unit JSON，写到 `03_extract/`。

**不做的事**：
- 不做标题层级推断（直接用 docling 给的 level，原样输出）
- 不做跨页续接检测（`first_block_continues_prev_tail` 始终 False）
- 不做脚注 callout 提取（docling 提取的 footnote text 原样输出，callout 留空）
- 不做 BookMemory
- 不做 audit_notes

**做的事**：
- 用 `doc.iterate_items(page_no=N)` 按读序遍历每页的 item（必须用这个API，不能用 doc.texts/doc.tables 的 flat list，否则顺序错）
- 按 isinstance 分发 item 类型（SectionHeaderItem, TextItem, TableItem, PictureItem, ListItem, FormulaItem 等）
- 对每个 item 生成对应的 block dict
- 每页一个 unit 文件
- 跳过 TOC 页（从 02_pages.json 读取）
- 跳过 PAGE_HEADER / PAGE_FOOTER

**Label/Type 到 Block 的映射**：

| Item Type | DocItemLabel | Block kind | 字段映射 |
|---|---|---|---|
| SectionHeaderItem | SECTION_HEADER | heading | level=item.level (clamp 1-6), text=item.text |
| TextItem | TITLE | heading | level=1, text=item.text |
| TextItem | TEXT | paragraph | text=item.text |
| TextItem | PARAGRAPH | paragraph | text=item.text |
| TextItem | FOOTNOTE | footnote | callout="", text=item.text (让subagent后续拆分) |
| TextItem | REFERENCE | paragraph | text=item.text |
| ListItem | LIST_ITEM | paragraph | text=f"{item.marker} {item.text}" |
| TableItem | TABLE | table | html=item.export_to_html(doc, add_caption=False) |
| PictureItem | PICTURE | figure | image_ref=从 prov 推导的文件路径, caption=从 item.captions 解析 |
| PictureItem | CHART | figure | 同 PICTURE |
| FormulaItem | FORMULA | equation | latex=item.text (质量取决于 docling) |
| CodeItem | CODE | paragraph | text=item.text (保留内容，格式丢失) |
| * | PAGE_HEADER | (skip) | |
| * | PAGE_FOOTER | (skip) | |
| * | CAPTION | (skip) | 已通过父级 item.captions 处理 |

**PictureItem.image_ref 推导**（必须与 docling_parser.py 一致）：
```python
ref_id = item.self_ref.replace("/", "_").replace("#", "_").lstrip("_")
page = item.prov[0].page_no if item.prov else 0
image_ref = f"images/p{page:04d}_{ref_id}.png"
```

**PictureItem/TableItem 的 caption 处理**：
```python
# item.captions 是 list[RefItem]，需要 dereference 获取文字
caption_texts = []
for ref in item.captions:
    ref_item = doc.get_ref_item(ref)  # or similar API
    if ref_item and hasattr(ref_item, 'text'):
        caption_texts.append(ref_item.text)
caption = " ".join(caption_texts) if caption_texts else ""
```

**Unit JSON 结构**（与 VLM 路径完全一致）：
```json
{
  "unit": {"kind": "vlm_group", "pages": [42]},
  "first_block_continues_prev_tail": false,
  "first_footnote_continues_prev_footnote": false,
  "blocks": [
    {"kind": "heading", "level": 2, "text": "...", "page": 42},
    {"kind": "paragraph", "text": "...", "page": 42}
  ],
  "audit_notes": []
}
```

unit.kind 用 `"vlm_group"` 保持兼容（assembler 对非 `"llm_group"` 的 kind 统一标记 source="vlm"，语义上不完美但不影响功能）。

**函数签名**：
```python
def extract_docling(
    raw_path: Path,
    pages_path: Path,
    out_dir: Path,
    *,
    force: bool = False,
    page_filter: set[int] | None = None,
) -> None:
```

注意：不需要 pdf_path（不渲染页面），不需要 cfg（不调用 API）。pipeline.py 调用时适配参数即可。

### 2. 修改：`src/epubforge/config.py`

在 `ExtractSettings` 加一个字段：
```python
extract_mode: Literal["vlm", "docling"] = "vlm"
```

在 `_ENV_MAP` 加一行：
```python
("EPUBFORGE_EXTRACT_MODE", "extract", "extract_mode", str),
```

### 3. 修改：`src/epubforge/pipeline.py`

`run_extract()` 根据 mode 分发：
```python
if cfg.extract.extract_mode == "docling":
    from epubforge.extract_docling import extract_docling
    log.info("Stage 3: extracting (docling-direct)…")
    with stage_timer(log, "3 extract (docling)"):
        extract_docling(raw, pages_json, out_dir, force=force, page_filter=pages)
else:
    from epubforge.extract import extract
    log.info("Stage 3: extracting (VLM)…")
    with stage_timer(log, "3 extract"):
        extract(pdf_path, raw, pages_json, out_dir, cfg, force=force, page_filter=pages)
```

### 4. 修改：`src/epubforge/cli.py`

在 `run` 和 `extract` 命令加 `--extract-mode` 选项：
```python
extract_mode: str | None = typer.Option(
    None, "--extract-mode",
    help="'vlm' (default) or 'docling' (skip VLM, use docling OCR output directly)"
),
```

应用到 cfg：
```python
if extract_mode is not None:
    cfg = cfg.model_copy(update={
        "extract": cfg.extract.model_copy(update={"extract_mode": extract_mode})
    })
```

条件跳过 API key 检查：
```python
if cfg.extract.extract_mode != "docling":
    cfg.require_llm()
    cfg.require_vlm()
```

### 5. 给 agentic workflow 提供的工具（未来增强，本次不实现）

这是 v2 方案的核心理念延伸。当 editor 的 subagent 发现 docling-direct 提取有问题时：
- subagent 本身是多模态的，可以直接看页面图像判断
- 或调用 VLM 工具重新提取特定页面
- 需要提供一个"查看页面图像"的工具让 subagent 使用

这部分本次不实现，但方案需要记录这个方向，以便后续迭代。

### 6. 不修改的文件

- `assembler.py` — unit JSON 格式完全兼容
- `extract.py` — VLM 路径不动
- `ir/semantic.py` — IR 模型不动
- `classifier.py` — 分类逻辑不动

### 7. 测试

新增 `tests/test_extract_docling.py`：
- 测试各 item type 到 block 的映射
- 测试 TOC 页跳过
- 测试 page_filter 过滤
- 测试 image_ref 推导公式
- 测试 unit JSON schema 兼容性

### 8. 需要用户决策的点（写在这里，不阻塞开发）

**Decision 1**: unit.kind 用什么值？
- 选项A: `"vlm_group"` — 完全兼容，但 source 标记为 "vlm" 语义不准
- 选项B: `"docling_page"` — 语义准确，但需要改 assembler 的 source 分发逻辑（加一行 elif）
- **暂定**: 选项A，最小改动。后续如果需要区分来源再改。

**Decision 2**: 默认 extract_mode 是否改为 "docling"？
- 当前默认 "vlm" 保持向后兼容
- 用户可随时在 config.local.toml 改为 "docling"
- **暂定**: 默认保持 "vlm"，用户明确选择 "docling"

**Decision 3**: extract_docling 模块约 150-200 行，是否足够简单直接？
- 不做启发式 = 代码非常简单
- 只做标签映射 + 写 JSON
- **暂定**: 是的，这正是目标

## v1 → v2 的关键变化

1. **删除所有启发式**：标题层级推断、跨页续接检测、脚注callout提取 — 全部删除
2. **continuation flags 始终 False**：让 agentic workflow 修复
3. **footnote callout 留空**：让 subagent 处理
4. **函数签名精简**：不需要 pdf_path 和 cfg
5. **VLM 从 pipeline 阶段变为 agentic 工具**（方向记录，本次不实现）
6. **修复 v1 审核发现的 bug**：
   - 用 `doc.iterate_items(page_no=N)` 保证读序（CRITICAL #4）
   - 用 isinstance 分发 item type（CRITICAL #2）
   - 补全 PARAGRAPH/CODE/REFERENCE/CHART 映射（CRITICAL #5, IMPORTANT #6/#7）
   - caption 通过 item.captions RefItem 解析（IMPORTANT #12）
   - image_ref 精确推导公式（IMPORTANT #13）
   - export_to_html(doc, add_caption=False)（IMPORTANT #8）
   - 条件跳过 require_llm() 和 require_vlm()（IMPORTANT #11/#14）
