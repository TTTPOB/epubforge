# epubforge 使用说明

## 快速开始

安装依赖，配置 MinerU API key，并确认 OpenCode 已完成模型认证：

```bash
uv sync
export EPUBFORGE_MINERU_API_KEY=...
opencode --version
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

Stage 3 创建隔离目录，写入 `TASK.md` 和 `content-projection.json`。打包的
`book-editor` agent 使用 `openai/gpt-5.6-luna` medium，写出
`boundaries.json`。Python 对照源 content index、page index 和精确标题校验边界，
再原子发布 `chapters.json`。

Stage 4 在程序内部读取 source PDF，生成带 content ID、page ID 和 bbox 标记的章节
HTML，以及 `pages/page-*.jpg` 标注页面。Stage 5 只把 `chapter.html`、
`chapter.json`、HTML 引用的 assets、按页排序的 JPEG、`TASK.md` 和预置的
`corrected.html` 复制进隔离目录。Agent 编辑 `corrected.html`。Python 检查标签、
content ID、bbox、asset 引用和文件哈希，再原子发布 HTML 与 revision metadata。

Runner 在系统临时目录创建 mode 0700 工作区，不把仓库、book workdir 或 PDF 暴露给
agent。它禁用项目配置和外部 skills，拒绝 shell、subagent、外部目录、web、MCP、
skill 和 question，只允许隔离目录内的文件读取、检索与编辑。Runner 限制运行时间、
stdout、stderr、单行和输出文件大小，失败时终止进程组并清理目录。

## 配置与日志

```toml
[mineru]
api_key = "..."
model_version = "vlm"
language = "ch"

[runtime]
work_dir = "work"
log_level = "INFO"

[chapters]
render_dpi = 150
jpeg_quality = 92
```

常用环境变量如下：

```bash
EPUBFORGE_MINERU_API_KEY=...
EPUBFORGE_RUNTIME_WORK_DIR=work
EPUBFORGE_RUNTIME_LOG_LEVEL=INFO
```

epubforge 配置不接收模型 endpoint、model 或 API key。OpenCode 从自己的配置与认证
存储中取得这些信息。

日志默认写入 `work/<name>/logs/`，同时输出到 stderr：

```bash
uv run epubforge -L DEBUG parse fixtures/bmsf.pdf
```
