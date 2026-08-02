# epubforge 使用说明

## 快速开始

安装依赖：

```bash
uv sync
```

Stage 1 使用 MinerU 官方 API。配置 `EPUBFORGE_MINERU_API_KEY` 或
`[mineru].api_key`，然后运行：

```bash
uv run epubforge --config config.example.toml parse fixtures/example.pdf
```

Stage 1 会写入：

- `work/example/source/source.pdf`
- `work/example/source/source_meta.json`
- `work/example/01_raw.zip`，原始 MinerU API ZIP 归档

已有 `01_raw.zip` 时，parse 会复用归档。传入 `--force-rerun` 才会重新调用 MinerU。
Stage 1 会先读取完整页数，超过 1000 页的 PDF 会在请求 MinerU 前拒绝。201 至
1000 页的 PDF 会按每段最多 200 页切分；外层 `01_raw.zip` 包含
`manifest.json` 和按页码排序的 `segments/segment-NNN-pages-FFFF-LLLL.zip`，每个
segment 保留一份完整的 MinerU 响应 ZIP。200 页以内继续直接保存单份原始响应 ZIP。
Stage 1 的崩溃一致性发布要求 POSIX 运行时，以及支持目录 `fsync` 和原子重命名的本地
文件系统。Windows 会在调用 MinerU 或修改 Stage 1 文件前拒绝执行。网络文件系统的
`fsync` 和重命名语义可能不同，项目不承诺其崩溃一致性。

## Pipeline

| 阶段 | 命令 | 当前输入 | 输出 |
|---|---|---|---|
| 1 | `epubforge parse` | PDF | `source/source.pdf`、`01_raw.zip` |
| 2 | `epubforge classify` | `01_raw.json` | `02_pages.json` |
| 3 | `epubforge extract` | `01_raw.json`、`02_pages.json`、source PDF | `03_extract/` |
| 4 | `epubforge assemble` | Stage 3 active manifest | `05_semantic_raw.json` |
| 5 | `epubforge build` | `edit_state/book.json` 或 `05_semantic.json` | `out/<name>.epub` |

Stages 2-4 仍读取 `01_raw.json`。MinerU ZIP 到该旧 JSON 接口的转换留在后续任务，当前边界会让完整 pipeline 在 Stage 2 停止。

单独运行后续命令：

```bash
uv run epubforge classify fixtures/example.pdf
uv run epubforge extract fixtures/example.pdf
uv run epubforge assemble fixtures/example.pdf
uv run epubforge build fixtures/example.pdf
```

`run` 会串行执行 Stage 1-4：

```bash
uv run epubforge --config config.example.toml run fixtures/example.pdf
uv run epubforge --config config.example.toml run fixtures/example.pdf --from 3
```

`--from` 接受 `1-4`。`--force-rerun` 会强制重跑指定阶段及后续阶段。

## Editor

```bash
uv run epubforge --config config.example.toml editor init work/example
uv run epubforge --config config.example.toml editor doctor work/example
uv run epubforge --config config.example.toml editor render-prompt work/example --kind fixer --chapter <chapter_uid>
uv run epubforge --config config.example.toml editor render-page work/example --page 5
```

编辑器通过 `AgentOutput` 和 `BookPatch` 记录结构化修改。`evidence_refs` 保留为外部证据引用字段，编辑器不解释其来源。

## 配置

必须通过 `--config <path>` 显式指定 TOML 文件；省略时只加载内建默认值和环境变量。

常用设置：

```toml
[llm]
api_key = "sk-or-..."
model = "anthropic/claude-haiku-4.5"

[mineru]
api_key = "..."
model_version = "vlm"
language = "ch"

[runtime]
cache_dir = "work/.cache"
work_dir = "work"
out_dir = "out"
```

环境变量示例：

```bash
EPUBFORGE_MINERU_API_KEY=...
EPUBFORGE_LLM_API_KEY=sk-or-...
EPUBFORGE_LLM_MODEL=anthropic/claude-haiku-4.5
EPUBFORGE_RUNTIME_LOG_LEVEL=INFO
```

日志默认写入 `work/<name>/logs/run-<timestamp>.log`，同时输出到 stderr：

```bash
uv run epubforge -L DEBUG parse fixtures/example.pdf
```
