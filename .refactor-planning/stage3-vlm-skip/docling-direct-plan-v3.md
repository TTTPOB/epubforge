# Docling-Direct Extraction Plan — v3

## Design Philosophy

- docling 提取原始文本和粗粒度标签，直接转为 unit JSON
- **不做任何启发式推断**（标题层级、跨页续接、脚注callout提取）
- 所有需要"判断"的事情交给 agentic workflow 的 subagent
- VLM 从 pipeline 必经阶段变为 agentic workflow 的工具

## 文件变更总览

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/epubforge/extract_docling.py` | **新建** | ~150 行，docling-direct 提取 |
| `src/epubforge/config.py` | **修改** | 加 extract_mode 字段 |
| `src/epubforge/pipeline.py` | **修改** | run_extract 分发 |
| `src/epubforge/cli.py` | **修改** | --extract-mode 选项 |
| `tests/test_extract_docling.py` | **新建** | 单元测试 |

不修改：assembler.py, extract.py, ir/semantic.py, classifier.py

## 1. 新模块：`src/epubforge/extract_docling.py`

### 主函数

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

不需要 pdf_path（不渲染页面），不需要 cfg（不调用 API）。

### Item 分发：用 item.label 而非 isinstance

**关键约束**：SectionHeaderItem, ListItem, FormulaItem, CodeItem 都是 TextItem 子类。如果用 isinstance 分发且顺序写反，具体类型会被 TextItem 分支拦截。

**解决方案**：统一用 `item.label` 判断，彻底消除继承顺序问题：

```python
def _map_item_to_block(item: DocItem, doc: DoclingDocument, page_no: int) -> dict | None:
    label = item.label

    if label == DocItemLabel.SECTION_HEADER:
        level = max(1, min(6, getattr(item, 'level', 2)))
        return {"kind": "heading", "level": level, "text": item.text, "page": page_no}

    if label == DocItemLabel.TITLE:
        return {"kind": "heading", "level": 1, "text": item.text, "page": page_no}

    if label in (DocItemLabel.TEXT, DocItemLabel.PARAGRAPH, DocItemLabel.REFERENCE):
        return {"kind": "paragraph", "text": item.text, "page": page_no}

    if label == DocItemLabel.FOOTNOTE:
        return {"kind": "footnote", "callout": "", "text": item.text, "page": page_no}

    if label == DocItemLabel.LIST_ITEM:
        marker = getattr(item, 'marker', '-')
        return {"kind": "paragraph", "text": f"{marker} {item.text}", "page": page_no}

    if label == DocItemLabel.TABLE:
        html = item.export_to_html(doc, add_caption=False)
        return {"kind": "table", "html": html, "page": page_no}

    if label in (DocItemLabel.PICTURE, DocItemLabel.CHART):
        image_ref = _derive_image_ref(item, page_no)
        caption = _resolve_caption(item, doc)
        return {"kind": "figure", "image_ref": image_ref, "caption": caption, "page": page_no}

    if label == DocItemLabel.FORMULA:
        return {"kind": "equation", "latex": item.text, "page": page_no}

    if label == DocItemLabel.CODE:
        return {"kind": "paragraph", "text": item.text, "page": page_no}

    if label in (DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER, DocItemLabel.CAPTION):
        return None

    # 未知 label 保底：输出为 paragraph 保留内容
    text = getattr(item, 'text', '')
    if text:
        return {"kind": "paragraph", "text": text, "page": page_no}
    return None
```

### image_ref 推导（必须与 docling_parser.py 完全一致）

```python
def _derive_image_ref(item: DocItem, page_no: int) -> str:
    ref_id = item.self_ref.replace("/", "_").replace("#", "_").lstrip("_")
    return f"images/p{page_no:04d}_{ref_id}.png"
```

### caption 解析（不使用 doc.get_ref_item —— 该 API 不存在）

DoclingDocument 没有 `get_ref_item()` 方法。`item.captions` 是 `list[RefItem]`，每个 `RefItem` 有 `$ref` 字段是 JSON Pointer（如 `#/texts/15`）。需要手动解析：

```python
def _resolve_caption(item: DocItem, doc: DoclingDocument) -> str:
    captions = getattr(item, 'captions', None)
    if not captions:
        return ""
    texts = []
    for ref in captions:
        ref_str = ref.cref if hasattr(ref, 'cref') else getattr(ref, '$ref', '')
        # JSON Pointer: #/texts/15 -> doc.texts[15]
        parts = ref_str.lstrip('#/').split('/')
        if len(parts) == 2:
            collection_name, index = parts[0], parts[1]
            try:
                collection = getattr(doc, collection_name, None)
                if collection and index.isdigit():
                    ref_item = collection[int(index)]
                    if hasattr(ref_item, 'text'):
                        texts.append(ref_item.text)
            except (IndexError, AttributeError):
                pass
    return " ".join(texts)
```

**注意**：这段代码需要在实现时验证 docling 的 RefItem 实际字段名（可能是 `cref`、`ref`、或 `$ref`）。实现时应先 print 一个 RefItem 确认。

### 页面遍历：必须用 iterate_items

```python
def _convert_page(doc: DoclingDocument, page_no: int) -> list[dict]:
    blocks = []
    for item, _level in doc.iterate_items(page_no=page_no):
        block = _map_item_to_block(item, doc, page_no)
        if block is not None:
            blocks.append(block)
    return blocks
```

`iterate_items(page_no=N)` 按文档树读序遍历，交叉返回 texts/tables/pictures。不能用 `itertools.chain(doc.texts, doc.tables, ...)` — 那样所有类型分组排列，顺序错误。

### 主流程

```python
def extract_docling(raw_path, pages_path, out_dir, *, force=False, page_filter=None):
    doc = DoclingDocument.load_from_json(raw_path)
    pages_data = json.loads(pages_path.read_text("utf-8"))["pages"]

    # 过滤 TOC 页
    pages_data = [p for p in pages_data if p["kind"] != "toc"]
    if page_filter is not None:
        pages_data = [p for p in pages_data if p["page"] in page_filter]

    for idx, page_info in enumerate(pages_data):
        out_path = out_dir / f"unit_{idx:04d}.json"
        if out_path.exists() and not force:
            log.info("extract unit %d/%d page=%d reused=Y", idx+1, len(pages_data), page_info["page"])
            continue

        pno = page_info["page"]
        blocks = _convert_page(doc, pno)

        data = {
            "unit": {"kind": "vlm_group", "pages": [pno]},
            "first_block_continues_prev_tail": False,
            "first_footnote_continues_prev_footnote": False,
            "blocks": blocks,
            "audit_notes": [],
        }
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("extract unit %d/%d page=%d blocks=%d", idx+1, len(pages_data), pno, len(blocks))
```

## 2. 修改：`src/epubforge/config.py`

在 `ExtractSettings` 加字段：
```python
extract_mode: Literal["vlm", "docling"] = "vlm"
```

在 `_ENV_MAP` 加一行：
```python
("EPUBFORGE_EXTRACT_MODE", "extract", "extract_mode", str),
```

## 3. 修改：`src/epubforge/pipeline.py`

`run_extract()` 分发：

```python
def run_extract(pdf_path, cfg, *, force=False, pages=None):
    work = cfg.book_work_dir(pdf_path)
    raw = _stage_path(work, "01_raw.json")
    pages_json = _stage_path(work, "02_pages.json")
    out_dir = work / "03_extract"
    out_dir.mkdir(parents=True, exist_ok=True)

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
    log.info("  -> %s/", out_dir)
```

## 4. 修改：`src/epubforge/cli.py`

在 `run` 和 `extract` 命令加 `--extract-mode` 选项：

```python
extract_mode: str | None = typer.Option(
    None, "--extract-mode",
    help="'vlm' (default) or 'docling' (skip VLM, use docling output directly)"
),
```

应用到 cfg 并条件跳过 API check：

```python
if extract_mode is not None:
    cfg = cfg.model_copy(update={
        "extract": cfg.extract.model_copy(update={"extract_mode": extract_mode})
    })
if cfg.extract.extract_mode != "docling":
    cfg.require_llm()
    cfg.require_vlm()
```

## 5. 测试：`tests/test_extract_docling.py`

- 各 label 到 block kind 的映射正确性
- TOC 页跳过
- page_filter 过滤
- image_ref 推导公式与 docling_parser.py 一致
- unit JSON schema 符合 assembler 预期
- PAGE_HEADER/PAGE_FOOTER 被跳过
- 未知 label 的兜底行为

## 6. 待决策点（不阻塞开发，暂定选项标注 ★）

| # | 问题 | 选项 | 暂定 |
|---|---|---|---|
| D1 | unit.kind 用什么值 | A: "vlm_group"（兼容）B: "docling_page"（语义准，要改 assembler） | ★A |
| D2 | 默认 extract_mode | A: "vlm"（向后兼容）B: "docling"（推荐新用户） | ★A |
| D3 | RefItem 字段名 | 实现时验证 cref/$ref/ref | 实现时确认 |

## v2 → v3 的修复

1. **CRITICAL 修复**: caption 解析不用 `doc.get_ref_item()`（不存在），改为手动解析 JSON Pointer
2. **IMPORTANT 修复**: item 分发改用 `item.label` 而非 isinstance，消除继承顺序陷阱
3. **IMPORTANT 修复**: 明确 pipeline.run_extract 必须在 run_classify 之后执行（02_pages.json 依赖）— 这已由 run_all() 的顺序保证
