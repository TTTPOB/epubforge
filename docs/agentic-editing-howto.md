# Agentic Editing How-To

## 适用范围

这份文档面向 supervisor。目标不是教人手改 JSON，而是说明如何围绕当前稳定命令面运行编辑循环。

当前稳定 command surface 通过 `epubforge editor <cmd>` 调用，配置文件通过根 `--config <path>` 指定（省略时使用 defaults + env）：

- `epubforge editor init`
- `epubforge editor doctor`
- `epubforge editor agent-output begin`
- `epubforge editor agent-output add-patch`
- `epubforge editor agent-output add-command`
- `epubforge editor agent-output add-memory-patch`
- `epubforge editor agent-output validate`
- `epubforge editor agent-output submit`
- `epubforge editor run-script`
- `epubforge editor compact`
- `epubforge editor snapshot`
- `epubforge editor render-prompt`
- `epubforge editor render-page`
- `epubforge editor vlm-page`

`python -m epubforge.editor.*` 入口已废除。配置通过顶层 root callback 的 `--config` 一次性注入，所有子命令共享同一 `AppContext`。

## skip-VLM 证据草稿与语义修复

当 Stage 3 以 `--skip-vlm` 模式运行时，产出的块带有 `Provenance.source="docling"`，角色标签为 `docling_*_candidate`（如 `docling_heading_candidate`、`docling_footnote_candidate`）。这些 candidate 角色是机械映射标签，不是语义决策。

fixer 通过以下新工作流修复语义：

- `BookPatch.replace_node`：替换块内容或角色
- `BookPatch.set_field`：标记跨页连续性、修复表格标题/说明等字段
- `PatchCommand`：表达拆分/合并/搬移/脚注配对等拓扑类修复

`vlm-page` 只读地产生页面证据，supervisor 需要手动解读结果，再通过 `agent-output` 工作流更新书稿。

## 初始化语义

`init <work>` 会用 `work/<book>/05_semantic.json` 初始化 `edit_state/`。它要求目标工作目录还没有既存编辑状态。

结果：

- 生成 `edit_state/book.json`
- 生成 `meta.json`（含 `stage3` 上下文，如果 Stage 3 产物存在）
- 生成 `memory.json`
- 初始化空的 `edit_log.jsonl`
- 创建 `agent_outputs/`、`scratch/`、`snapshots/` 等目录
- 为 chapter / block 补全稳定 uid

## 核心循环

1. 先跑 `doctor <work>`，读取 readiness、issues 和 hints。
2. 用 `render-prompt <work> --kind scanner|fixer|reviewer --chapter <uid>` 生成当前 memory、chapter 和 patch workflow 指引。
3. 用 `agent-output begin` 创建本轮 AgentOutput。
4. 将 subagent 结果写入 AgentOutput：
   - 字段/节点级变更：`agent-output add-patch --patch-file patch.json`
   - 拓扑宏：`agent-output add-command --command-file command.json`
   - 记忆更新：`agent-output add-memory-patch --patch-file memory_patch.json`
5. 用 `agent-output validate` 做无副作用校验。
6. 用 `agent-output submit --apply` 事务性提交；或用 `--stage` 仅校验并归档。
7. 再跑 `doctor`，决定下一轮。

## AgentOutput 提交流程

AgentOutput 是唯一的 agent 写入入口。它可包含：

- `patches`: 直接的 `BookPatch`
- `commands`: 编译为 `BookPatch` 的 `PatchCommand`
- `memory_patches`: 合并进 `memory.json` 的记忆补丁
- `open_questions` / `notes` / `evidence_refs`

`submit --apply` 的顺序为：验证 AgentOutput → 编译 commands → 应用编译出的 patches → 应用直接 patches → 合并 memory → 归档 AgentOutput → 写入 audit log。任何验证或应用失败都不会部分写入 `book.json`。

## 角色分工

### scanner

- 通读一个 chapter
- 根据 `docs/rules/*.md` 提炼 conventions、patterns、open questions
- 只提交非常确定的 `set_field` 修正
- 为章节增加至少一次 `read_passes`

### fixer

- 处理已明确的问题和提示
- 优先使用 chapter-scoped BookPatch；拓扑类动作使用 PatchCommand
- 不做多方案都合理的风格裁决

### reviewer

- 仲裁 open questions
- 解决 convention 冲突
- 必要时提交少量高确定性 `set_field` 修正

## render-page 与 vlm-page

```bash
# 渲染第 5 页为 JPEG，写入 edit_state/audit/page_images/page_0005.jpg
epubforge --config config.toml editor render-page work/mybook --page 5

# 对第 5 页重新调用 VLM，结果写入 edit_state/audit/vlm_pages/page_0005.json
epubforge --config config.toml editor vlm-page work/mybook --page 5
```

`render-page` 不消耗 LLM/VLM token。`vlm-page` 只处理 Stage 3 已选中页面，结果不会自动修改 `book.json`。

## 收敛与归档

- `doctor` 连续 quiet round 后可认为当前轮次收敛。
- `snapshot` 复制当前 `edit_state/` 到 `snapshots/<tag>/`。
- `compact` 归档当前 audit log 并写入 compact marker，不修改书稿内容。
