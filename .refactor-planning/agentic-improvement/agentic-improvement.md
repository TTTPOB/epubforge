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
- UID 存在且唯一。
- `old` / `old_node` 与当前 Book 匹配，充当 precondition（字段级冲突检测由此实现）。
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
projection export (首次读取，获取完整上下文)
    ↓ agent 阅读 projection，推理
CLI 命令提交 patch → book.json 更新
    ↓ submit 成功后展示提示：
    ↓   "patch applied. use rg/grep -A -B on uid <last-touched-uid>
    ↓    in the projection file to inspect context around the change
    ↓    and continue reviewing."
    ↓
agent 用 grep -A -B <uid> 定点查看变化周围上下文，继续审阅
```

不应每轮都重新 export 完整 projection——大量无变化内容会填满 agent context。提交 patch 后，`submit` 的输出应提示 agent 用 `rg`/`grep -A -B` 按 UID 定点查看刚修改区域的上下文，增量推进审阅。

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

### D6. 移除 op_log_version / base_version

Git worktree 提供版本控制，BookPatch 的 `old` 值 precondition 提供字段级冲突检测，不需要额外的版本号。

后果：
- `AgentOutput` 和 `BookPatch` 均不携带 `base_version` 字段。
- Book 模型不再维护 `op_log_version` 字段。
- validate 流程不做 `base_version == op_log_version` 检查；冲突检测依赖 apply 时的 `old` / `old_node` precondition。
- 版本隔离由 Git branch/worktree（D1）承担。

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
- `validate` checks: schema, UID existence, old-value preconditions, scope.
- `submit --apply`: validate → compile commands → apply patches → archive output.
- Tests: malformed JSON, precondition mismatch, invalid uid, full submit round-trip.

### Phase 3: PatchCommand → BookPatch compilation

- Define `PatchCommand` macros: `split_block`, `merge_blocks`, `split_chapter`, `merge_chapters`, `relocate_block`, `pair_footnote`, `unpair_footnote`, `mark_orphan`, `split_merged_table`.
- Each macro compiles to `BookPatch` (list of `IRChange`).
- 普通字段修改收敛到 `set_field` / `replace_node`，不再需要独立 op type.
- Tests: each macro's compilation output + apply round-trip.

### Phase 4: Remove old EditOp/OpEnvelope system

Status: **completed**. The old edit operation, apply queue, staging, and lease/lock implementation files have been removed; current editor mutation entry points are AgentOutput, BookPatch, and PatchCommand only.

前提：Phase 1-3 测试全部通过，BookPatch 能表达所有现有 EditOp 语义。

- Remove `ops.py` (EditOp, OpEnvelope).
- Remove `apply.py` (旧 apply_envelope).
- Remove `propose_op.py`.
- Remove `apply_queue.py` (staging.jsonl workflow).
- Remove `leases.py`, `acquire_book_lock.py`, `release_book_lock.py`, `acquire_lease.py`, `release_lease.py`.
- Update `tool_surface.py`: remove old commands, wire new ones.
- Update `app.py` CLI registration.

### Phase 5: Projection export (read-only)

#### 5.1 概述

Projection export 提供 Book IR 当前状态的**只读 Markdown-ish 渲染**。它是 agent prompt 上下文的阅读材料，不是可编辑的中间格式。Agent 不直接修改 projection 文件，所有编辑通过 CLI 命令（`agent-output add-command/add-patch`）提交。

设计原则：
- **只读**：不 parser、不 import、不 round-trip、不 apply。
- **严格读取当前 `edit_state/book.json`**：不回退到 `05_semantic*.json`。未初始化的 workdir 报错退出。
- **文件级别输出**：写入 `edit_state/projections/` 目录，不输出到 stdout（Phase 5 暂不实现 `--stdout`）。
- **增量友好**：agent 提交 patch 后，再次 export 反映已应用的修改。

#### 5.2 命令设计

```
epubforge editor projection export <work>                      # 全量导出
epubforge editor projection export <work> --chapter <uid>      # 单个 chapter 导出
```

**命令树**：Projection 是 editor 子命令组下的一个子命令组。

```
epubforge editor projection export <work>
                                ↑
                              子命令组
```

**stdout 输出**：Phase 5 中，export 命令在 stdout 仅输出 JSON 摘要：
```json
{"exported_at": "2026-04-25T12:00:00", "chapters": 3, "out_dir": "edit_state/projections", "files": ["index.md", "chapters/ch-001-a3f.md", "chapters/ch-002-b7c.md", "chapters/ch-003-d9e.md"]}
```

实际内容写入文件，不输出到 stdout（Phase 5 不实现 `--stdout` 标志）。

**错误处理**：
- workdir 未初始化（`edit_state/book.json` 不存在）→ 报错退出，exit code 1，提示运行 `editor init`。
- `--chapter <uid>` 指定的 chapter 不存在 → 报错退出，exit code 1，列出可用 chapter UID。
- `book.json` 有 schema 错误 → 报错退出，exit code 1，提示 schema 校验失败。

#### 5.3 输出路径

```
edit_state/projections/
  index.md                                    # 全书索引
  chapters/
    <chapter_uid>.md                          # 每个 chapter 一个文件
```

**index.md** 内容：

```markdown
[[book]] {"title":"The Book Title","authors":["Author Name"],"exported_at":"2026-04-25T12:00:00","source":"edit_state/book.json","chapters":3}

## Chapters

| # | UID | Title | Blocks | Pages |
|---|-----|-------|--------|-------|
| 1 | ch-001-a3f | Introduction     | 42 | 1-12 |
| 2 | ch-002-b7c | Chapter 1        | 87 | 13-45 |
| 3 | ch-003-d9e | Chapter 2        | 65 | 46-78 |
```

索引也列出每个 chapter 中的 block 数量和页码范围。

**chapters/\<chapter_uid\>.md** 内容格式：

```markdown
# Chapter: Introduction [ch-001-a3f]

[[chapter ch-001-a3f]] {"title":"Introduction","blocks":42,"page_range":[1,12]}

---

[[block p001-b01-c7e]] {"uid":"p001-b01-c7e","kind":"heading","page":1,"level":1}
Introduction

[[block p001-b02-f91]] {"uid":"p001-b02-f91","kind":"paragraph","page":1,"role":"body"}
This is the first paragraph of the introduction...

[[block p002-fn1-8ab]] {"uid":"p002-fn1-8ab","kind":"footnote","page":2,"callout":"1","paired":false}
1. Footnote text for the first callout.

[[block p003-fig-1b3]] {"uid":"p003-fig-1b3","kind":"figure","page":3,"provenance":{"source":"docling"}}
![Figure caption: Architecture diagram](image_ref_or_placeholder)
Caption: Architecture diagram

[[block p004-tbl-7d2]] {"uid":"p004-tbl-7d2","kind":"table","page":4,"multi_page":false,"num_rows":2,"num_cols":2}
<table>
  <thead><tr><th>Name</th><th>Value</th></tr></thead>
  <tbody><tr><td>foo</td><td>bar</td></tr></tbody>
</table>
**Table title:** Sample Table
**Caption:** A sample table with data

[[block p005-eq-9f4]] {"uid":"p005-eq-9f4","kind":"equation","page":5}
E = mc²
```

**格式说明**：

Projection 使用 JSON metadata marker 行标记每个结构化元素。每行语法为：

```
[[<element-type> <identifier>]] {<json-object>}
```

- `<element-type>` 是元素种类：`book`、`chapter`、`block`。
- `<identifier>` 是元素的 UID（block UID 或 chapter UID）。
- `{<json-object>}` 是元素的元数据 JSON，包含当前状态的关键字段快照。

**设计理由**：选择内联 JSON 而非 key=value 格式，是为了避免转义歧义。Block content 中可能天然包含 `|`、`]`、换行符等字符（尤其是表格 HTML、LaTeX 公式、脚注文本），key=value 格式在这些场景下需要复杂转义规则才能保证唯一分割。JSON 有标准化的转义（`\"`、`\n`、`\\` 等），任何 JSON parser 都能正确解析，同时 `[[ ]]` 包装在文本 grep 时也保持可识别。

输出结构：

- 每个 chapter 文件以 Markdown 标题 `# Chapter: <title> [<uid>]` 开头。
- 紧接着一行 `[[chapter <uid>]] {json}` 提供章节的元数据快照。
- 分隔线 `---` 后依次排列该章节的所有 block。
- 每个 block 输出至少两行：
  - **metadata marker 行**：`[[block <uid>]] {json}`，包含 UID 和该 block 的关键字段 JSON。
  - **content**：block 的主体文本内容（可能跨多行）。Table block 的 content 是多行的原始 HTML。
- marker 行**不是**可反序列化回完整 Book IR 的格式（只读，不做 round-trip）。JSON 中的字段是当前状态的快照摘录，用于 agent 阅读和 grep 定位，不是 book.json 的精确子集。

#### 5.4 各 Block 类型字段覆盖

Marker 行 JSON 中的字段按 block 类型不同。以下列出每种类型的必含字段和条件字段：

| Block 类型 | JSON 中的字段 | content 行 |
|-----------|--------------|-----------|
| **Paragraph** | `uid`, `kind`, `page`, `role`, `cross_page` (if true), `provenance` | `text` raw |
| **Heading** | `uid`, `kind`, `page`, `level`, `heading_id` (if present), `provenance` | `text` raw |
| **Footnote** | `uid`, `kind`, `page`, `callout`, `paired`, `orphan` (if true) | `text` raw |
| **Figure** | `uid`, `kind`, `page`, `provenance` | `![caption](image_ref)` 格式；如果 image_ref 不存在则只输 caption text |
| **Table** | `uid`, `kind`, `page`, `multi_page`, `num_rows`, `num_cols`, `num_segments` (if multi_page), `segment_pages` (if multi_page), `provenance` | raw HTML（`<table>...</table>`）；下方附加 `**Table title:**` 和 `**Caption:**` |
| **Equation** | `uid`, `kind`, `page`, `provenance` | `text` raw（LaTeX 公式文本） |

**输出规则**：
- 所有 block 类型**都输出**，无遗漏。
- 全部字段从当前 `book.json`（即 Book IR）读取，不调用 PDF source 或 VLM。
- 不需要 round-trip 能力——agent 如需修改，应使用 `agent-output add-patch/add-command`，而非直接编辑 projection 文件。

#### 5.5 Table 设计特殊处理

Table block 在 projection 中的处理需要特别注意，因为 table HTML 可能很长且有复杂结构。

**规则**：
- **原始 HTML 完整输出**：`<table>...</table>` 直接写入 projection 文件，不做截断、缩略或摘要。
- **保留 colspan/rowspan/merged cell**：HTML 包含的 `colspan`、`rowspan`、`th`/`td` 标签结构原样保留。
- **`merge_record` 字段**：当 `multi_page == true` 时，在 HTML 下方输出 `**Merge record:**` 摘要，包含 `segment_order`、`segment_pages` 和被合并的 `num_segments`。**不**输出 `segment_html` 字段（避免重复 HTML）。
  ```markdown
  [[block p010-tbl-3a1]] {"uid":"p010-tbl-3a1","kind":"table","page":10,"multi_page":true,"num_segments":3,"segment_pages":[10,11,12]}
  <table>
    ...full merged HTML...
  </table>
  **Table title:** Comparative Data
  **Caption:** Table comparing metrics across three groups
  **Merge record:** segments: 3, pages: [10, 11, 12], order: [0, 1, 2]
  ```
- 对于 `multi_page == false` 的 table，不输出 merge_record。

#### 5.6 CLI 详细设计

**命令注册**：在 `editor/tool_surface.py`（或对应的 CLI 注册文件）中新增 `projection` 子命令组：

```python
@app.group()
def projection():
    """Read-only projection export commands."""

@projection.command()
@click.argument("work")
@click.option("--chapter", default=None, help="Chapter UID to export")
def export(work, chapter):
    """Export book IR to Markdown-ish projection files."""
    ...
```

**导出流程**：

1. 解析工作目录路径，验证 `edit_state/book.json` 存在且合法。
2. 加载 `Book` IR（Pydantic parse）。失败则报错退出。
3. 如果指定了 `--chapter`，验证 UID 存在。不存在则报错退出。
4. 确定输出文件列表：
   - 无 `--chapter`：所有 chapter → index.md + 每个 chapter 一个文件。
   - 有 `--chapter`：单个 chapter → index.md + 该 chapter 文件（覆盖 index.md，只列出该 chapter）。
5. 确保 `edit_state/projections/chapters/` 目录存在（`mkdir -p`）。
6. 对每个 chapter，生成 Markdown-ish 内容并写入对应文件。
7. 写入 `index.md`。
8. stdout 输出 JSON 摘要。

**不实现的功能（Phase 5 明确排除）**：
- `--stdout` 标志（将整体或单个 chapter 内容输出到 stdout）。
- `--format json` 或 `--format md` 选项。
- 增量/差异导出。
- projection diff（跨版本比较 projection 文件）。

#### 5.7 与其他 Phases 的关系

- **Phase 3 (PatchCommand macros)**：Projection 显示的是 patch 应用后的结果，不涉及 command 编译逻辑。
- **Phase 6 (Book diff engine)**：Projection 是纯输出，不参与 diff 或 patch apply。
- **Phase 7 (Git workspace workflow)**：Projection 存放在 `edit_state/projections/`，属于 worktree 内的文件，默认由 Git 跟踪。agent 提交 patch 后重新 export 会覆盖旧文件。Git 可以追踪 projection 文件本身的变更历史。
- **Phase 8 (VLM evidence)**：Projection 可以引用 VLM observation 的 observation_id（defer 至 Phase 8 讨论）。

#### 5.8 测试计划

**Renderer 单元测试**（`tests/editor/test_projection.py`）：
| 测试用例 | 描述 |
|---------|------|
| `test_full_book_export` | 导入一个最小 Book IR（含所有 6 种 block 类型各一个），验证 export 输出文件、index.md 格式、每个 chapter 文件 header 存在、每个 block 的 metadata marker 行和 content 行存在 |
| `test_single_chapter_export` | 使用 `--chapter <uid>` 导出，验证只生成该 chapter 文件和 index.md |
| `test_table_html_preserved` | Book 包含一个带复杂 `<table>` 的 block（含 colspan/rowspan）。验证 export 输出的 HTML 与原始 HTML 完全一致（字符串对比），未被截断或修改 |
| `test_table_merge_record` | Book 包含一个 `multi_page=True` 的 table，带 `merge_record`。验证 export 输出包含 merge_record 摘要行但不包含 `segment_html` |
| `test_paragraph_cross_page` | Paragraph 有 `cross_page=True`。验证 metadata 行包含 `cross_page` |
| `test_footnote_orphan` | Footnote 有 `orphan=True`。验证 metadata 行包含 `orphan=true` |
| `test_heading_with_id` | Heading 有 `heading_id`。验证 metadata 行包含 `heading_id` |
| `test_index_format` | 验证 index.md 包含标题、作者、导出时间、chapter 列表和 block 计数 |
| `test_provenance_source` | 验证 block 的 `provenance.source` 被包含在 metadata 行中 |
| `test_empty_chapter` | 验证空 chapter（无 blocks）正确输出无 block 的 chapter 文件 |

**CLI 集成测试**（`tests/editor/test_projection_cli.py`）：
| 测试用例 | 描述 |
|---------|------|
| `test_cli_export_from_book_json` | 初始化完整的 `edit_state/`（包含 `book.json`），运行 `epubforge editor projection export`，验证文件被写入且内容正确 |
| `test_cli_export_chapter` | 同上，但使用 `--chapter <uid>`，验证只导出指定 chapter |
| `test_cli_invalid_chapter` | 使用不存在的 chapter UID，验证 CLI 报错、exit code 1 |
| `test_cli_uninitialized_workdir` | 在无 `edit_state/book.json` 的目录上运行，验证 CLI 报错、exit code 1 |
| `test_cli_repeat_overwrite` | 连续运行两次 export，验证第二次成功覆盖且 content 一致 |
| `test_cli_stdout_summary` | 验证 stdout 输出 JSON 摘要，包含 `exported_at`、`chapters`、`files` |

**测试数据**：
- 测试 fixture 提供完整的最小 Book IR（含全部 block 类型），供 renderer 测试使用。
- CLI 集成测试使用 `pytest` + `CliRunner`（click.testing）或 `subprocess` 运行 CLI，取决于项目现有测试模式。

#### 5.9 Deferred / Uncertain Items

以下事项在 Phase 5 **不实现、不决策**，列为未来考察项：

| 事项 | 原因 |
|------|------|
| **display_handle** | Phase 5 直接使用 UID。display_handle（UID + 短随机后缀）的设计决策放至未来 UID 重设计时处理 |
| **snapshot / projection 关系** | snapshot（`book.json` 的纯 JSON 备份）和 projection（可读渲染）是否需要统一的版本管理，目前不明确，留待 Phase 7（Git workflow）积累实际使用经验后决定 |
| **`--stdout` 输出模式** | Phase 5 只写文件。`--stdout` 需求（piping、agent 直接读取、vlm-page caller 输出）推迟到有实际用例时再实现 |
| **Table 摘要/截断策略** | Phase 5 完整输出 table HTML。如果实测中大 table 导致 prompt 超限，再考虑截断/摘要策略，并增加配置项 `projection.max_table_rows` |
| **Projection diff** | 比较两个 projection 版本的差异，属于 Phase 7（Git workflow）和 Phase 6（Book diff）的增量功能，Phase 5 不做 |

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

### Phase 8: VLM as editor evidence tool

- Introduce `VLMObservation` schema.
- Upgrade `vlm-page` / add `vlm-range` to accept IR scope.
- Store evidence with metadata; let AgentOutput reference evidence ids.

### Phase 9: Simplify Stage 3

- Make Docling-derived extraction the only ingestion mode.
- Remove VLM mode branch from pipeline.
- No backward compatibility for old workdirs.

### Phase 10: Doctor task generation

- Add task-oriented doctor output (`DoctorTask`).
- Map issues/hints to recommended agent work.
- Use as supervisor scheduling input.
