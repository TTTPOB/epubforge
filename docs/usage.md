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
| 4 prepare | `prepare` | content、chapter plan、source PDF | `04_edit/manifest.json`、章节 HTML、干净与标注页面 JPEG、每页几何契约 |
| 5 revise | `revise` | `04_edit/` | 每章 `corrected.html`、`revision.json`、已应用的 bbox 修复 |

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
        └── pages/
            ├── page-0000.jpg
            ├── page-0000.annotated.jpg
            └── page-0000.meta.json
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
HTML。每页同时产出三种文件：干净页面图 `pages/page-NNNN.jpg`、带 bbox 标注的
`pages/page-NNNN.annotated.jpg`，以及几何契约 `pages/page-NNNN.meta.json`
（记录 page_idx、canonical 宽高、raster 像素宽高和换算比例）。页面只渲染一次，
两种 JPEG 与契约文件共享同一份渲染结果。

Stage 5 只把 `chapter.html`、`chapter.json`、HTML 引用的 assets、按页排序的干净图、
标注图、meta 契约、`TASK.md` 和预置的 `corrected.html` 复制进隔离目录。Agent
编辑 `corrected.html`，并可对可疑 bbox 批量调用本地 `epubforge-paddle-page`
CLI（只接受工作区内的干净页面图，不接触 PDF）：

```bash
epubforge-paddle-page --image pages/page-0001.jpg --page-idx 1 \
  --image pages/page-0002.jpg --page-idx 2 \
  --output .paddle --evidence-out paddle-evidence.json
```

CLI 输出 `evidence.json`、`candidate.json` 和证据 JPEG，缓存原始检测；
`--reuse-raw` 可调整后处理参数后免推理重跑，`--page-patch` 可应用 agent 自己
写的补丁。Agent 在 `bbox-repairs.json` 中只引用候选 ID，绝不手写坐标：Python
依据 `paddle-evidence.json` 中的候选像素框和 `pages/page-NNNN.meta.json` 的换算
契约，把候选换算成 canonical 页面坐标、应用到 `corrected.html`，并把应用结果
（目标、操作、候选 ID、最终 bbox）记录进 `revision.json`。没有可靠候选时，agent
保留原 bbox 值并加 `data-bbox-status="needs-repair"` 作为降级。

Runner 在系统临时目录创建 mode 0700 工作区，不把仓库、book workdir 或 PDF 暴露给
agent。它禁用项目配置和外部 skills，拒绝 subagent、外部目录、web、MCP、skill 和
question。`[runtime] execution_profile` 控制工作区内的 bash：Stage 3 segmentation
始终使用 `restricted`（拒绝命令）；Stage 5 revision 使用配置的 profile——
`development`（默认）在工作区内打开本地 bash，供 agent 调用
`epubforge-paddle-page`；`restricted` 保持旧版完全拒绝命令的权限。development
的 bash 是有意信任的模式，不是 OS 沙盒：agent 依靠提示词约束留在工作区内，未来
可用更强的沙盒替换。Runner 限制运行时间、stdout、stderr、单行和输出文件大小，
失败时终止进程组并清理目录。

## 配置与日志

```toml
[mineru]
api_key = "..."
model_version = "vlm"
language = "ch"

[runtime]
work_dir = "work"
log_level = "INFO"
execution_profile = "development"  # development（开工作区内 bash，供 Paddle CLI 使用）| restricted（旧版权限）

[chapters]
render_dpi = 150
jpeg_quality = 92
```

常用环境变量如下：

```bash
EPUBFORGE_MINERU_API_KEY=...
EPUBFORGE_RUNTIME_WORK_DIR=work
EPUBFORGE_RUNTIME_LOG_LEVEL=INFO
EPUBFORGE_RUNTIME_EXECUTION_PROFILE=restricted
```

execution_profile 参与 agent fingerprint：切换 profile 会让对应阶段判定为不新鲜并
重跑；Stage 3 固定使用 restricted，不受此配置影响。epubforge 配置不接收模型
endpoint、model 或 API key。OpenCode 从自己的配置与认证存储中取得这些信息。

日志默认写入 `work/<name>/logs/`，同时输出到 stderr：

```bash
uv run epubforge -L DEBUG parse fixtures/bmsf.pdf
```
