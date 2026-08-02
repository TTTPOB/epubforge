# epubforge 使用说明

## 快速开始

安装依赖并配置两个 API key：

```bash
uv sync
export EPUBFORGE_MINERU_API_KEY=...
export EPUBFORGE_LLM_API_KEY=...
uv run epubforge --config config.example.toml run fixtures/bmsf.pdf
```

`--config` 必须指向明确的 TOML 文件。省略时，程序只读取默认值和环境变量，
不会扫描当前目录中的配置文件。

## 五个阶段

| 阶段 | 命令 | 输入 | 输出 |
|---|---|---|---|
| 1 parse | `parse` | PDF | `source/source.pdf`、`source/source_meta.json`、`01_raw.zip` |
| 2 normalize | `normalize` | `01_raw.zip`、source PDF | `02_content/content.json`、`assets/` |
| 3 segment | `segment` | `content.json` | `03_chapters/chapters.json` |
| 4 prepare | `prepare` | content、chapter plan、source PDF | `04_edit/manifest.json`、章节 HTML、标注页面 JPEG |
| 5 revise | `revise` | `04_edit/` | 每章 `corrected.html`、`revision.json` |

`run` 按顺序执行五个阶段：

```bash
uv run epubforge --config config.example.toml run fixtures/bmsf.pdf
uv run epubforge --config config.example.toml run fixtures/bmsf.pdf --from 3 --force-rerun
```

`--from` 接受 `1` 到 `5`。程序复用更早阶段的新鲜输出；加入
`--force-rerun` 后，程序从指定阶段起重建输出。每个单阶段命令也接受
`--force-rerun` 或 `-f`。Stage 5 默认遇到首个章节错误就停止，同时保留已经成功
发布的章节；传入 `--continue-on-error` 才会继续后续章节。

## 输出目录

```text
work/bmsf/
├── source/
│   ├── source.pdf
│   └── source_meta.json
├── 01_raw.zip
├── 02_content/
│   ├── content.json
│   └── assets/
├── 03_chapters/chapters.json
└── 04_edit/
    ├── manifest.json
    └── chapters/0001/
        ├── chapter.json
        ├── chapter.html
        ├── corrected.html
        ├── revision.json
        └── pages/page-0000.jpg
```

Stage 1 直接保存 MinerU 返回的 ZIP。200 页以内保存一份原始响应；201 到 1000
页的 PDF 会按每段最多 200 页上传，`01_raw.zip` 会保存外层
`manifest.json` 和完整的分段响应 ZIP。程序最多接受 1000 页。

Stage 1 发布结果要求 POSIX 环境、目录 `fsync` 和原子重命名。程序会先检查这些能力，
再读取 PDF 或调用 MinerU。网络文件系统不提供项目承诺的崩溃一致性保证。

## Agent 页面工作区

Stage 4 在程序内部读取 source PDF，生成带 content ID、page ID 和 bbox 标记的章节
HTML，以及 `pages/page-*.jpg` 标注页面。章节 agent 只接收这些 HTML 和 JPEG 文件，
不会打开、渲染或访问 PDF。

Luna 在 Stage 3 只返回经过校验的章节边界，在 Stage 5 只返回修订后的 HTML。程序会
检查边界、标签、content ID、bbox 和文件哈希，再原子发布结果。

## 配置与日志

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
log_level = "INFO"

[chapters]
render_dpi = 150
jpeg_quality = 92
```

常用环境变量如下：

```bash
EPUBFORGE_MINERU_API_KEY=...
EPUBFORGE_LLM_API_KEY=sk-or-...
EPUBFORGE_LLM_MODEL=openai/gpt-5.6-luna
EPUBFORGE_RUNTIME_WORK_DIR=work
EPUBFORGE_RUNTIME_CACHE_DIR=work/.cache
EPUBFORGE_RUNTIME_LOG_LEVEL=INFO
```

日志默认写入 `work/<name>/logs/`，同时输出到 stderr：

```bash
uv run epubforge -L DEBUG parse fixtures/bmsf.pdf
```
