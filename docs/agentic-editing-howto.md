# Agentic Editing How-To

## 适用范围

这份文档面向 supervisor。目标不是教人手改 JSON，而是说明如何围绕当前稳定命令面运行编辑循环。

当前稳定 command surface 通过 `epubforge editor <cmd>` 调用，配置文件通过根 `--config <path>` 指定（省略时使用 defaults + env）：

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

`python -m epubforge.editor.*` 入口已废除。配置通过顶层 root callback 的 `--config` 一次性注入，所有子命令共享同一 `AppContext`。

## 角色分工

### scanner

职责：

- 通读一个 chapter
- 根据 `docs/rules/*.md` 提炼 conventions、patterns、open questions
- 只做非常确定的小改动
- 为章节增加至少一次 `read_passes`

适合运行的时机：

- `doctor` 报告某章 `needs_scan`
- 出现 `style_inconsistency` 或 `unusual_density` 提示
- 新导入的书还没有形成足够的风格记忆

不适合交给 scanner 的事：

- 大范围结构改造
- 跨章拓扑改动
- 多方案都合理的风格裁决

### fixer

职责：

- 处理已明确的问题和提示
- 在 chapter lease 保护下输出 op envelopes
- 只修自己租到的章节

适合运行的时机：

- `doctor.issues` 非空
- scanner 已经把问题缩小为确定性修复
- reviewer 已经给出裁决，剩下只是执行

### reviewer

职责：

- 仲裁 open questions
- 解决 convention 冲突
- 在必要时给出少量高确定性修正 op

适合运行的时机：

- memory 中存在 unresolved open questions
- 同一风格有多种合理解释
- 某次修复会改变全书约定而不是单章局部

## 初始化语义

### `init`

`init <work>` 会用 `work/<book>/05_semantic.json` 初始化 `edit_state/`。它要求目标工作目录还没有既存编辑状态。

适用场景：

- 你已经有当前架构下的干净 `05_semantic.json`
- 你希望从标准编辑入口开始

结果：

- 生成 `edit_state/book.json`
- 生成 `meta.json`、`memory.json`、`leases.json`
- 初始化空的 `edit_log.jsonl` 和 `staging.jsonl`
- 为 chapter / block 补全稳定 uid，并把 `book.op_log_version` 置为 0

## 核心循环

### 1. 先跑 `doctor`

`doctor <work>` 会生成并刷新：

- `edit_state/audit/doctor_report.json`
- `edit_state/audit/doctor_context.json`

它做三件事：

- 跑硬规则 detector
- 结合 memory 生成 hints
- 计算 delta 与 readiness

supervisor 每一轮都应先读 doctor 结果，再决定本轮开 scanner、fixer 还是 reviewer。

### 2. 按需分派 subagent

推荐判断方式：

- 有 `issues`：先开 fixer
- 无 `issues`，但有 `chapters_unscanned` 或扫描类 hints：开 scanner
- 有 unresolved `open_questions`：开 reviewer

### 3. 用 `render-prompt` 生成上下文

`render-prompt <work> --kind scanner|fixer|reviewer --chapter <uid>` 会把当前 `book.op_log_version`、memory 快照和 chapter 信息渲染成稳定提示词。

fixer / reviewer 可以额外通过 `--issues` 传入本轮关注的问题列表。

这一步的目的是把“规则知识”和“当前书况”绑定到同一提示里，而不是让 subagent 自己到处翻文件猜状态。

### 4. 章节内修改走 lease

对单章进行编辑前，先 `acquire-lease`；完成后 `release-lease`。

lease 的意义：

- 防止多个 fixer 同时改同一章
- 让 apply 层能拒绝越权修改

凡是只影响单章 block 的 op，都应该在 chapter lease 下完成。

### 5. 跨章拓扑改动走 book lock

`merge_chapters`、`split_chapter`、`relocate_block` 这类高影响动作，应先申请 `acquire-book-lock`，完成后再 `release-book-lock`。

`--reason` 只能是：

- `topology_op`
- `compact`
- `init`

book lock 不是普通“更强 lease”，而是整本书的独占保护。

`compact` 不属于“持锁执行”的范畴。它只能在当前没有任何 active chapter lease、且也没有 book lock 时运行。

### 6. 先暂存，再应用

subagent 产出的不是直接文件改写，而是 `OpEnvelope[]`：

1. 送入 `propose-op`
2. 再由 `apply-queue` 统一应用

`propose-op` 只负责校验并追加到 `staging.jsonl`。

`apply-queue` 才会：

- 校验 base version / preconditions / lease
- 把成功 op 写入 `edit_log.jsonl`
- 更新 `edit_state/book.json`
- 更新 `memory.json`
- 把失败 op 记入 rejected log

这套两步式流程的意义是把“生成修改”与“接受修改”分开，便于 supervisor 管理并发和回退。

## `run-script` 的语义

`run-script` 只服务于 scratch 脚本，不是常规编辑通道。

- `--write <desc>`：分配 `edit_state/scratch/` 下的新脚本路径并写入 stub
- `--exec <path>`：在项目环境中执行该脚本

`--exec` 仅接受 `scratch_dir` 内的 `.py` 文件；拒绝路径错误会通过 stdout JSON 返回。

何时使用：

- 需要做只读分析
- 需要构造辅助检查
- 需要在不污染主命令面的前提下做一次性整理

何时不该使用：

- 用它绕开 op queue 直接改 `book.json`
- 把它当成长期工作流的主要入口

## `snapshot`、`compact`、`revert`

### `snapshot`

`snapshot <work> --tag <name>` 会把当前 `edit_state/` 复制到 `edit_state/snapshots/<tag>/`。

它是检查点，不会改变当前工作状态，也不是回退命令。命令会拒绝覆盖已存在的同名 tag。适合在以下时机使用：

- 大批量修复前
- reviewer 做出全书风格裁决后
- compact 前后

### `compact`

`compact <work>` 会把当前已接受的 edit log 归档到 `edit_state/log.archive/<timestamp>/`，并在新的 `edit_log.jsonl` 里留下一个 `compact_marker`。

它的语义是“压缩日志历史”，不是“冻结书稿”：

- 不改变 `book.json` 内容
- 需要当前没有任何 active chapter lease，也没有 book lock
- 历史 op 仍可通过索引定位到归档日志

适合时机：

- 已接受 op 数量很多
- supervisor 想把后续回合建立在更短的当前日志上

### `revert`

当前没有独立的 `revert` CLI。回退是一个 op envelope：

- `op = {"op": "revert", "target_op_id": "..."}`

它和普通 op 一样，先经 `propose-op`，再经 `apply-queue`。

应用 `revert` 时，系统会：

- 校验目标 op 存在且尚未被回退
- 生成对应的 inverse op
- 记录 revert backref

因此，`revert` 的语义是“通过日志反操作回退某条历史 op”，不是把整个工作目录回滚到某个 snapshot。

## 收敛怎么判断

当前 `doctor` 的收敛条件是四项同时满足：

1. `issues` 为空
2. 没有未扫描章节
3. 没有 unresolved open questions
4. 连续两轮 `doctor` 都没有新增 convention、pattern，也没有新应用的 op

满足后，`doctor.readiness.converged` 会为真。

实践上，supervisor 应把它理解为“可以停”的信号，而不是“必须停”的命令。若你刚做完重大 reviewer 裁决，通常还值得再跑一轮确认 doctor delta 已静默。

## 推荐节奏

一个典型回合应当长这样：

1. 跑 `doctor`
2. 读取 `issues`、`hints`、`readiness`、`delta`
3. 按需获取 chapter lease 或 book lock
4. 用 `render-prompt` 生成 subagent 提示
5. 收集 `OpEnvelope[]`
6. `propose-op`
7. `apply-queue`
8. 释放 lease / lock
9. 再跑 `doctor`

如果 `delta.quiet_round_streak` 长期增长、当前 log 已很长，可以在没有 active lease / lock 的空闲窗口做 `snapshot` 或 `compact`。

## 与规则文档的关系

本 howto 只回答“怎么 orchestrate”。至于“什么叫正确的标点、表格、脚注、结构判断”，必须回到：

- [rules/punctuation.md](./rules/punctuation.md)
- [rules/tables.md](./rules/tables.md)
- [rules/footnotes.md](./rules/footnotes.md)
- [rules/structure.md](./rules/structure.md)

supervisor 不应把 howto 当成规则来源，也不应让 subagent 在缺少规则上下文时直接修书。
