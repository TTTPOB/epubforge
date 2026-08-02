# epubforge 使用说明

## 快速开始

安装依赖：

```bash
uv sync
```

默认流程经过五个阶段生成经过修订的章节 HTML。Stage 1 使用 MinerU 官方 API。
配置 `EPUBFORGE_MINERU_API_KEY` 或 `[mineru].api_key`，然后运行：

```bash
uv run epubforge --config config.example.toml run fixtures/example.pdf
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

| 阶段 | 命令 | 输入 | 输出 |
|---|---|---|---|
| 1 | `epubforge parse` | PDF | `source/source.pdf`、`01_raw.zip` |
| 2 | `epubforge normalize` | `01_raw.zip`、source PDF | `02_content/content.json`、assets |
| 3 | `epubforge segment` | `02_content/content.json` | `03_chapters/chapters.json` |
| 4 | `epubforge prepare` | normalized content、chapter plan、source PDF | `04_edit/manifest.json`、chapter HTML、page JPEGs |
| 5 | `epubforge revise` | `04_edit/` | 每章 `corrected.html`、`revision.json` |

输出树示例：

```text
work/example/
├── source/source.pdf
├── 01_raw.zip
├── 02_content/content.json
├── 02_content/assets/
├── 03_chapters/chapters.json
└── 04_edit/
    ├── manifest.json
    └── chapters/0001/
        ├── chapter.html
        ├── corrected.html
        ├── revision.json
        └── pages/page-0000.jpg
```

单独运行阶段：

```bash
uv run epubforge normalize fixtures/example.pdf
uv run epubforge segment fixtures/example.pdf
uv run epubforge prepare fixtures/example.pdf
uv run epubforge revise fixtures/example.pdf
```

`run` 会串行执行 Stage 1-5：

```bash
uv run epubforge --config config.example.toml run fixtures/example.pdf
uv run epubforge --config config.example.toml run fixtures/example.pdf --from 3 --force-rerun
```

`--from` 接受 `1-5`。早期阶段会按新鲜度检查确保依赖存在；
`--force-rerun` 会强制重跑选定阶段及后续阶段。默认 `run` 不接受旧版
`--pages` 参数。Stage 5 默认在首个章节失败后停止，并保留已经发布的成功章节；
传入 `--continue-on-error` 才会继续处理后续章节。

章节阶段从 `[llm].model` 读取模型 ID。示例配置使用
`openai/gpt-5.6-luna` 选择 Luna Medium；代码不会硬编码模型。Provider 专属的
reasoning 或 variant 参数放在 `[llm].extra_body`，不要把 OpenCode 任务 variant
当作应用 API 模型设置。

旧的 `classify`、`extract`、`assemble`、`build` 命令仍保留为迁移期接口，`run`
不会调用这些命令。

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
model = "openai/gpt-5.6-luna"

[mineru]
api_key = "..."
model_version = "vlm"
language = "ch"

[runtime]
cache_dir = "work/.cache"
work_dir = "work"
out_dir = "out"

[chapters]
render_dpi = 150
jpeg_quality = 92
```

环境变量示例：

```bash
EPUBFORGE_MINERU_API_KEY=...
EPUBFORGE_LLM_API_KEY=sk-or-...
EPUBFORGE_LLM_MODEL=openai/gpt-5.6-luna
EPUBFORGE_RUNTIME_LOG_LEVEL=INFO
```

日志默认写入 `work/<name>/logs/run-<timestamp>.log`，同时输出到 stderr：

```bash
uv run epubforge -L DEBUG parse fixtures/example.pdf
```
