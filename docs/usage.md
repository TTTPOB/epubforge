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

进程组清理是保证而非尽力而为：只有 leader 已被收割且组内没有残留进程时，
`_terminate_process_group` 才返回，否则抛出有界的 `AgentExecutionError`。当
进程组无法清空时，runner 抛出 `AgentCleanupError`，携带原始运行错误作为原因，
以及可能仍被存活的进程访问的工作区、config home 与 probe 目录路径；这些目录
**不会被删除**，会记录日志供人工清理，journal 也保留真实的运行中进程记录，
不谎报结束。config home、config probe 目录或主工作区的清理失败不会覆盖已有
的超时、解析、journal 或语义错误——它们只添加有界说明和日志，仅当它是唯一
错误时才正常抛出。正常（成功）路径上如果工作区清理单独失败，运行记录会先标记
为 `failed` 再传播清理错误，绝不留下 `started` 的 manifest 或未关闭的描述符。

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

## Agent 运行记录

Stage 3 的每次分割调用与 Stage 5 的每个章节调用都会在
`work/<name>/logs/agent-runs/<run-id>/` 留下持久化运行记录（run-id 由
UTC 时间戳加随机十六进制组成，并发运行互不冲突）。目录内包含：

- `stdout.jsonl` 与 `stderr.bin`：主 agent 进程的有界原始输出（边接收边
  落盘，不会产生无限缓冲），**仅当调用方在构造 runner 时显式传入危险的
  调试 opt-in**（`OpenCodeAgentRunner(..., capture_raw_main_streams=True)`）
  才存在。默认一律关闭：development profile 的工作区 bash 可能把继承的
  环境变量或提供商凭据回显到 stdout/stderr，未经明确选择就落盘会引入
  凭据/内容泄漏风险；生产管道与直接构造 runner 的脚本都使用默认关闭。
  关闭时主 agent 在 manifest 中标记为 `capture: "disabled"`（无流文件、
  大小或哈希），会话解析、进程结果、输出哈希与语义状态不受影响。config
  probe 进程的原始输出**从不落盘**，且没有 opt-in——它可能包含提供商
  凭据，journal 只记录该进程的时间戳、PID、返回码与通用结果（manifest
  中标记为 `capture: "disabled"`，无流文件、大小或哈希）。
- `manifest.json`：原子替换、严格版本化的 JSON 清单（
  `schema`/`schema_version`/`run_id`/`title`/agent 身份与执行 profile/UTC
  起止时间/工作区路径/输入输出元数据（名称、大小、SHA-256 与上限，绝不含
  输入内容）/每个进程的开始、结果与返回码（config probe 为
   `capture: "disabled"`，无流元数据；主 agent 默认同为 `capture: "disabled"`，
   捕获开启时含 stdout、stderr 的文件名、大小与 SHA-256）/`session_id`（解析
   到后立即记录）/输出收集状态/最终语义校验与发布状态/`finalization`（终态
   转换意图，运行中为 `null`，详见下段））。运行中与运行后的每个持久化
   manifest 都满足严格校验：流捕获开启时进程从开始即带零字节
   流记录，`capture_complete` 在进程结束前保持待定（`null`），因此宿主进程
   在开始与结束之间崩溃时，最后一份 manifest 仍是合法的“运行中/不完整捕获”
   记录。

主 agent 通信被中断时（超时、stdout/stderr 字节或行长超限、读取失败等），
manifest 会用尽力解析已接收的完整 JSONL 行来抢救 session ID（
`session_parse_mode: "best-effort"`，从不记录半行、畸形行或相互冲突的 ID）；
捕获开启时会额外把流捕获标记为不完整（`capture_complete: false` 加上限长
的非敏感原因），捕获关闭时没有流可标记，仅保留 session 抢救结果。
正常运行固定为 `session_parse_mode: "strict"`。标记 accepted/rejected 的
前提：捕获开启的运行必须捕获完整且严格解析；捕获关闭的运行依靠严格会话
解析与成功进程结果，不要求流记录。

终态取 `started`、`failed`、`rejected`、`accepted` 之一：agent 进程本身
失败（非零退出、JSON 解析失败、输出收集失败等）记为 `failed`；运行成功但
语义校验或原子发布失败记为 `rejected`（带限长的错误类型与消息）；只有
当前校验与原子发布都成功后记为 `accepted`。每次终态转换都是两阶段写入：
先原子持久化一条 `finalization` 意图（`status: "pending"`、目标终态、
尝试时间戳与有界的校验/runner 错误载荷），再原子写入终态 manifest 并把
意图标记为 `status: "completed"`。若第二次写入失败或宿主进程在两次写入
之间崩溃，最后一份持久化 manifest 保持非终态（`final_status: "started"`、
语义校验仍为 `pending`），但携带 pending 意图，明确暴露被中断的终态
目标；运行中的普通记录 `finalization` 为 `null`，因此这类记录不会被误读
为普通运行中运行。已发布的工件（如 `chapters.json`）不会因此回滚，但
manifest 绝不谎报 accepted。失败运行同样保留 manifest 与
流文件，不依赖 `chapters.json` 或 `revision.json` 是否存在。目录与文件
权限为 0700/0600，路径全程不跟随符号链接；journal 写入失败会使本次运行
明确失败，而不是悄悄丢失诊断。

直接构造 runner 的脚本或测试默认不启用记录（`journal_root` 缺省为
None）；生产管道自动指向书本的 `logs/agent-runs`。即使配置了
`journal_root`，主 agent 原始流默认也不落盘——只有构造 runner 时显式传入
`capture_raw_main_streams=True` 的调试 opt-in 才持久化流文件。运行开始时
日志会打印 manifest 路径，`session_id` 解析到后立即打印。agent 工作区在
运行结束后立即清理；当进程组无法保证清空或目录删除失败时，工作区、config
home 与 probe 路径会按上文规则保留并记录日志，供人工清理（工作区保留策略
在后续版本提供）。
