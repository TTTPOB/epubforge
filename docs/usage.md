# epubforge 使用说明

## 总览

当前稳定架构分成两层：

1. ingestion pipeline: `parse -> classify -> extract -> assemble`
2. agentic editing layer: 以 `edit_state/` 为中心，由 supervisor 调度 scanner / fixer / reviewer

最终 `build` 读取 `work/<book>/edit_state/book.json`。

## 快速开始

安装依赖：

```bash
uv sync
```

基础转换（默认 VLM 模式）：

```bash
uv run epubforge run fixtures/example.pdf
```

跳过 VLM（无需 provider key，使用 Docling 证据草稿）：

```bash
uv run epubforge run fixtures/example.pdf --skip-vlm
```

产物会写入：

- `work/example/01_raw.json`
- `work/example/02_pages.json`
- `work/example/03_extract/artifacts/<id>/` + `03_extract/active_manifest.json`
- `work/example/05_semantic_raw.json`

如果只做 ingestion，到这里为止。进入编辑层：用 `epubforge editor init` 初始化编辑状态。

## Ingestion Pipeline

### 阶段

| 阶段 | 命令 | 输出 |
|---|---|---|
| 1 | `epubforge parse` | `work/<name>/01_raw.json` |
| 2 | `epubforge classify` | `work/<name>/02_pages.json` |
| 3 | `epubforge extract` | `work/<name>/03_extract/artifacts/<id>/` + `active_manifest.json` |
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
uv run epubforge extract fixtures/example.pdf --skip-vlm --pages 10-12
```

`--pages` 主要用于抽样、调试或构造 synthetic 测试资产。
`extract --skip-vlm --pages` 可用于只对部分页面生成 Docling 证据草稿。

### VLM 模式与 skip-VLM 模式

Stage 3 支持两种模式：

| 模式 | 标志 | 描述 |
|---|---|---|
| VLM（默认） | 无或 `--no-skip-vlm` | 调用 VLM 对页面图像进行语义分析 |
| skip-VLM | `--skip-vlm` | 只用 Docling 机械解析，产出证据草稿 |

**provider key 门控：**

- VLM 模式需要 `[vlm]` 配置（或 `EPUBFORGE_VLM_API_KEY`）；无 key 会报错。
- skip-VLM 模式不需要任何 LLM/VLM provider key，可完全离线运行。

**skip-VLM 的语义限制：**

skip-VLM 产出的是**证据草稿**，不是最终语义。`docling_*_candidate` 角色是机械映射标签，不代表语义决策。具体而言，skip-VLM **不决定**：

- 章节边界与章节标题
- 脚注的配对与归属
- 跨页块的连续性
- 图表标题的归属
- 列表的逻辑层级
- 跨页表格合并

这些需要在 agentic editing 层由 scanner/fixer/reviewer 补全。

### Stage 4 freshness

Stage 4（assemble）读取 Stage 3 的 `active_manifest.json`。如果 `active_manifest.json`
指向的 artifact sha256 与 `05_semantic_raw.json` 中记录的 `stage3_manifest_sha256` 不一致，
Stage 4 会被视为过期，需重跑。

mode 或 pages 参数的变化会生成新的 artifact_id，进而触发 Stage 4 重跑。

### 旧 workdir 不迁移

旧格式（`03_extract/unit_*.json` 布局、无 `source/source.pdf`）的 workdir 不会被自动迁移。
需重跑完整 pipeline 生成新格式产物。

## 目录结构

```text
work/
└── example/
    ├── source/
    │   └── source.pdf          # stable whole-page render source (hardlinked/copied from input)
    ├── 01_raw.json
    ├── 02_pages.json
    ├── 03_extract/
    │   ├── active_manifest.json           # pointer to active artifact
    │   └── artifacts/
    │       └── <artifact_id>/
    │           ├── manifest.json
    │           ├── evidence_index.json
    │           └── units/
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
    ├── images/                 # figure crops only (not whole-page renders)
    └── logs/
out/
└── example.epub
```

**注意：** `source/source.pdf` 是全页渲染的唯一权威来源。`work/images/` 只存储图片 crop，
不用于整页渲染。

## Agentic Editing Layer

编辑层的稳定命令面通过 `epubforge editor <cmd>` 调用，所有命令均需通过根 `--config <path>` 传入配置文件（或省略以使用 defaults + env）：

- `epubforge editor init`
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
- `epubforge editor render-page`
- `epubforge editor vlm-page`

示例：

```bash
uv run epubforge --config config.toml editor init work/mybook
uv run epubforge --config config.toml editor doctor work/mybook
uv run epubforge --config config.toml editor propose-op work/mybook < ops.json
uv run epubforge --config config.toml editor apply-queue work/mybook

# 渲染原始 PDF 页面为图像（无 LLM/VLM 调用）
uv run epubforge --config config.toml editor render-page work/mybook --page 5

# 对指定页面重新调用 VLM（结果写入 edit_state/audit/vlm_pages/）
uv run epubforge --config config.toml editor vlm-page work/mybook --page 5
```

详细工作流见 [agentic-editing-howto.md](./agentic-editing-howto.md)。

规则知识库见：

- [rules/punctuation.md](./rules/punctuation.md)
- [rules/tables.md](./rules/tables.md)
- [rules/footnotes.md](./rules/footnotes.md)
- [rules/structure.md](./rules/structure.md)

## 配置

配置加载方式：通过 `--config <path>` 显式指定 TOML 文件，或仅使用 defaults + 环境变量（不指定 `--config` 时不会隐式读取任何 TOML 文件）。

配置优先级：

1. CLI `--log-level` / 其他 CLI override
2. 环境变量（`EPUBFORGE_*`）
3. `--config <path>` 指定的 TOML 文件
4. 内建默认值

### 最小配置（VLM 模式）

```toml
[llm]
api_key = "sk-or-..."
model = "anthropic/claude-haiku-4.5"

[vlm]
model = "google/gemini-2.5-flash-preview"
```

skip-VLM 模式不需要 `[llm]` 或 `[vlm]` 配置，可以不指定任何 provider key 运行。

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
vlm_dpi              = 200
skip_vlm             = false
max_vlm_batch_pages  = 4
enable_book_memory   = true

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
EPUBFORGE_EXTRACT_SKIP_VLM=1          # enable skip-VLM mode
EPUBFORGE_EXTRACT_MAX_VLM_BATCH_PAGES=4  # max pages per VLM batch
```

## 日志

日志默认写入 `work/<name>/logs/run-<timestamp>.log`，同时输出到 stderr。

```bash
uv run epubforge -L DEBUG run fixtures/example.pdf
uv run epubforge -L WARNING build fixtures/example.pdf
```
