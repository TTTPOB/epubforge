# Agentic Editing Improvement Plan

## Background

epubforge 的目标不是一次性把 PDF 转成 EPUB，而是产出能给人类阅读的 EPUB。多数真实书籍都需要 LLM/agent 参与后期修订，所以 editor 子系统不是附属能力，而是核心产品路径。

当前讨论收敛出的核心判断：

- editor 必须保留，并应围绕“修书工作流”而不是“底层操作系统接口”组织。
- VLM 不应再作为 ingestion pipeline 的 Stage 3 分支，而应降级为 editor 阶段可按需调用的 evidence 工具。
- Codex/Claude Code 这类订阅式 agent 无法保证 structured output，因此不能依赖它们手写最终 JSON。
- ~~lease/lock 机制仍然有价值~~ → 已决定移除 lease/lock，改用 Git worktree 做并发隔离（见 D1）。

## Current Problems

### 1. Agent output has no stable top-level schema

当前已有稳定结构：

- `OpEnvelope`（current implementation）
- `EditOp`（current implementation）
- `MemoryPatch`
- `OpenQuestion`
- `ChapterStatus`
- `ConventionNote`
- `PatternNote`

但没有稳定的顶层 `AgentOutput` 模型。

prompt 里只是文字要求 agent 输出：

```json
{
  "commands": [],
  "patches": [],
  "memory_patches": [],
  "open_questions": [],
  "notes": []
}
```

这不是代码层面的 schema，也没有统一 validator/submitter。

结果是：

- scanner/fixer/reviewer 的完整工作包无法统一校验。
- notes 不会被系统正式接收或归档。
- supervisor 需要手动拆分 agent 输出，再喂给当前 `propose-op`。
- 当前 `propose-op` 只能校验 `OpEnvelope[]`，覆盖不了完整 agent output。

### 2. Agent-written JSON is unreliable

在 Codex/Claude Code 这类界面下，agent 可以推理、读文件、调用命令，但不能保证最终聊天输出或手写 JSON 永远合法。

风险包括：

- JSON 语法不合法。
- 字段名漂移。
- `base_version` 过期。
- uid 不存在。
- 修改范围越过 chapter lease。
- scanner/fixer/reviewer 输出形状不一致。

因此应该让 agent 使用本地命令增量构造结构化产物，而不是自己拼最终 JSON。

### 3. ~~Lease only renews on explicit acquire~~ (RESOLVED: lease system removed, see D1)

### 4. ~~Lock failure happens at apply time~~ (RESOLVED: lease system removed, see D1)

### 5. VLM is overloaded as a pipeline stage

当前 Stage 3 有 VLM mode 和 skip-VLM mode。这个分支让 ingestion pipeline 同时承担两种职责：

- 机械抽取草稿。
- 视觉语义判断。

但如果目标是人类可读 EPUB，语义判断应该发生在 editor 修书阶段。VLM 更适合作为 scanner/fixer/reviewer 可调用的 evidence 工具，而不是 ingestion 的默认 stage。

### 6. The current EditOp surface is expensive to maintain

当前 `EditOp` 同时承担两类职责：

- 表达 agent 的高层意图，例如 split block、merge chapters、pair footnote。
- 表达低层 IR 变化，例如 set text、set role、set heading level、set table metadata。

这会让 operation 类型、apply 分支、validator 和测试持续膨胀。很多低层字段修改本质上都是同一种事情：对某个 UID-addressed node 的某个字段做带 precondition 的替换。

更好的方向是：

- 保留少量底层 atomic patch 能力。
- 在其上提供 ergonomic high-level commands/macros。
- 推荐 agent 使用高层命令。
- 高层命令无法表达时，才允许 agent 组合底层 atomic changes。

命名决策：

- 目标设计统一使用 `Patch` 术语。
- 底层机器可应用结构叫 `BookPatch`。
- 高层人体/agent 友好的意图叫 `PatchCommand`。
- 不设计 `Op` → `Patch` 的兼容层；实施时可以直接替换当前 operation terminology。

## Target Design

### 1. Introduce AgentOutput as the top-level contract

新增统一模型，覆盖 scanner/fixer/reviewer/supervisor 的提交产物。

建议结构：

```python
class AgentOutput(BaseModel):
    output_id: str
    kind: Literal["scanner", "fixer", "reviewer", "supervisor"]
    agent_id: str
    chapter_uid: str | None = None
    base_version: int
    created_at: str
    updated_at: str
    patches: list[BookPatch] = []
    commands: list[PatchCommand] = []
    memory_patches: list[MemoryPatch] = []
    open_questions: list[OpenQuestion] = []
    notes: list[str] = []
    evidence_refs: list[str] = []
```

重要点：

- 这个模型是系统内部契约。
- agent 不应直接手写完整 JSON。
- 所有 agent 产物最终都经过同一套 validate/submit 流程。

### 2. Build AgentOutput through commands, not manual JSON

新增一组命令，让 agent 通过小粒度命令构造 output。

示例：

```bash
epubforge editor agent-output begin work/book \
  --kind scanner \
  --chapter ch-1 \
  --agent scanner-1
```

```bash
epubforge editor agent-output add-note work/book <output-id> \
  --text "第 12 页脚注密度异常，需要复查 callout 归属。"
```

```bash
epubforge editor agent-output add-question work/book <output-id> \
  --question "第 12 页脚注 ③ 是否应归属上一段？" \
  --context-uid block-abc
```

```bash
epubforge editor agent-output add-command work/book <output-id> \
  --command-file scratch/command.json
```

```bash
epubforge editor agent-output add-patch work/book <output-id> \
  --patch-file scratch/patch.json
```

```bash
epubforge editor agent-output validate work/book <output-id>
epubforge editor agent-output submit work/book <output-id>
```

这样 agent 仍然使用订阅界面完成推理，但结构化数据由本地 CLI 维护。

### 3. Validate AgentOutput before submission

`validate` 应检查：

- output 文件是合法 JSON。
- 顶层 schema 合法。
- `kind`、`agent_id`、`chapter_uid` 合法。
- `base_version == current book.op_log_version`，或提供明确的 stale 处理。
- `commands` 全部是合法 `PatchCommand`，并能编译成 `BookPatch`。
- `patches` 全部是合法 `BookPatch`。
- `memory_patches` 全部是合法 `MemoryPatch`。
- `open_questions` 全部是合法 `OpenQuestion`。
- 所有 block/chapter uid 存在。
- 修改范围不越过当前 agent 的 chapter 或 book lock。
- scanner 完成扫描时必须更新对应 `chapter_status.read_passes`。
- topology patch 必须由 supervisor 或明确授权流程提交。

### 4. Submit AgentOutput through one command

`submit` 应成为统一入口。

建议语义：

```bash
epubforge editor agent-output submit work/book <output-id> --stage
epubforge editor agent-output submit work/book <output-id> --apply
```

`--stage`：

- validate output。
- 将合法 commands / patches 追加到 staging。
- 归档完整 AgentOutput。
- 不修改 `book.json`。

`--apply`：

- validate output。
- 将 commands 编译成 patches，并将 patches 应用到 `book.json`。
- 应用 memory patches。
- 归档完整 AgentOutput。
- 成功后续租。

目标设计中不保留 `propose-op` 作为兼容入口。新的低层入口应是 `patch validate/apply`，普通 agent workflow 应走 `agent-output submit`。

### 5. Introduce UID-addressed BookPatch as the atomic edit layer

新增 `BookPatch` 作为底层修改语言，用 UID 而不是 JSON array index 定位节点。

目标不是暴露裸 RFC 6902 JSON Patch，而是采用 JSON Patch 的思想：小而通用的 atomic operations，加上 epubforge 的 Book IR 语义校验。

建议结构：

```python
class BookPatch(BaseModel):
    patch_id: str
    agent_id: str
    base_version: int
    scope: PatchScope
    changes: list[IRChange]
    rationale: str
    evidence_refs: list[str] = []

class PatchScope(BaseModel):
    chapter_uid: str | None = None
    book_wide: bool = False
```

底层 change 集合保持很小：

```python
class SetFieldChange(BaseModel):
    op: Literal["set_field"]
    target_uid: str
    field: str
    old: object
    new: object

class ReplaceNodeChange(BaseModel):
    op: Literal["replace_node"]
    target_uid: str
    old_node: dict[str, object]
    new_node: dict[str, object]

class InsertNodeChange(BaseModel):
    op: Literal["insert_node"]
    parent_uid: str | None
    after_uid: str | None
    node: dict[str, object]

class DeleteNodeChange(BaseModel):
    op: Literal["delete_node"]
    target_uid: str
    old_node: dict[str, object]

class MoveNodeChange(BaseModel):
    op: Literal["move_node"]
    target_uid: str
    from_parent_uid: str | None
    to_parent_uid: str | None
    after_uid: str | None
```

这套底层 patch 应能表达任意合法 Book IR 状态变化：

- 字段变化：`set_field`
- 节点新增：`insert_node`
- 节点删除：`delete_node`
- 节点移动：`move_node`
- 节点整体替换：`replace_node`

对于大范围移动或批量操作，可以额外提供 ergonomic batch change，例如 `move_block_range`。这不是表达能力必需，但能降低 agent 输出噪音。

### 6. Keep high-level ergonomic PatchCommands as macros

高层能力仍然重要，因为 agent 和人类 supervisor 更容易理解“为什么改”，而不是只看底层字段变化。

建议把当前一部分 `EditOp` 替换为 macro-style `PatchCommand`：

- `split_block`
- `merge_blocks`
- `split_chapter`
- `merge_chapters`
- `relocate_block`
- `pair_footnote`
- `unpair_footnote`
- `mark_orphan`
- `split_merged_table`

这些 high-level commands 不再各自拥有复杂 apply 逻辑，而是：

```text
PatchCommand
        ↓ compile
UID-addressed BookPatch
        ↓ validate
apply patch
```

普通字段修改则优先收敛到 `set_field` / `replace_node`：

- `set_text`
- `set_role`
- `set_style_class`
- `set_heading_level`
- `set_heading_id`
- `set_footnote_flag`
- `set_paragraph_cross_page`
- `set_table_metadata`

推荐 agent 工作方式：

- 优先使用高层 ergonomic command。
- 如果没有对应 command，再使用底层 `BookPatch` atomic changes。
- 所有 high-level command 最终都应可展开为 `BookPatch`，以便统一 validate/apply/rebase。

### 7. Validate patches semantically, not just structurally

`BookPatch` 可以表达任意状态转换，也可以表达非法状态转换。因此 validator 必须保留 Book IR 语义规则。

至少需要检查：

- patch schema 合法。
- `base_version` 与当前版本兼容。
- UID 存在且唯一。
- `old` / `old_node` 与当前 Book 匹配，充当 precondition。
- 修改范围匹配 lease / patch scope。
- field 是否允许被该 agent 修改。
- 新 node 带有合法 provenance。
- chapter/block order 合法。
- table HTML 合法。
- footnote invariants 合法。
- 应用 patch 后 Book 仍可通过 Pydantic validation 和 audit invariants。

这意味着 `BookPatch` 是低层表达能力，不是绕过规则的逃生口。

### 8. Use Book diff for integration merge validation

当多个 agent 在不同 worktree 工作后，integration 阶段需要验证合并结果的语义正确性：

```text
base Book IR (integration branch 上的版本)
merged Book IR (Git merge 后的版本)
        ↓
diff_books(base, merged) → BookPatch
        ↓
semantic validation
```

三方关系：

```text
base: agent 开始工作时的版本
current: integration branch 当前版本
merged: Git merge 后的版本
```

乐观并发：

- agent 改了 block A，current 没改 block A：Git 自动 merge，semantic validation 通过即可。
- agent 和 current 都改了 block A.text：Git conflict，交 reviewer/supervisor。
- Git merge 成功但 semantic validation 失败：reject，需要人工/agent 介入。

### 9. Add Book diff as the bridge from edited state to BookPatch

核心能力：

```text
old Book IR + new Book IR
        ↓
diff_books(base, proposed)
        ↓
BookPatch
```

第一版目标不应追求最小 diff，而应追求准确可重放：

```text
apply_book_patch(base, diff_books(base, proposed)) == proposed
```

建议流程：

```text
base Book
proposed Book
        ↓
Pydantic validate both
        ↓
canonicalize both
        ↓
index chapters/blocks by uid
        ↓
compare book fields, chapter fields, block fields, parent, and order
        ↓
emit BookPatch
        ↓
apply patch to base
        ↓
assert canonical result == canonical proposed
```

第一版 diff 规则：

- 同 UID 存在于两边：比较允许编辑的字段，生成 `set_field`。
- 同 UID 存在于两边但 parent/order 变化：生成 `move_node`。
- proposed 有、base 没有：生成 `insert_node`。
- base 有、proposed 没有：生成 `delete_node`。
- block/chapter kind 变化：优先生成 `replace_node`。
- UID 变化：不要猜测 rename；第一版按 `delete_node` + `insert_node` 处理。

每条 change 都必须携带旧值或旧节点：

```json
{
  "op": "set_field",
  "target_uid": "p001-b02-f91",
  "field": "text",
  "old": "teh book",
  "new": "the book"
}
```

`old` / `old_node` 充当 precondition。apply 时当前值不匹配就拒绝，交给 rebase/conflict 处理。

### 10. Use Git for projection version control, not semantic correctness

完成 Book diff 后，可以大量利用 Git，但 Git 仍不能完全替代 epubforge 的 patch/validator。

如果仍然把整本书存成一个巨大 `book.json`，Git merge 帮助有限。更好的形态是 Git-friendly projection：

```text
book.json
chapters/order.json
chapters/<chapter_uid>/meta.json
chapters/<chapter_uid>/blocks.order
blocks/<block_uid>.md
blocks/<block_uid>.json
```

这种形态下：

- 不同 agent 修改不同 block，Git 大概率自动 merge。
- 同一个 block 同一段文本冲突，Git 会标出冲突。
- branch/worktree 可以承载 agent 私有工作区。
- `git diff` / difftastic 可以作为 review display。

但 Git 只能处理文本/文件层面的版本控制。merge 之后仍必须回到 epubforge 语义层：

```text
merged projection
        ↓ parse
proposed Book
        ↓ diff_books(base, proposed)
BookPatch
        ↓ validate
apply
```

Git 可以替代或弱化的部分：

- 版本存储。
- 分支/worktree。
- 文本级 merge/rebase。
- 冲突标记。
- 历史查看。

Git 不能替代的部分：

- Book IR schema validation。
- UID/scope/lease 校验。
- footnote pairing 合法性。
- table HTML 和跨页表格合法性。
- provenance 完整性。
- doctor/audit invariants。
- patch audit log。

因此推荐边界是：

```text
Git: projection history and text merge
BookPatch: machine-applicable semantic change representation
validator: Book-specific correctness
```

如果选择 Git 作为 agentic workflow 的 VCS/隔离机制，则应直接取消长时间 chapter lease 模型：

```text
agent A -> branch/worktree agent/a
agent B -> branch/worktree agent/b
        ↓
each agent edits its own projection workspace
        ↓
Git merge/rebase into integration branch
        ↓
parse merged projection -> proposed Book
        ↓
diff_books(base, proposed) -> BookPatch
        ↓
semantic validation -> apply/commit
```

在这个模式下：

- agent 读写自己的 worktree/branch，不需要 chapter lease。
- 并发隔离由 Git branch/worktree 提供。
- 文本冲突由 Git merge/rebase 暴露。
- Book 语义冲突由 `BookPatch` validator 暴露。
- 不应同时维护长期 lease 和 Git branch/worktree 两套并发模型。

仍可能需要一个很短的 integration transaction，用于把已验证的 merged Book/Patch 提交到主状态；但这不是 agent 工作期间持有的 chapter lease。

### 11. Use difftastic/Git for review and display, not as the apply layer

difftastic/CST diff 适合作为 human review 展示层。它能更清楚地展示结构变化，尤其是 JSON/YAML/Markdown/projection 文件的变化。

但它不应作为核心 apply 层：

- difftastic 输出主要面向人类阅读，不是稳定 patch schema。
- CST diff 懂语法树，不懂 Book IR 语义。
- Git 能提供版本、分支、diff/merge，但仍需要 epubforge 自己校验脚注、章节、表格、provenance 等语义。

推荐分工：

```text
Git: storage, history, branch/diff support
difftastic: review/display
BookPatch: machine-applicable edit representation
semantic validator: Book-specific correctness
```

### 12. Make UIDs both collision-resistant and agent-readable

BookPatch 依赖 UID 定位，因此 UID 需要同时满足两个目标：

- 机器层面：稳定、唯一、跨 revision 不易碰撞。
- agent 层面：可读、可引用、可在自然语言上下文里辨认。

建议：

- canonical UID 继续作为不透明稳定 id，不依赖数组位置。
- runtime-created nodes 必须包含随机/nonce 成分，避免不同 revision 或不同 agent 创建节点时碰巧生成同名。
- 面向 agent 的 display handle 可以带 3-4 位短随机后缀，例如 `p012-heading-a3f`、`fn-014-b9c`、`tbl-008-7d2`。
- 不能只用语义 slug 作为 UID，例如 `introduction`、`chapter-1`，因为跨 revision 和同名标题容易冲突。

需要区分：

```text
uid: 系统级稳定标识，用于 patch/apply。
display_handle: agent-readable alias，用于 prompt、projection、review。
```

如果决定把二者合并，也应确保 handle 里包含短 nonce。

### 13. Provide agent-friendly serde/projections

虽然 agent 是机器，但 Codex/Claude Code 的工作方式接近自然语言审读。让它直接读完整嵌套 `book.json` 并不理想。

应提供可反序列化、可回写、可 diff 的 projection：

```text
edit_state/projections/chapters/<chapter_uid>.md
edit_state/projections/chapters/<chapter_uid>.jsonl
```

示例 Markdown-ish projection：

```text
# Chapter: Introduction [ch-001-a3f]

[[block p001-b01-c7e | kind=heading | page=1 | level=1]]
Introduction

[[block p001-b02-f91 | kind=paragraph | page=1 | role=body]]
This is the first paragraph...

[[block p002-fn1-8ab | kind=footnote | page=2 | callout=1 | paired=false]]
1. Footnote text...
```

Projection 是 Book IR 当前状态的**只读渲染**，不是可编辑的中间格式。Agent 不直接修改 projection 文件；所有编辑通过 CLI 命令（`agent-output add-command/add-patch`）提交。

用途：
- 作为 agent prompt 的阅读上下文，替代让 agent 在巨大 JSON 里定位 block。
- `projection export` 读取当前 `book.json`，因此 agent 在同一 worktree 内提交 patch 后，再次 export 会反映已应用的修改。

要求：

- 每个 block 明确显示 UID / display handle。
- provenance/page/role/kind 等关键元数据就近显示。
- 不需要 parse 回结构化 form（只读，不做 round-trip）。
- agent prompt 优先引用 projection，而不是要求 agent 在巨大 JSON 里定位 block。

典型 agent 工作循环：

```text
projection export (读当前 Book IR 状态)
    ↓ agent 阅读 projection，推理
CLI 命令提交 patch → book.json 更新
    ↓
projection export (读更新后状态，继续下一轮)
```

### 14. Concurrency model: Git workspaces (DECIDED)

**决策：选择 Git branch/worktree 作为唯一并发模型。** 移除整个 lease/lock 子系统。

- Agent 在自己的 branch/worktree 工作，不使用 chapter lease。
- 并发隔离由 Git 提供。
- 最终 integration 只做短事务 + semantic validation。
- 不保留 Direct-edit mode 作为备选。

### 16. Move VLM out of pipeline stage semantics

目标状态：

- ingestion Stage 3 只负责生成 Docling-derived evidence draft。
- 不再有 VLM mode / skip-VLM mode 的主流程分叉。
- VLM 成为 editor 工具。

VLM 工具输入应包含：

- 当前 `Book` IR 的指定范围。
- 对应 PDF page image。
- page/block/chapter context。
- 可选 doctor issues/hints。

VLM 工具输出应是 structured observation，不直接修改 `book.json`。

建议 schema：

```python
class VLMObservation(BaseModel):
    observation_id: str
    page: int
    chapter_uid: str | None
    related_block_uids: list[str]
    model: str
    image_sha256: str
    prompt_sha256: str
    findings: list[VLMFinding]
    raw_text: str | None = None
```

这些 observation 存入：

```text
edit_state/evidence/vlm_observations/
```

或继续放在：

```text
edit_state/audit/vlm_pages/
```

但需要可被 op/evidence 引用。

### 17. Make doctor output schedulable

doctor 现在输出 `issues`、`hints`、`readiness`、`delta`，但 supervisor 仍需要人工解释这些内容。

后续应考虑把 doctor 输出转为明确工作项：

```python
class DoctorTask(BaseModel):
    task_id: str
    kind: Literal["scan", "fix", "review"]
    chapter_uid: str | None
    block_uid: str | None
    source_issue_key: str | None
    source_hint_key: str | None
    priority: int
    recommended_agent: Literal["scanner", "fixer", "reviewer"]
```

这样 supervisor 可以直接调度：

- 有硬规则 issue：开 fixer。
- 有 `needs_scan`：开 scanner。
- 有 `open_question`：开 reviewer。
- 有 candidate role：优先 scanner/fixer。
- 有 VLM evidence need：scanner 调用 VLM 工具。

## Proposed Workflow

### Scanner/Fixer workflow (Git workspace mode)

Agent 在自己的 branch/worktree 里工作，通过 CLI 命令提交修改：

```bash
# supervisor 创建 worktree
git worktree add ../epubforge-scan-ch-1 -b agent/scanner-1/ch-1
cd ../epubforge-scan-ch-1

# agent 读取当前状态（只读 projection）
epubforge editor projection export work/book \
  --chapter ch-1

# agent 推理后通过 CLI 命令提交修改
epubforge editor agent-output begin work/book \
  --kind scanner \
  --chapter ch-1 \
  --agent scanner-1

epubforge editor agent-output add-command work/book <output-id> \
  --command-file scratch/command.json

epubforge editor agent-output add-note work/book <output-id> \
  --text "第 12 页脚注密度异常"

epubforge editor agent-output submit work/book <output-id> --apply

# agent 可以再次 export 查看修改后的状态，继续下一轮
epubforge editor projection export work/book \
  --chapter ch-1

# 完成后提交到 Git
git add edit_state/
git commit -m "scanner-1 scan chapter ch-1"
```

Supervisor/integration side：

```bash
# Git merge agent 的工作分支
git merge agent/scanner-1/ch-1

# 验证合并后的 Book IR 语义正确性
epubforge editor diff-books work/book \
  --base-ref main \
  --proposed-ref HEAD

# 如果有语义冲突，交 reviewer/supervisor 处理
```

这个模式下：

- agent 的读写隔离由 Git branch/worktree 提供。
- agent 通过 CLI 命令修改 Book IR，不直接编辑 projection 文件。
- projection export 是只读的，用于给 agent 提供可读上下文。
- Git merge 成功只说明文本层没有冲突。
- `diff-books` + semantic validation 确认 Book IR 语义可接受。

### VLM evidence

```bash
epubforge editor vlm-range work/book \
  --chapter ch-1 \
  --page 12 \
  --blocks block-a,block-b
```

Expected behavior:

- render relevant page image。
- include current IR context。
- call VLM with structured output。
- write `VLMObservation` evidence file。
- return `observation_id` for scanner/fixer to reference。

## Resolved Decisions

### D1. Concurrency model: Git workspace mode

选择 Git branch/worktree 作为唯一并发模型。理由：利用现成工具，不造轮子。

后果：
- 移除整个 lease/lock 子系统（`leases.py`, `acquire_lease`, `release_lease`, `acquire_book_lock`, `release_book_lock`）。
- 不需要 lease 续租（原 Phase 10 取消）。
- agent 隔离由 Git worktree 提供。
- 最终 integration 只需短事务 + semantic validation。

### D2. Big-bang 替换 EditOp，不保留兼容层

一次性重写，不做 EditOp → BookPatch 兼容/迁移层。不考虑旧 workdir 向后兼容。

前提：先把测试写好，确保 BookPatch 能完整表达所有现有 EditOp 语义后再切换。

### D3. diff_books 不做语义推断

`diff_books` 只做结构差异检测，不推断高层意图（如 footnote pairing、chapter split）。

处理策略：
- diff 输出的 changes 按 block 顺序做 1D 距离层次聚类（hclust），将空间邻近的变化分组输出。每个 change 的坐标是其 target block 在 chapter block list 中的 index；跨 chapter 的 changes 按 chapter 顺序再做一层分组。组内按 block index 排序，组间按最小 block index 排序。这样 agent/reviewer 看到的 diff 天然反映局部编辑意图，无需语义推断。
- 不做启发式语义推断——这会引入另一套需要维护的规则体系。
- validator 仍然校验最终状态的语义合法性（footnote invariants、table HTML 等），但不要求 patch 本身表达意图。

### D4. 不考虑历史迁移

已选择 Git workspace mode，完全不考虑旧 edit_log / staging.jsonl / op_log_version 的迁移。新系统从零开始。

### D5. 项目定位：个人自用

不需要考虑工期、release cadence、backward compatibility。可以大刀阔斧重写。

## Open Questions (Remaining)

- Should scanner be allowed to submit any low-level patch, or only low-risk intra-chapter PatchCommands?
- Should topology patches be restricted to supervisor outputs?
- Should memory changes be top-level `memory_patches` on `AgentOutput`, or encoded as BookPatch-adjacent records?
- Should canonical `uid` and agent-facing `display_handle` be separate fields, or should UID itself be human-readable with a nonce suffix?
- ~~What exact projection format should be canonical~~ (RESOLVED: projection 是只读渲染，格式选择不影响系统正确性，按需调整即可)
- Which high-level commands remain first-class macros after `BookPatch` exists?
- Should Git commits store projections only, or also materialized `book.json` snapshots?
- What is the first acceptable conflict model: reject on same-field conflicts, or attempt semantic merge for selected fields?
- Should VLM evidence live under `edit_state/audit/` or a new `edit_state/evidence/` directory?

## Review Notes

### ~~R1. Projection round-trip spec~~ (RESOLVED: projection 改为只读，不需要 parser 或 round-trip)

### R2. Orphaned agent-output 需要 GC 机制

`agent-output begin` 创建了 output 文件但 agent crash、永远不 submit。需要 TTL 或 GC 清理。Git workspace mode 下可以简化为：丢弃未 merge 的 worktree 即可。

### R3. Phase 顺序已调整

原 Phase 8（CLI 命令组）过晚。CLI 是 agent 使用新系统的唯一入口，应和 model 同步交付。已在下方重排。

## Implementation Phases

### Phase 1: BookPatch model and validator

- Add `editor/patches.py`.
- Define `BookPatch`, `PatchScope`, and 5 种 `IRChange` union (`set_field`, `replace_node`, `insert_node`, `delete_node`, `move_node`).
- Implement semantic validation: UID existence, old-value preconditions, Book IR invariants.
- Tests: field edits, block insert/delete/move, replace node, invalid cases.

### Phase 2: AgentOutput model + CLI command group

- Add `editor/agent_output.py`: define `AgentOutput` model.
- Add CLI commands: `agent-output begin`, `add-note`, `add-question`, `add-command`, `add-patch`, `add-memory-patch`, `validate`, `submit`.
- `validate` checks: schema, base_version, UID existence, scope.
- `submit --apply`: validate → compile commands → apply patches → archive output.
- Tests: malformed JSON, stale base_version, invalid uid, full submit round-trip.

### Phase 3: PatchCommand → BookPatch compilation

- Define `PatchCommand` macros: `split_block`, `merge_blocks`, `split_chapter`, `merge_chapters`, `relocate_block`, `pair_footnote`, `unpair_footnote`, `mark_orphan`, `split_merged_table`.
- Each macro compiles to `BookPatch` (list of `IRChange`).
- 普通字段修改收敛到 `set_field` / `replace_node`，不再需要独立 op type.
- Tests: each macro's compilation output + apply round-trip.

### Phase 4: Remove old EditOp/OpEnvelope system

前提：Phase 1-3 测试全部通过，BookPatch 能表达所有现有 EditOp 语义。

- Remove `ops.py` (EditOp, OpEnvelope).
- Remove `apply.py` (旧 apply_envelope).
- Remove `propose_op.py`.
- Remove `apply_queue.py` (staging.jsonl workflow).
- Remove `leases.py`, `acquire_book_lock.py`, `release_book_lock.py`, `acquire_lease.py`, `release_lease.py`.
- Update `tool_surface.py`: remove old commands, wire new ones.
- Update `app.py` CLI registration.

### Phase 5: Projection export (read-only)

- Add `projection export` CLI command: chapter-scoped Markdown-ish view with UID/display handles, kind, page, role, text.
- 只读输出，不需要 parser 或 round-trip。
- 读取当前 `book.json`，因此 agent 提交 patch 后再次 export 会反映修改。
- Tests: export 输出包含所有 block UID 和关键元数据。

### Phase 6: Book diff engine

- Implement `diff_books(base: Book, proposed: Book) -> BookPatch`.
- Implement `apply_book_patch(base: Book, patch: BookPatch) -> Book`.
- Round-trip invariant: `apply_book_patch(base, diff_books(base, proposed)) == proposed`.
- Changes 按 node 空间邻近性排序（见 D3）。
- 不做 rename inference；UID 变化按 delete + insert 处理。
- 主要用于 integration merge 验证（见 §8），不用于 agent 提交路径。
- Tests: field edits, insert/delete/move block, replace block kind, chapter order, duplicate UID rejection.

### Phase 7: Git-backed workspace workflow

- Support branch/worktree for agent private workspaces.
- Agent 通过 CLI 命令修改 Book IR，projection export 作为只读上下文。
- Integration 阶段：Git merge → `diff_books` → semantic validation。
- No lease system; only short integration transaction.
- Orphaned worktree cleanup（丢弃未 merge 的 worktree 即可）。

### Phase 9: VLM as editor evidence tool

- Introduce `VLMObservation` schema.
- Upgrade `vlm-page` / add `vlm-range` to accept IR scope.
- Store evidence with metadata; let AgentOutput reference evidence ids.

### Phase 10: Simplify Stage 3

- Make Docling-derived extraction the only ingestion mode.
- Remove VLM mode branch from pipeline.
- No backward compatibility for old workdirs.

### Phase 11: Doctor task generation

- Add task-oriented doctor output (`DoctorTask`).
- Map issues/hints to recommended agent work.
- Use as supervisor scheduling input.
