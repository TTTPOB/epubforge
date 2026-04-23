# epubforge 使用说明

## 总览

当前稳定架构分成两层：

1. ingestion pipeline: `parse -> classify -> extract -> assemble`
2. agentic editing layer: 以 `edit_state/` 为中心，由 supervisor 调度 scanner / fixer / reviewer

最终 `build` 优先读取 `work/<book>/edit_state/book.json`；如果尚未初始化编辑层，则回退到 `work/<book>/05_semantic.json`。

旧的 `refine-toc`、`proofread`、`footnote-verify` 已不再是 runtime stage。

## 快速开始

安装依赖：

```bash
uv sync
```

基础转换：

```bash
uv run epubforge run fixtures/example.pdf
```

产物会写入：

- `work/example/01_raw.json`
- `work/example/02_pages.json`
- `work/example/03_extract/`
- `work/example/05_semantic_raw.json`

如果只做 ingestion，到这里为止。进入编辑层有两种入口：

- 已有可作为编辑基线的 `05_semantic.json`：用 `epubforge editor init`
- 只有 `05_semantic_raw.json` 或其他 legacy artifact：用 `epubforge editor import-legacy --from ...`

## Ingestion Pipeline

### 阶段

| 阶段 | 命令 | 输出 |
|---|---|---|
| 1 | `epubforge parse` | `work/<name>/01_raw.json` |
| 2 | `epubforge classify` | `work/<name>/02_pages.json` |
| 3 | `epubforge extract` | `work/<name>/03_extract/unit_*.json` |
| 4 | `epubforge assemble` | `work/<name>/05_semantic_raw.json` |
| 8 | `epubforge build` | `out/<name>.epub` |

`run` 只会串行执行 1-4。`build` 独立运行。

### 单独运行

所有 ingestion/build 子命令都接收 PDF 路径：

```bash
uv run epubforge parse fixtures/example.pdf
uv run epubforge classify fixtures/example.pdf
uv run epubforge extract fixtures/example.pdf
uv run epubforge assemble fixtures/example.pdf
uv run epubforge build fixtures/example.pdf
```

### 从指定阶段继续

```bash
uv run epubforge run fixtures/example.pdf --from 3
uv run epubforge run fixtures/example.pdf --from 4 --force-rerun
```

`--from` 只允许 `1-4`。`--force-rerun` 会强制重跑该阶段及其后续阶段。

### 局部提取

```bash
uv run epubforge run fixtures/example.pdf --pages 1-20
```

`--pages` 主要用于抽样、调试或构造 synthetic 测试资产；不要把它和旧文档里的手工 unit 删除流程混为一谈。

## Agentic Editing Layer

编辑层的稳定命令面通过 `epubforge editor <cmd>` 调用，所有命令均需通过根 `--config <path>` 传入配置文件（或省略以使用 defaults + env）：

- `epubforge editor init`
- `epubforge editor import-legacy`
- `epubforge editor doctor`
- `epubforge editor propose-op`
- `epubforge editor apply-queue`
- `epubforge editor acquire-lease`
- `epubforge editor release-lease`
- `epubforge editor acquire-book-lock`
- `epubforge editor release-book-lock`
- `epubforge editor run-script`
- `epubforge editor compact`
- `epubforge editor snapshot`
- `epubforge editor render-prompt`

示例：

```bash
uv run epubforge --config config.toml editor init work/mybook
uv run epubforge --config config.toml editor doctor work/mybook
uv run epubforge --config config.toml editor propose-op work/mybook < ops.json
uv run epubforge --config config.toml editor apply-queue work/mybook
```

详细工作流见 [agentic-editing-howto.md](./agentic-editing-howto.md)。

规则知识库见：

- [rules/punctuation.md](./rules/punctuation.md)
- [rules/tables.md](./rules/tables.md)
- [rules/footnotes.md](./rules/footnotes.md)
- [rules/structure.md](./rules/structure.md)

## 目录结构

```text
work/
└── example/
    ├── 01_raw.json
    ├── 02_pages.json
    ├── 03_extract/
    │   ├── unit_0000.json
    │   ├── unit_0001.json
    │   └── ...
    ├── 05_semantic_raw.json
    ├── 05_semantic.json        # optional curated baseline for `editor.init`
    ├── edit_state/
    │   ├── book.json
    │   ├── meta.json
    │   ├── memory.json
    │   ├── leases.json
    │   ├── staging.jsonl
    │   ├── edit_log.jsonl
    │   ├── audit/
    │   │   ├── doctor_report.json
    │   │   └── doctor_context.json
    │   ├── scratch/
    │   └── snapshots/
    ├── images/
    └── logs/
out/
└── example.epub
```

## 配置

配置加载方式：通过 `--config <path>` 显式指定 TOML 文件，或仅使用 defaults + 环境变量（不指定 `--config` 时不会隐式读取任何 TOML 文件）。

配置优先级：

1. CLI `--log-level` / 其他 CLI override
2. 环境变量（`EPUBFORGE_*`）
3. `--config <path>` 指定的 TOML 文件
4. 内建默认值

### 最小配置

```toml
[llm]
api_key = "sk-or-..."
model = "anthropic/claude-haiku-4.5"

[vlm]
model = "google/gemini-2.5-flash-preview"
```

### 常用项

```toml
[llm]
base_url = "https://openrouter.ai/api/v1"
api_key = "sk-or-..."
model = "anthropic/claude-haiku-4.5"
timeout_seconds = 300
max_tokens = 8192
prompt_caching = true

[vlm]
base_url = "https://openrouter.ai/api/v1"
api_key = "sk-or-..."
model = "google/gemini-2.5-flash-preview"
timeout_seconds = 300
max_tokens = 8192
prompt_caching = true

[runtime]
concurrency = 4
cache_dir = "work/.cache"
work_dir = "work"
out_dir = "out"

[extract]
vlm_dpi = 200
max_simple_batch_pages = 8
max_complex_batch_pages = 12
enable_book_memory = true

[editor]
lease_ttl_seconds = 1800
compact_threshold = 50
max_loops = 50
```

### 环境变量

```bash
EPUBFORGE_LLM_API_KEY=sk-or-...
EPUBFORGE_LLM_MODEL=anthropic/claude-haiku-4.5
EPUBFORGE_VLM_MODEL=google/gemini-2.5-flash-preview
EPUBFORGE_RUNTIME_CONCURRENCY=4
EPUBFORGE_RUNTIME_LOG_LEVEL=INFO
EPUBFORGE_LLM_PROMPT_CACHING=1
```

## 日志

日志默认写入 `work/<name>/logs/run-<timestamp>.log`，同时输出到 stderr。

```bash
uv run epubforge -L DEBUG run fixtures/example.pdf
uv run epubforge -L WARNING build fixtures/example.pdf
```

## 迁移提示

- 如果你还在寻找 `refine-toc` / `proofread` / `footnote-verify`，请转到 [agentic-editing-howto.md](./agentic-editing-howto.md)。
- 如果你想知道“如何判断一本书的标点、表格、脚注、结构该怎么修”，请直接读 `docs/rules/`，不要再参考旧的人工审校 SOP。
