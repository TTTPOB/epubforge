# Phase 6 实施计划：Book Diff Engine

> 状态：修订后的实施计划（pro review）  
> 对应主设计：`agentic-improvement.md` §8（Integration merge validation）、§9（Book diff bridge）、D3（`diff_books` 不做语义推断）、D6（不使用 `base_version` / `op_log_version`）  
> 前置条件：Phase 1–3 的 `BookPatch` / `PatchCommand` / `AgentOutput` workflow 已可作为 editor mutation 层；Phase 5 projection 是只读上下文  
> 下游依赖：Phase 7 Git-backed workspace workflow 依赖本 phase 把 Git merge 后的 `Book` 快照转为 UID-addressed semantic `BookPatch`

---

## 0. Plan-review loop policy / no human blocking

本计划必须支持无人值守的后续实现流程。实现 worker / reviewer **不得阻塞等待人类在线答疑**，也不得依赖任何 ask-human 工具或等价机制。

规则：

1. **不 ask human**：遇到设计不确定性时，不发起 ask-human；把问题写入本文件的 [Open questions register](#13-open-questions-register)。
2. **默认假设可执行**：每个开放问题必须同时记录：影响、默认假设、推荐决策、触发复核的条件。实现者按默认假设继续推进，除非代码事实或测试证明该默认不可行。
3. **多轮 plan-review 后仍未解决的问题必须保留**：如果经过多轮 plan-review loop 仍存在开放问题，不删除、不隐藏；将其标记为 `unresolved-after-review`，并保留默认实现路径与复核条件。
4. **计划文件是异步决策载体**：所有需要用户之后查看的设计点、风险、折中、默认选择和后续复核点都写在本计划中。
5. **实现期间的新发现回写计划**：如果实现时发现本计划与实际 `Book` / `BookPatch` / editor workflow 不一致，应先修订本计划的相应条目，再继续实现；不要在聊天中等待人类裁决。

---

## 1. 目标与非目标

### 1.1 目标

Phase 6 实现一个确定性的、UID-addressed Book diff engine：

1. 比较两个 Semantic IR `Book` 实例：`base` 与 `proposed`。
2. 生成可机器应用的 `BookPatch`，其 `changes` 由现有 5 种 `IRChange` 组成：
   - `set_field`
   - `replace_node`
   - `insert_node`
   - `delete_node`
   - `move_node`
3. 在可表达的范围内满足 round-trip：
   ```python
   apply_book_patch(base, diff_books(base, proposed)) == proposed
   ```
4. 生成的 patch 能通过现有 `validate_book_patch()` 与 `apply_book_patch()` 的 precondition / invariant 检查。
5. 输出顺序在保证可应用性的前提下尽量按 Book/Chapter/Block 空间邻近性组织，便于 reviewer 观察局部变化。
6. 为 Phase 7 Git-backed workspace workflow 提供 integration merge validation 的语义桥接：
   ```text
   Git merge result -> proposed Book -> diff_books(base, proposed) -> BookPatch -> semantic validation
   ```

### 1.2 非目标

Phase 6 不做：

- **不做语义推断**：不推断 footnote pairing、chapter split/merge、caption attribution、table continuation 等高层意图（遵循 D3）。
- **不做 rename inference**：同一内容但 UID 不同，按 `delete_node` + `insert_node` 处理。
- **不追求最小 diff**：第一版优先准确、可重放、可验证；不优化 change 数量。
- **不替代 AgentOutput workflow**：agent 日常编辑仍通过 `AgentOutput` → `PatchCommand` / `BookPatch` → `agent-output submit`；`diff_books` 主要服务 integration validation。
- **不实现 projection round-trip**：Phase 5 projection 是只读渲染，不作为 diff 输入格式。
- **不把 Git 当语义 apply 层**：Git 负责 worktree/branch/text merge；Book diff + BookPatch validator 负责 Semantic IR correctness。

---

## 2. 当前代码事实与约束

本计划按当前代码库事实修订，避免实现阶段踩到隐藏冲突。

### 2.1 Semantic IR

Semantic IR 位于 `src/epubforge/ir/semantic.py`：

- `Book`：`initialized_at`, `uid_seed`, `title`, `authors`, `language`, `source_pdf`, `chapters`, `extraction`
- `Chapter`：`kind="chapter"`, `uid`, `title`, `level`, `id`, `blocks`
- `Block` union：`Paragraph`, `Heading`, `Footnote`, `Figure`, `Table`, `Equation`
- `Provenance`：包含 `page`, `bbox`, `source`, `raw_ref`, `raw_label`, `artifact_id`, `evidence_ref`

### 2.2 BookPatch / IRChange

`src/epubforge/editor/patches.py` 已提供：

- `BookPatch(patch_id, agent_id, scope, changes, rationale, evidence_refs)`
- `PatchScope(chapter_uid: str | None)`；`chapter_uid=None` 表示 book-wide patch
- `SetFieldChange`：只能作用于 block 或 chapter UID，不支持 Book 顶层字段
- `ReplaceNodeChange`：只允许替换 block，不允许 chapter
- `InsertNodeChange`：`parent_uid=None` 插入 chapter；`parent_uid=<chapter_uid>` 插入 block
- `DeleteNodeChange`：可删除 block 或 empty chapter；删除非空 chapter 前必须先把 blocks 移走或删除，使 chapter 变为空
- `MoveNodeChange`：`from_parent_uid=None and to_parent_uid=None` 表示 chapter reorder；否则表示 block move
- `BookPatch.changes` 当前 `min_length=1`，因此“空 patch”目前不能用 `BookPatch` 表示
- 当前 patch payload 的 table node schema 未明确接收 `Table.merge_record`；Phase 6 若要声称 table round-trip，必须先对齐该 payload（见 §6.1、§7.4、OQ-04），不能一边忽略 `merge_record` 一边声称完整 round-trip。

### 2.3 可编辑字段集合

`_ALLOWED_SET_FIELD` 当前定义：

| kind | 可用 `set_field` 修改的字段 |
|---|---|
| `paragraph` | `text`, `role`, `style_class`, `cross_page`, `display_lines` |
| `heading` | `text`, `level`, `id`, `style_class` |
| `footnote` | `callout`, `text`, `paired`, `orphan`, `ref_bbox` |
| `figure` | `caption`, `image_ref`, `bbox` |
| `table` | `html`, `table_title`, `caption`, `continuation`, `multi_page`, `bbox` |
| `equation` | `latex`, `image_ref`, `bbox` |
| `chapter` | `title`, `level`, `id` |

`uid`, `kind`, `provenance` 是 immutable fields，不通过 `set_field` 修改。

### 2.4 对计划的直接影响

1. Book 顶层字段 diff 与 empty diff 都需要明确处理策略；不能默默假设现有 `BookPatch` 已支持。
2. 若要严格支持全 `Book` round-trip，需要有限扩展 `patches.py`；若不扩展，则必须把 unsupported deltas 显式报告为 non-round-trippable。
3. patch change 顺序不是纯展示问题：`apply_book_patch()` 按顺序执行，每一步都会重建索引并检查 precondition。因此排序必须首先可应用，其次才是 review-friendly。

---

## 3. 架构位置

### 3.1 Phase 7 integration flow 中的位置

```text
agent worktree/branch
        ↓
Git merge / rebase
        ↓
merged edit_state/book.json  ── parse as proposed Book
        ↓
diff_books(base Book, proposed Book)
        ↓
BookPatch (UID-addressed semantic delta)
        ↓
validate_book_patch(base, patch)
        ↓
apply_book_patch(base, patch)
        ↓
semantic/audit validation
        ↓
accept integration result or reject for reviewer/supervisor repair
```

### 3.2 与 AgentOutput/editor workflow 的关系

- `AgentOutput` 是 agent 正常提交路径。
- `PatchCommand` 是高层 macro，编译为 `BookPatch`。
- `BookPatch` 是 UID-addressed atomic mutation layer。
- `diff_books` **不是**普通 agent 编辑入口；它把两个 `Book` 快照之间的差异桥接成 `BookPatch`，用于 integration validation、debug 和 CI。

---

## 4. API 设计

### 4.1 推荐公共 API

```python
class DiffError(RuntimeError):
    """Raised when a Book diff cannot be generated safely."""


class EmptyBookDiff(RuntimeError):
    """Internal sentinel if keeping BookPatch.changes min_length=1."""


def diff_books(base: Book, proposed: Book) -> BookPatch:
    """Return a UID-addressed BookPatch from base to proposed.

    Raises:
        DiffError: invalid input, duplicate UID, unsupported Book-level delta,
        or no representable changes when empty BookPatch is not supported.
    """
```

### 4.2 Empty diff 的默认策略

当前 `BookPatch.changes` 要求至少 1 条 change，但主设计要求 `base == proposed` 时可产生空 diff。默认推荐决策：

- **修改 `BookPatch.changes` 允许空列表**，因为“无语义变化”是合法 patch 状态。
- `validate_book_patch()` 和 `apply_book_patch()` 对空 `changes` 应返回成功，`apply_book_patch(base, empty_patch)` 返回与 `base` 等价的深拷贝或原语义等价结果。
- `diff_books(base, base)` 返回 `changes=[]` 的 `BookPatch`。

如果实现者不愿在 Phase 6 修改 Phase 1 schema，则备用策略：新增 `diff_book_snapshots()` 返回：

```python
class BookDiffResult(BaseModel):
    is_empty: bool
    patch: BookPatch | None
    unsupported_diffs: list[UnsupportedDiff] = []
```

但备用策略会偏离 `agentic-improvement.md` 中 `diff_books(...) -> BookPatch` 的设计，因此默认不推荐。

### 4.3 Book 顶层字段的默认策略

Book 顶层字段没有 UID，现有 `set_field` 无法定位。默认推荐决策：

- Phase 6 MVP **不修改 Book 顶层字段**，只比较并报告差异。
- 如果 `initialized_at`, `uid_seed`, `title`, `authors`, `language`, `source_pdf`, `extraction` 等 Book-level fields 不同，`diff_books` 抛出 `DiffError`，错误信息列出字段名与建议：先通过专门 metadata patch 设计扩展，再要求 full-Book round-trip。
- Round-trip acceptance 在 Phase 6 中限定为：`chapters` / `Chapter` fields / `Block` fields / block topology / chapter topology。

原因：

- 贸然引入 `_BOOK_ROOT_UID` 会扩展 `apply_book_patch()` 的定位语义，并与“UID 来自 Book/Chapter/Block”的模型边界混淆。
- Phase 7 的主要 integration conflict 来自 chapter/block editing；Book metadata 通常不是 agent 并发编辑的热点。

如果后续实际需要 Book metadata diff，再新增显式 `BookMetadataPatch` 或扩展 `SetFieldChange` 的 target model，而不是隐藏伪 UID。

### 4.4 迁移 / 兼容性说明

Phase 6 计划对现有 schema 做两个小范围兼容性调整：

1. `BookPatch.changes=[]` 从非法变为合法 no-op patch。
2. Table node patch payload 支持序列化/反序列化 `merge_record`（见 §6.1、§7.4）。

影响范围：

- 现有非空 `BookPatch` JSON 仍保持兼容，不需要迁移。
- 任何测试若断言 `BookPatch(changes=[])` 会触发 schema `ValidationError`，应改为断言 empty patch 可通过 `validate_book_patch()`，且 `apply_book_patch(base, empty_patch)` 返回语义等价 Book。
- `AgentOutput.patches` 路径必须允许包含 empty patch：`agent-output validate`、`submit --stage`、`submit --apply` 不应把 empty patch 当 schema 错误；`submit --apply` 对 empty patch 只记录/归档审计事件，不修改 `book.json`。
- `PatchCommand` 编译路径如果偶然产生 empty patch（例如宏在预检后发现目标已经满足），也应作为 no-op 成功或在 command 层跳过；不能再依赖 `min_length=1` 拦截。
- Table payload 增加 `merge_record` 是向后兼容字段：旧 patch 没有该字段仍合法；新 patch 带该字段时，validator/apply 必须保留它以支持 round-trip。

本项目主计划 D4 已说明不考虑旧 edit log / staging 历史迁移；这里的“兼容性”仅指当前测试、当前 `AgentOutput` 代码路径和新旧 patch JSON 的 schema 行为。

---

## 5. 文件布局计划

### 5.1 新增文件

| 文件 | 用途 |
|---|---|
| `src/epubforge/editor/diff.py` | `diff_books()`, `DiffError`, UID indexing, field comparison, topology planning, ordering helpers |
| `tests/editor/test_diff.py` | diff engine 单元测试与 round-trip 测试 |
| `tests/editor/test_diff_cli.py` | 如果实现 CLI，则放置 CLI 集成测试 |

### 5.2 修改文件

| 文件 | 修改内容 |
|---|---|
| `src/epubforge/editor/patches.py` | 推荐：允许 `BookPatch.changes=[]`；确保 empty patch validate/apply 成功；Table payload 支持 `merge_record`；导出 diff 可复用的字段序列化/allowed-field helper |
| `src/epubforge/editor/__init__.py` | 导出 `diff_books`, `DiffError` |
| `src/epubforge/editor/tool_surface.py` | 可选：新增 `run_diff_books()` 业务函数 |
| `src/epubforge/editor/app.py` | 可选：注册 `epubforge editor diff-books` CLI |

---

## 6. Diff 语义范围

### 6.1 支持范围（MVP 必做）

1. Chapter existence：新增 empty chapter container、删除空 chapter、删除非空 chapter（先移动/删除 blocks 再删 empty chapter）。
2. Chapter reorder：同 UID chapter 在 `book.chapters` 中顺序变化 → `move_node`。
3. Chapter metadata fields：`title`, `level`, `id` → `set_field`。
4. Block existence：新增/删除 block → `insert_node` / `delete_node`。
5. Block move：同 UID block 在同 chapter 或跨 chapter 位置变化 → `move_node`。
6. Block kind change：同 UID 但 kind 不同 → `replace_node`。
7. Block editable fields：按 `_ALLOWED_SET_FIELD` 生成 `set_field`。
8. Table `merge_record`：Phase 6 默认**扩展 patch table payload 支持 `merge_record`**。`merge_record` 不加入 `_ALLOWED_SET_FIELD`；same UID table 的 `merge_record` delta 使用 `replace_node` 表达，insert/delete/replace 的 node snapshot 必须可携带完整 `merge_record`，以维持 table round-trip。

### 6.2 显式不支持 / 检测即报错

| 差异 | 默认行为 | 理由 |
|---|---|---|
| Book 顶层字段变化（`initialized_at`, `uid_seed`, `title`, `authors`, `language`, `source_pdf`, `extraction` 等） | 抛 `DiffError`，列为 unsupported Book-level delta | 现有 `BookPatch` 无 Book target |
| `provenance` 变化且节点未 replace/insert | 抛 `DiffError` 或列为 unsupported immutable delta | `provenance` immutable，不能用 `set_field` 表达 |
| 同 UID chapter kind 变化 | 抛 `DiffError` | `Chapter.kind` 应恒为 `chapter` |
| `uid=None` | 抛 `DiffError` | UID-addressed patch 需要稳定 UID |
| 重复 UID（chapter/block 跨全书重复） | 抛 `DiffError` | patch target 不唯一 |

---

## 7. 算法设计

### 7.1 总体流程

```text
diff_books(base, proposed):
  1. validate inputs are Book instances and all UIDs are non-null / unique
  2. reject unsupported Book-level / immutable deltas
  3. build base/proposed indexes for chapters and blocks
  4. generate topology changes with an apply-safe simulated transform
     (create target chapter containers first, fill/move blocks, then delete emptied source chapters)
  5. generate replace_node for same UID block kind changes and table merge_record changes
  6. generate set_field for same UID, same kind chapter/block editable field changes,
     excluding nodes already replaced and applying footnote paired/orphan safety rules
  7. combine changes in apply-safe order
  8. build BookPatch(scope=PatchScope(chapter_uid=None), agent_id="diff-engine")
  9. verify in tests: apply_book_patch(base, patch) == proposed on supported scope
```

### 7.2 UID index

需要构建一个包含 chapter/block parent 与 order 的索引：

```python
@dataclass(frozen=True)
class NodeLoc:
    uid: str
    kind: str
    chapter_uid: str | None
    chapter_index: int
    block_index: int | None


@dataclass(frozen=True)
class BookDiffIndex:
    chapter_by_uid: dict[str, Chapter]
    block_by_uid: dict[str, Block]
    loc_by_uid: dict[str, NodeLoc]
```

索引规则：

- chapter UID 与 block UID 共用全书命名空间，必须全局唯一。
- `uid=None` 是硬错误，不尝试自动初始化 UID。
- index 保留原始顺序，用于 move detection 与 review grouping。

### 7.3 字段比较规则

字段比较必须使用与 `patches.py` apply/precondition 相同的序列化语义，避免 Pydantic model / list / dict 的 mode 差异。

规则：

- 字段名默认按字典序遍历，保证确定性；但 footnote `paired` / `orphan` 是耦合字段，必须遵循下方安全规则，不能盲目字典序 apply。
- 只比较 `_ALLOWED_SET_FIELD[kind]` 内的字段。
- `old` 使用 base 当前字段序列化值。
- `new` 使用 proposed 字段序列化值。
- 若 `old == new`，不生成 change。
- 对 kind changed 的 block，或因 `merge_record` / footnote 耦合字段安全规则而选择 `replace_node` 的 block，不再生成字段级 diff。

#### 7.3.1 Patch helper 复用策略

`diff.py` 不应长期直接 import `_ALLOWED_SET_FIELD` / `_serialize_field_value` 这类私有实现细节，也不应复制一份 allowed-field mapping，避免 future drift。推荐在 `patches.py` 中新增小型公共 helper：

```python
def allowed_set_fields(kind: str) -> frozenset[str]: ...


def serialize_patch_field_value(value: object) -> object: ...
```

并让现有私有实现作为这些公共 helper 的内部实现或兼容 alias。`diff.py` 只依赖公共 helper。若实现阶段为了降低改动临时复用私有 helper，必须在同一 PR/变更中补 TODO 或后续子任务；Phase 6 验收前推荐完成公共 helper 导出。

#### 7.3.2 Footnote `paired` / `orphan` 安全规则

Footnote 的 `paired` 与 `orphan` 可能受 invariant 约束（例如同一 footnote 不应同时 paired 与 orphan）。如果 base/proposed 中这两个字段同时变化，按字段名字典序生成两个 `set_field` 可能产生非法中间状态，导致 `apply_book_patch()` 在第一条 change 后失败。

默认策略：

1. 如果 same UID footnote 的 `paired` 与 `orphan` **同时变化**，生成一条 `replace_node` 替换整个 footnote block；该 node 的其他字段级 diff 被 replace 覆盖，不再额外生成 `set_field`。
2. 如果只变化其中一个字段，可生成普通 `set_field`，但测试必须覆盖 apply 后 invariant 合法。
3. 如果未来 validator 支持 batch-level invariant 或明确允许安全中间状态，可以改为特定排序；在当前 sequential apply 语义下，`replace_node` 是默认安全路径。

### 7.4 Replace-node 触发条件（kind change / table merge_record / footnote flags）

同 UID block 在以下场景生成 `replace_node`：

1. block `kind` 变化。
2. same kind `table` 的 `merge_record` 变化（包括 `None` ↔ 非 `None`、segment 内容变化）。
3. same kind `footnote` 的 `paired` 与 `orphan` 同时变化（见 §7.3.2）。

示例：

```python
ReplaceNodeChange(
    op="replace_node",
    target_uid=uid,
    old_node=base_block.model_dump(mode="python"),
    new_node=proposed_block_without_uid,
)
```

`new_node` 必须包含 `kind`，且必须移除 `uid`，因为 `replace_node` apply 时会从 `target_uid` 注入 UID。

Table `merge_record` 的默认决策是**扩展 patch payload 支持**，因此：

- `old_node` / `new_node` 的 table payload 必须能包含 `merge_record`。
- `Table.merge_record` 不通过 `set_field` 修改，避免扩大 `_ALLOWED_SET_FIELD` 的语义。
- 如果实现阶段发现 current `replace_node` payload 无法承载 `merge_record`，应先完成 payload alignment；不得退回到静默忽略 `merge_record` delta。

### 7.5 Topology planner：目标容器优先，最后删除源容器

Topology planner 必须解决 split/merge chapter-like diff，而不能把 “new chapter full-node insert” 与 “先删除缺失 chapter” 混用。默认策略是：**先创建 proposed 需要的 chapter 容器（empty chapter），再按 proposed order 插入/移动 blocks，最后删除已清空的缺失 chapter**。

核心不变量：

- 新 chapter 的 `insert_node(parent_uid=None)` 只插入 empty chapter container：`blocks=[]`，但保留 proposed chapter 的 `uid/title/level/id/kind`。
- 任何 block（无论目标 parent 是 existing chapter 还是 new chapter）都只通过 block-level `insert_node` 或 `move_node` 到达目标位置。
- base 中 proposed 不再包含的 chapter 不在开头删除；它们作为临时源容器保留，直到其中需要保留的 blocks 已被移动、需要删除的 blocks 已被删除。
- chapter delete 的 `old_node` 必须是 blocks 已清空后的 chapter snapshot。

#### 7.5.1 Simulated order model

使用 lightweight simulation 跟踪 apply-time 当前拓扑：

```python
@dataclass
class SimChapter:
    uid: str
    blocks: list[str]


class SimBookOrder:
    chapters: list[str]
    blocks_by_chapter: dict[str, list[str]]
    parent_by_block: dict[str, str]
```

每生成一条 topology change，立即更新 simulation。后续 `after_uid` 必须引用 simulation 中已经位于目标容器内的 UID，或为 `None` 表示插入到 head。

#### 7.5.2 Apply-safe topology phases

Topology changes 按以下 apply-safe phases 生成：

1. **Insert target chapter containers**：对 proposed 中 base 不存在的 chapter，按 proposed chapter order 生成 empty chapter `insert_node(parent_uid=None, after_uid=<previous_proposed_chapter_uid_or_None>)`，并更新 simulation。不得把 proposed chapter 的 blocks 嵌入 chapter insert。
2. **Reorder proposed chapters**：按 proposed chapter order 从左到右扫描所有 proposed chapters（包括刚插入的 empty containers）。如果 chapter 不在目标相对位置，生成 chapter-level `move_node(..., after_uid=<previous_proposed_chapter_uid_or_None>)`。base-only chapters 可以暂时留在 simulation 中的任意位置，后续删除。
3. **Place proposed blocks**：对每个 proposed chapter，按 proposed block order 从左到右扫描：
   - 若 block UID 不存在于 base simulation，生成 block-level `insert_node(parent_uid=<target_chapter_uid>, after_uid=<previous_block_uid_or_None>, node=<proposed block snapshot>)`。
   - 若 block UID 已存在，但 parent 或相对顺序不同，生成 `move_node(target_uid=<block_uid>, from_parent_uid=<current_parent>, to_parent_uid=<target_chapter_uid>, after_uid=<previous_block_uid_or_None>)`。
   - 若 block 已在正确位置，不生成 change。
   - 每一步都更新 simulation，使同一 chapter 中后续 block 可以锚定到刚插入/刚移动的 previous block。
4. **Delete leftover blocks**：扫描 simulation 中仍存在但 proposed 全书不存在的 block UID，生成 `delete_node`。这些 blocks 可能位于 surviving chapters，也可能位于待删除的 base-only chapters。
5. **Delete emptied missing chapters**：对 base 中存在、proposed 中不存在的 chapter，确认 simulation 中 blocks 已为空后生成 chapter-level `delete_node`。`old_node` 使用当前 empty snapshot：`base_chapter.model_copy(update={"blocks": []})`。

#### 7.5.3 必须覆盖的 chapter-like diff 场景

该 planner 必须通过以下 round-trip 场景：

- **Split chapter**：base `ch-a` 的后半 blocks 在 proposed 中移动到新 `ch-b`。计划应先插入 empty `ch-b`，再 move 相关 blocks 到 `ch-b`，保留 `ch-a`。
- **Merge chapter**：base `ch-b` 在 proposed 中消失，其 blocks 移动到 `ch-a`。计划应先 move blocks 到 `ch-a`，再删除 empty `ch-b`。
- **New chapter mixed existing/new blocks**：proposed 新 `ch-x` 同时包含从旧 chapter 移来的 existing blocks 和新创建 blocks。计划应先插 empty `ch-x`，再按 proposed order 混合 move/insert blocks。
- **Delete chapter but keep some blocks**：base `ch-z` 在 proposed 中消失，但其中部分 blocks 移到其他 chapter，其余 blocks 删除。计划应先 move retained blocks，再 delete leftover blocks，最后 delete empty `ch-z`。
- **Pure new chapter**：proposed 新 chapter 的所有 blocks 都是新 UID。计划仍使用 empty chapter insert + block inserts，而不是 full-node chapter insert。

### 7.6 删除非空 chapter 的 precondition

现有 `delete_node` 只能删除 empty chapter。Topology phase 5 删除 missing chapter 时，`old_node` 必须是“blocks 已删除/移走后的当前快照”：

```python
old_chapter_empty = base_chapter.model_copy(update={"blocks": []})
```

不要把 base 中含 blocks 的完整 chapter snapshot 放进 chapter delete 的 `old_node`，否则 `apply_book_patch()` 在 sequential apply 时会看到当前 chapter 已为空，导致 precondition 与 full old_node 不匹配。

### 7.7 Move/reorder determinism

直接比较 base index 与 proposed index 会在复杂 swap / rotate 中生成难以应用的 move 序列。上述 simulated planner 的确定性要求：

- chapter 扫描顺序使用 proposed chapter order。
- block 扫描顺序使用 proposed block order。
- leftover block delete 使用 base order（chapter order + block order）或稳定 UID order；推荐 base order，便于 review。
- missing chapter delete 使用 base chapter order。
- `after_uid` 始终指向当前 simulation 中目标容器内的 previous proposed sibling，避免锚定到稍后会被删除的 base-only sibling。

优势：

- 支持 swap / rotate / cross-chapter move。
- 支持 split/merge chapter-like diff，不产生重复 UID，不丢失 moved blocks。
- `after_uid` 不需要只锚定 base 中已存在的 UID；它可以指向本 patch 前面已经插入或移动到位的 UID。
- 与 `apply_book_patch()` 的 sequential semantics 一致。

### 7.8 Change order：apply-safe first, review-friendly second

D3 要求按 node 空间邻近性排序，但当前 `BookPatch` 的 `changes` 同时也是 apply order。默认策略：

1. **Patch order 以可应用为硬约束**。
2. 在每个 apply-safe phase 内按空间邻近性排序。
3. CLI 可额外输出 `review_groups`（不进入 `BookPatch` schema），按 proposed chapter/block 坐标分组展示。

推荐 patch order：

```text
1. insert new target chapter containers as empty chapters
2. reorder/move proposed chapters using simulated proposed order
3. insert/move blocks into proposed chapters using simulated proposed order
4. delete leftover blocks that are absent in proposed
5. delete now-empty chapters that are absent in proposed
6. replace_node for same UID kind changes, table merge_record changes, and paired/orphan coupled footnote changes
7. set_field for remaining chapter/block editable fields
```

如果某个顺序与 round-trip 测试冲突，以 round-trip/apply success 为准，并在 CLI/review output 中补足空间分组展示。

---

## 8. CLI / tool surface

CLI 是建议实现项，但优先级低于 library API 与 tests。

### 8.1 最小 CLI

```bash
epubforge editor diff-books <work> \
  --base-file <path/to/base/book.json> \
  --proposed-file <path/to/proposed/book.json>
```

可选简写：

```bash
epubforge editor diff-books <work> \
  --proposed-file <path/to/proposed/book.json>
```

此时 base 默认为 `<work>/edit_state/book.json`。

### 8.2 输出 JSON

```json
{
  "diff_applies": true,
  "round_trip_verified": true,
  "change_count": 3,
  "base_sha256": "...",
  "proposed_sha256": "...",
  "patch": {
    "patch_id": "...",
    "agent_id": "diff-engine",
    "scope": {"chapter_uid": null},
    "changes": [],
    "rationale": "No semantic changes detected between base and proposed Book snapshots.",
    "evidence_refs": []
  },
  "unsupported_diffs": [],
  "review_groups": []
}
```

错误：

- invalid JSON / invalid `Book` schema → exit code 1
- duplicate/missing UID → exit code 1
- unsupported Book-level or immutable delta → exit code 2（如果项目 CLI 暂无错误码约定，也可统一 exit code 1，但 JSON error 中要区分 `kind`）
- generated patch apply failed → exit code 2

---

## 9. 测试策略

### 9.1 单元测试：`tests/editor/test_diff.py`

#### 基础 fixtures

- `_prov(page=1, source="passthrough")`
- `_make_book(chapters=[...])`
- `_make_chapter(uid, blocks=[...])`
- `_assert_round_trip(base, proposed)`：
  ```python
  patch = diff_books(base, proposed)
  result = apply_book_patch(base, patch)
  assert result.model_dump(mode="json") == proposed.model_dump(mode="json")
  ```

#### 必测用例

| 类别 | 用例 |
|---|---|
| identity | `base == proposed` 返回 empty patch，apply 后等价 |
| field diff | paragraph text、heading level/id、chapter title、table html、footnote paired/orphan；paired/orphan 同时变化必须走安全 replace_node 或等价 atomic 策略 |
| insert | insert block at head/middle/tail；insert new empty chapter container + block inserts（不使用 full-node chapter insert） |
| delete | delete block；delete empty chapter；delete non-empty chapter via block deletes + empty chapter delete |
| move | same-chapter reorder、cross-chapter move、chapter reorder、swap、rotate；split chapter；merge chapter；new chapter mixed existing/new blocks；delete chapter but retain/move subset blocks |
| replace | paragraph→heading、table→figure、equation→paragraph |
| table merge_record | same UID table `merge_record` delta 生成 `replace_node` 并 round-trip；insert/replace table payload 保留 `merge_record`；旧 patch 缺省 `merge_record` 仍合法 |
| combined | 同一 book 中混合 insert/delete/move/replace/set_field |
| UID errors | `uid=None`、duplicate chapter UID、duplicate block UID、chapter/block UID collision |
| unsupported | Book `initialized_at` / `uid_seed` / title/authors/language/source_pdf/extraction change；provenance-only change |
| ordering | patch 顺序可 apply；review grouping（如实现）按 chapter/block 坐标稳定 |

### 9.2 CLI tests：`tests/editor/test_diff_cli.py`

如果实现 CLI，增加：

| 用例 | 断言 |
|---|---|
| `test_cli_diff_books_files` | 输出 JSON 包含 `patch`, `change_count`, sha256 |
| `test_cli_diff_current_book_default_base` | 不传 `--base-file` 时读取 `<work>/edit_state/book.json` |
| `test_cli_invalid_book_json` | 非法 JSON 报错，exit code 非 0 |
| `test_cli_duplicate_uid` | duplicate UID 报错，错误信息可诊断 |
| `test_cli_round_trip_verified` | 默认或 `--verify` 模式中 round-trip 成功 |

### 9.3 质量 gates

实现完成后至少运行：

```bash
uv run pytest tests/editor/test_diff.py
uv run pytest tests/editor/test_diff_cli.py  # 如果实现 CLI
uv run pyrefly check
```

如 pyrefly 全仓较慢，可先运行 diff 相关模块的 targeted check；最终交付前仍应跑项目约定的完整质量门禁。

---

## 10. 分阶段实施任务

### Sub-phase 6A：Schema alignment 与 empty patch 支持（必须先做）

任务：

1. 决定并实现 empty diff 表示。默认：允许 `BookPatch.changes=[]`。
2. 更新 `validate_book_patch()` / `apply_book_patch()` 对 empty patch 的行为。
3. 扩展 table patch payload 支持 `merge_record`，确保 insert/replace/old_node precondition 可保留完整 TableMergeRecord。
4. 从 `patches.py` 导出公共 helper：allowed set fields 与 patch field value serialization，供 `diff.py` 使用。
5. 添加 empty patch tests，确保 `apply_book_patch(base, empty_patch)` 不失败。

验收：

- `test_round_trip_identity` 通过。
- 现有 BookPatch tests 如因 min_length 变化失败，应更新测试预期，说明 empty patch 是合法 no-op semantic delta。
- Table payload round-trip tests 证明 `merge_record` 不被丢弃。

### Sub-phase 6B：核心索引与输入验证

任务：

1. 新增 `editor/diff.py` 与 `DiffError`。
2. 实现全书 UID validation：non-null、全局唯一、chapter/block collision 检测。
3. 构建 `BookDiffIndex`。
4. 检测 unsupported Book-level / immutable deltas。

验收：

- UID 错误用例全部抛 `DiffError`。
- unsupported deltas 报错信息包含字段名、UID（如有）和默认建议。

### Sub-phase 6C：字段 diff 与 replace_node

任务：

1. 实现 `_compare_node_fields()`。
2. 实现 same UID block kind change → `replace_node`。
3. 实现 table `merge_record` delta → `replace_node`。
4. 实现 footnote `paired` / `orphan` 同时变化 → `replace_node`，避免非法中间状态。
5. 确保 `old` / `old_node` precondition 与现有 `apply_book_patch()` 序列化规则一致。

验收：

- 所有 field diff tests 通过。
- 所有 replace kind tests 通过。
- paired/orphan 同时切换 tests 不因 sequential invariant 检查失败。
- table merge_record delta tests 可 round-trip。

### Sub-phase 6D：Topology planner

任务：

1. 实现 empty target chapter container insert。
2. 实现 chapter reorder，确保 proposed chapter order 中的新容器已存在。
3. 实现 block insert/move/reorder 的 simulated order planner，所有 blocks 按 proposed order 落位。
4. 实现 leftover block delete。
5. 实现 now-empty missing chapter delete，`old_node` 使用 empty snapshot。
6. 覆盖 swap / rotate / cross-chapter move / split chapter / merge chapter / new chapter mixed existing-new blocks / delete chapter but retain subset blocks。

验收：

- insert/delete/move/chapter reorder tests 全部 round-trip。
- 非空 chapter 删除的 precondition 不失败。
- 不生成 full-node chapter insert；新 chapter 内 blocks 均通过 block-level insert/move 进入。

### Sub-phase 6E：CLI 与 review output（可延后到 Phase 7 前）

任务：

1. 新增 `run_diff_books()`。
2. 新增 `epubforge editor diff-books` 命令。
3. 输出 patch JSON、sha256、change_count、unsupported diagnostics。
4. 可选输出 `review_groups`。

验收：

- CLI 能比较两个 Book JSON 文件。
- CLI 不写 `edit_state/book.json`，只读输入并输出 JSON。

### Sub-phase 6F：Phase 7 readiness checks

任务：

1. 添加一个模拟 Git merge 后的 proposed Book fixture。
2. 验证 `diff_books(base, proposed)` 输出可用于 semantic validation。
3. 文档说明 Phase 7 如何接入：base/proposed 由 Git refs 解析得到，Phase 6 不直接操作 Git。

验收：

- Phase 7 可以只依赖 `diff_books()` / CLI，不需要重新定义 semantic diff 规则。

---

## 11. 验收标准

### 11.1 必须满足

1. `diff_books` 对合法输入 deterministic：相同输入生成相同 semantic changes（忽略 UUID patch_id 时）。
2. 支持范围内 round-trip 通过：`apply_book_patch(base, diff_books(base, proposed)) == proposed`。
3. `old` / `old_node` preconditions 在 base 上 apply 时全部匹配。
4. Missing/duplicate UID 被拒绝。
5. Same UID block kind 变化生成 `replace_node`。
6. UID change 不被推断为 rename，而是 delete + insert。
7. 新 chapter 通过 empty container insert + block-level insert/move 构建，不使用 full-node chapter insert；split/merge chapter-like diff 可 round-trip。
8. 非空 chapter 删除通过“先移动/删除 blocks，再 delete empty chapter snapshot”实现。
9. Footnote `paired` / `orphan` 同时变化不会生成不可 apply 的字典序 set_field 序列；默认用 `replace_node`。
10. Table `merge_record` delta 可通过扩展后的 table payload + `replace_node` round-trip；不得静默忽略。
11. 不通过 `AgentOutput` 正常提交路径调用 `diff_books`。

### 11.2 应满足

1. Patch order apply-safe，且在安全范围内按空间邻近性组织。
2. CLI 输出可机器读取的 JSON。
3. 错误信息包含可操作诊断：字段、UID、reason、建议。
4. 大书 O(n) 或 O(n log n)；不引入明显二次复杂度热点。

### 11.3 可延后

1. Book 顶层字段 diff。
2. 完整 hclust review grouping。
3. Git ref 直接输入（`--base-ref`, `--proposed-ref`）。
4. Difftastic / projection diff 展示。

---

## 12. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Empty diff 与当前 `BookPatch.changes min_length=1` 冲突 | identity diff 无法表示，主设计 round-trip 破损 | 默认放宽 `changes` 允许空列表，并更新 tests |
| Book 顶层字段无 UID | Full Book round-trip 无法覆盖 metadata | MVP 报 unsupported；后续设计显式 metadata patch，不用隐藏伪 UID |
| Patch order 与空间排序冲突 | review-friendly order 可能 apply 失败 | apply-safe order 优先；CLI 输出单独 review groups |
| 删除非空 chapter 的 precondition 易错 | 用 base full chapter old_node 会失败 | chapter delete old_node 使用 blocks 已删除/移走后的 empty snapshot |
| New chapter full-node insert 与 split/merge chapter diff 冲突 | 可能重复 UID、丢失 moved blocks 或提前删除源 chapter | 禁止 full-node chapter insert；先插 empty target chapter，再按 proposed order move/insert blocks，最后删 empty source chapter |
| `provenance` immutable 但 proposed 可能变化 | round-trip 失败或非法 set_field | 默认报 unsupported immutable delta；不要偷偷忽略 provenance-only delta |
| Move planner 在 swap/rotate 中生成不可应用序列 | apply precondition 失败 | 使用 simulated order planner，并以 round-trip tests 覆盖 swap/rotate |
| Footnote `paired` / `orphan` 同时切换 | 字段字典序 set_field 可能产生非法中间状态 | 同时变化时用 `replace_node`；增加 paired↔orphan round-trip tests |
| `Table.merge_record` payload 缺口 | Table round-trip 边界模糊，replace/insert 可能丢 provenance | Phase 6 默认扩展 table patch payload 支持 `merge_record`；same UID delta 用 `replace_node`，测试覆盖 |

---

## 13. Open questions register

> 状态说明：`default-proceed` 表示无需等待人类，按默认假设实现；`unresolved-after-review` 表示经过多轮 plan-review 仍未关闭，仍按默认假设实现并保留复核点。

| ID | 问题 | 影响 | 默认假设 / 实现路径 | 推荐决策 | 何时复核 | 状态 |
|---|---|---|---|---|---|---|
| OQ-01 | Empty diff 如何表示？ | `base == proposed` 时 `BookPatch(changes=[])` 当前不合法 | 放宽 `BookPatch.changes` 允许空列表 | 允许 empty BookPatch；no-op semantic delta 是合法 patch | 如果现有 tests 明确要求空 patch 非法且无法调整，改用 `BookDiffResult.patch=None` | default-proceed |
| OQ-02 | 是否支持 Book 顶层字段 diff？ | 不支持则 full `Book` round-trip 不覆盖 `title/authors/language/source_pdf/extraction` | MVP 检测并报 unsupported Book-level delta | 不引入 `_BOOK_ROOT_UID`；后续设计显式 metadata patch | Phase 7 实测 agent/Git workflow 会频繁修改 Book metadata 时 | default-proceed |
| OQ-03 | `provenance` 差异如何处理？ | `provenance` immutable，无法 `set_field`；忽略会破坏 full round-trip | same UID 节点的 provenance-only diff 报 unsupported immutable delta | 不生成非法 set_field；不静默吞掉 | 如果 ingestion/editor 后续允许 provenance 修订，再扩展 patch schema | default-proceed |
| OQ-04 | `Table.merge_record` 是否参与 diff？ | merge_record 是 table merge provenance；当前 TablePayload 若不支持会让 table round-trip 不严格 | 默认扩展 patch table payload 支持 `merge_record`；same UID table merge_record delta 用 `replace_node`；insert/replace node snapshot 保留完整 merge_record | 参与 diff，但不加入 `set_field`；不得静默忽略或声称 unsupported 之外的 round-trip | 如果 payload alignment 证明侵入过大或 validator 无法安全承载，则把 merge_record delta 改为显式 `DiffError` unsupported，并同步收窄验收标准 | default-proceed |
| OQ-05 | Patch order 是否必须严格按 D3 hclust？ | 严格空间排序可能导致 apply order 无效 | apply-safe order 优先；review grouping 另行输出 | `BookPatch.changes` 是 apply order，不是纯 display order | 如果 reviewer 明确需要 patch 内严格空间顺序，再考虑引入 non-applying display layer | default-proceed |
| OQ-06 | 新 chapter 插入应 full-node insert 还是 empty chapter + blocks？ | full insert 会与 split/merge chapter-like diff 冲突，可能重复 UID、丢失 moved blocks 或导致 delete precondition 失败 | 默认 empty chapter container insert；所有 blocks 通过 block-level insert/move 按 proposed order 放入；最后删除 empty missing chapters | 选择 empty+blocks，牺牲少量 change 数量换取统一 topology planner 与可应用性 | 如果未来新增 chapter-range atomic op 且能证明覆盖 split/merge 场景，再复核是否优化 change 数量 | default-proceed |
| OQ-07 | CLI 是否 Phase 6 必做？ | 无 CLI 会影响手动 debug，但 library tests 已足够验证核心 | library API 必做；CLI 可延后到 Phase 7 前 | 实现最小文件输入 CLI，除非时间不足 | Phase 7 接入前必须有 CLI 或等价 integration helper | default-proceed |
| OQ-08 | 是否实现生产 `--verify` round-trip？ | 生产 verify 增加成本，但可发现 diff bug | CLI 默认可执行 apply check；library 不在生产路径强制 deep compare | tests 强制 round-trip；CLI 输出 `round_trip_verified` | 如果性能不足或大书过慢，可把 verify 改为 flag | default-proceed |
| OQ-09 | 是否为 unsupported deltas 返回 partial patch？ | partial patch 可能误导 integration accept | 遇 unsupported delta 直接抛 `DiffError`，不返回 partial patch | fail closed，避免 semantic loss | 如果 UI 需要展示 partial diff，再引入 `BookDiffResult` | default-proceed |

---

## 14. 与 Phase 7 Git workflow 的衔接

Phase 7 不应重新实现 diff 逻辑，只负责把 Git refs / worktree 文件解析为 `Book`：

```text
base_ref/edit_state/book.json      -> base Book
merged_worktree/edit_state/book.json -> proposed Book
diff_books(base, proposed)           -> BookPatch
validate/apply/audit                 -> accept or reject
```

Phase 7 的冲突分类建议：

| 场景 | Git 结果 | Phase 6 结果 | Phase 7 行为 |
|---|---|---|---|
| 不同 blocks 修改 | Git merge 成功 | diff/apply/audit 成功 | accept |
| 同字段文本冲突 | Git conflict | 不调用 diff 或 diff 输入不可 parse | reject to reviewer/supervisor |
| Git merge 成功但 footnote invariant 破坏 | Git 成功 | `apply_book_patch` 或 audit 失败 | reject to reviewer/supervisor |
| Git merge 改了 Book metadata | Git 成功 | `DiffError unsupported Book-level delta` | reject 或 metadata-specific follow-up |

---

## 15. 参考

- 主计划：`.refactor-planning/agentic-improvement/agentic-improvement.md`
- Phase 1 BookPatch：`.refactor-planning/agentic-improvement/phase1-bookpatch.md`
- Semantic IR：`src/epubforge/ir/semantic.py`
- BookPatch / apply：`src/epubforge/editor/patches.py`
- AgentOutput workflow：`src/epubforge/editor/agent_output.py`
- PatchCommand macros：`src/epubforge/editor/patch_commands.py`
