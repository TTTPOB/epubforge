# Phase 1 实现计划：BookPatch 模型与 Validator

> 状态：草稿（R1 修订版）
> 对应设计文档：agentic-improvement.md §5、§6、§7  
> 前置条件：无（Phase 1 是全新文件，不依赖其他 Phase）
>
> **R1 修订说明**：本文件已根据 phase1-review-r1.md 的评审意见全面修订。
> 每处关键修改以 `[R1: ...]` 标注，供下一轮评审核查。

---

## 1. 文件布局

### 新增文件

| 文件路径 | 用途 |
|---|---|
| `src/epubforge/editor/patches.py` | BookPatch 模型、IRChange union、PatchScope、apply_book_patch（事务性 validate+apply 合并实现）、validate_book_patch（轻量级静态预检） |
| `tests/editor/test_patches.py` | Phase 1 全部单元测试 |

### 修改文件

无。Phase 1 只新增，不修改现有代码。

Phase 4 才会删除旧的 `ops.py` / `apply.py`。在 Phase 1 阶段，两套系统并存，互不干扰。

---

## 2. 模型定义

### 2.1 总体结构

```
patches.py
  ├── PatchError                # 运行时错误
  ├── PatchScope                # 修改范围声明
  ├── SetFieldChange            # 字段赋值
  ├── ReplaceNodeChange         # 节点整体替换
  ├── InsertNodeChange          # 节点插入
  ├── DeleteNodeChange          # 节点删除
  ├── MoveNodeChange            # 节点移动
  ├── IRChange                  # 上述 5 种的 discriminated union
  ├── BookPatch                 # 顶层 patch 容器
  ├── _BookIndex                # 内部辅助：UID 快速查找
  ├── validate_book_patch()     # 轻量级静态预检（不做深拷贝）
  └── apply_book_patch()        # 事务性 validate+apply 合并执行
```

所有 Pydantic 模型继承 `StrictModel`（即 `extra="forbid"`），与现有 editor 代码保持一致。

### 2.2 PatchError

```python
class PatchError(RuntimeError):
    """Raised when a patch cannot be validated or applied."""
    def __init__(self, reason: str, patch_id: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.patch_id = patch_id
```

直接类比现有 `ApplyError`，携带 `patch_id` 便于调用方定位来源。

### 2.3 PatchScope [R2: simplified — removed book_wide, chapter_uid=None means book-wide]

```python
class PatchScope(StrictModel):
    chapter_uid: str | None = None
```

语义：
- `chapter_uid` 非 None：patch 只能修改该章节内的节点。
- `chapter_uid=None`：patch 可修改任意节点（包括跨章节结构）。

实际约束：当 `chapter_uid` 非 None 时，`changes` 里所有 `target_uid` / `parent_uid` 必须属于该章节，否则 `apply_book_patch` 拒绝。

### 2.4 五种 IRChange

所有 change 类型均使用 `op` 字段作为 discriminator。

#### 2.4.1 SetFieldChange

```python
class SetFieldChange(StrictModel):
    op: Literal["set_field"]
    target_uid: str          # block uid 或 chapter uid（非空字符串）
    field: str               # 字段名，见下方可编辑字段表（非空字符串）
    old: Any                 # 当前预期值（precondition），JSON-compatible 类型
    new: Any                 # 目标值，JSON-compatible 类型

    @field_validator("target_uid", "field")
    @classmethod
    def _require_non_empty(cls, value: str, info: Any) -> str:
        return require_non_empty(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _no_op_check(self) -> SetFieldChange:
        if self.old == self.new:
            raise ValueError("set_field old and new must differ")
        return self
```

**[R1: S3 addressed]** `old` 与 `new` 的类型注解改为 `Any`（从 `object`），并在文档中明确序列化约定：

**`old` / `new` 序列化约定**：
- 两者均必须是 JSON-compatible 类型：`str | int | float | bool | None | list | dict`。
- patch 提交方在构造 `SetFieldChange` 时，通过对当前节点调用 `node.model_dump(mode="json")` 获取字段值（该方法将所有嵌套 Pydantic 对象递归序列化为 JSON-compatible dict/list），然后将目标字段的序列化值填入 `old`。
- precondition 比较时（apply 阶段），系统同样对当前节点的对应字段调用相同的序列化方式，再与 `change.old` 做深度比较（`==`）。这确保 `Provenance`、`list[float]` 等复杂类型的比较是在统一的 JSON 表示上进行的，不会出现 Pydantic 对象与 dict 的混合比较问题。
- `old == new` 的相等比较在 JSON-compatible 表示下进行，`1` 与 `1.0` 的比较结果是 `True`（Python 语义）。若需要严格类型检查，patch 提交方应在生成阶段自行过滤。

**`target_uid` / `field` 约束**：均通过 `require_non_empty` 验证，拒绝空字符串和纯空白字符串。

#### 2.4.2 ReplaceNodeChange

```python
class ReplaceNodeChange(StrictModel):
    op: Literal["replace_node"]
    target_uid: str              # 被替换节点的 uid
    old_node: dict[str, Any]     # 被替换节点的完整序列化快照（precondition），包含 uid 和 kind
    new_node: dict[str, Any]     # 替换后节点的完整序列化内容，必须包含 kind，不含 uid

    @model_validator(mode="after")
    def _validate_new_node(self) -> ReplaceNodeChange:
        if "uid" in self.new_node:
            raise ValueError("new_node must not contain uid — it is injected at apply time")
        if "kind" not in self.new_node:
            raise ValueError("new_node must contain a kind field")
        return self
```

用途：整体替换一个 block（可跨 kind 替换，例如 paragraph → heading）。

**[R1: S4 addressed]** `old_node` 序列化约定如下：
- `old_node` 必须包含 `uid` 和 `kind` 字段（与节点的完整快照对应）。
- patch 提交方通过 `current_block.model_dump(mode="python")` 生成 `old_node`（`mode="python"` 保留 Python 原生类型如 `bool`、`int`，不做 JSON string 转换）。
- precondition 比较时，系统对当前节点调用 `current_block.model_dump(mode="python")`，结果与 `change.old_node` 做深度比较（`==`）。
- 同样的约定适用于 `DeleteNodeChange.old_node`。

**[R1: D6 addressed]** `ReplaceNodeChange` 仅用于 block 级别的替换，不用于替换 chapter。chapter metadata 修改通过 `SetFieldChange`（`title`、`level`、`id` 字段），chapter 结构变更通过 `delete_node` + `insert_node` 组合。这样避免了"替换 chapter 时如何处理 blocks 列表"的歧义语义。

若将来需要 chapter 级整体替换，可引入 `ReplaceChapterMetaChange` 专用类型（Phase 3 评估）。

`new_node` 不含 uid：apply 时保留原 `target_uid` 注入。如果需要更换 uid（例如改变 block kind 时想重新分配 uid），应使用 `delete_node` + `insert_node` 组合。

#### 2.4.3 InsertNodeChange

```python
class InsertNodeChange(StrictModel):
    op: Literal["insert_node"]
    parent_uid: str | None       # 父容器 uid：chapter_uid 表示插入 block；None 表示插入 chapter 到 book
    after_uid: str | None        # 插入位置：None 表示插到父容器的最前面
    node: dict[str, Any]         # 新节点完整内容，必须包含 uid 和 kind

    @model_validator(mode="after")
    def _validate_node(self) -> InsertNodeChange:
        if "uid" not in self.node:
            raise ValueError("insert_node.node must contain a uid field")
        if "kind" not in self.node:
            raise ValueError("insert_node.node must contain a kind field")
        return self
```

用途：在指定父节点中插入新节点。

父子关系：
- `parent_uid` 为 chapter uid：在该章节 blocks 中插入 block
- `parent_uid` 为 None：在 book.chapters 中插入新章节（需要 `scope.chapter_uid=None`）

`node` 必须包含新节点的 `uid`（由 patch 提交方分配，需全局唯一）。

**[R1: D4 addressed]** InsertNodeChange 的内部 Pydantic 校验路径：
- 当 `node["kind"]` 在 `BLOCK_KINDS` 中时：使用 `BLOCK_PAYLOAD_MODELS[kind].model_validate({k: v for k, v in node.items() if k not in ("uid", "kind")})` 做字段合法性校验。
- 当 `parent_uid is None`（插入 chapter）时：使用 `Chapter.model_validate(node)` 做完整校验。Chapter 模型要求 `title` 字段，`blocks` 默认为空列表（也可含 blocks，但 apply 时会完整写入）。

#### 2.4.4 DeleteNodeChange

```python
class DeleteNodeChange(StrictModel):
    op: Literal["delete_node"]
    target_uid: str              # 被删除节点的 uid
    old_node: dict[str, Any]     # 删除前节点完整快照（precondition），序列化约定同 ReplaceNodeChange.old_node
```

`old_node` 作为 precondition：apply 时将当前节点序列化与 `old_node` 比较，不一致则拒绝（optimistic locking）。序列化约定：`current_node.model_dump(mode="python")`。

注意：当被删节点是 chapter 时，验证阶段必须确认该 chapter 的 blocks 为空（或 patch 中已预先 delete 所有 blocks）。空章节删除允许；非空章节删除拒绝——这是 Book IR 的拓扑安全规则（若允许批量删除，使用 `scope.chapter_uid=None` 并在 changes 列表中先 delete 所有 blocks 再 delete chapter）。

#### 2.4.5 MoveNodeChange

```python
class MoveNodeChange(StrictModel):
    op: Literal["move_node"]
    target_uid: str              # 被移动节点的 uid
    from_parent_uid: str | None  # 移动前父容器 uid（precondition，用于确认当前位置）
    to_parent_uid: str | None    # 移动后父容器 uid
    after_uid: str | None        # 移动后插入位置：None 表示插到目标容器最前面

    @model_validator(mode="after")
    def _validate_no_self_ref(self) -> MoveNodeChange:
        if self.after_uid is not None and self.after_uid == self.target_uid:
            raise ValueError("move_node after_uid must differ from target_uid")
        return self
```

用途：跨章节或章内移动 block；也可用于调整 chapter 顺序（`from_parent_uid` 和 `to_parent_uid` 均为 None 时，chapter 在 book.chapters 中移动）。

**[R1: D5 addressed]** chapter 移动的完整语义：
- 当 `from_parent_uid=None` 且 `to_parent_uid=None` 时，`target_uid` 是 chapter uid，`after_uid` 必须是另一个 chapter 的 uid（或 None 表示移到最前）。
- apply 时在 `book.chapters` 中查找 `after_uid`（而非在任何 chapter 的 blocks 中查找）。
- 校验时：`target_uid` 在 `chapter_index` 中查找；`after_uid` 非 None 时也在 `chapter_index` 中查找。

### 2.5 IRChange Union

```python
IRChange = Annotated[
    SetFieldChange
    | ReplaceNodeChange
    | InsertNodeChange
    | DeleteNodeChange
    | MoveNodeChange,
    Field(discriminator="op"),
]
```

**[R1: V1 addressed]** IRChange union 是封闭的五种原子类型。batch change（如 `move_block_range`）通过 Phase 3 的 PatchCommand macro 实现（编译为多个 MoveNodeChange），不在 IRChange 层面扩展。这是有意的设计决策，避免 IRChange union 随 macro 需求不断膨胀。

### 2.6 BookPatch

```python
class BookPatch(StrictModel):
    patch_id: str                        # UUID4
    agent_id: str                        # 非空字符串
    scope: PatchScope
    changes: list[IRChange] = Field(min_length=1)
    rationale: str                       # 非空字符串，说明修改理由
    evidence_refs: list[str] = []        # 可选，VLMObservation id 等

    @field_validator("patch_id")
    @classmethod
    def _validate_patch_id(cls, value: str) -> str:
        return validate_uuid4(value, field_name="patch_id")

    @field_validator("agent_id", "rationale")
    @classmethod
    def _validate_required_text(cls, value: str, info: Any) -> str:
        return require_non_empty(value, field_name=info.field_name)
```

字段说明：<!-- [R2: removed base_version/op_log_version] -->
- `patch_id`：UUID4，在 validate/apply 时作为 PatchError 的 context key
- `scope`：决定 apply 如何做范围检查
- `changes`：至少一个 change（禁止空 patch）
- `rationale`：强制非空，保障每个 patch 有可追溯原因
- `evidence_refs`：可引用 VLM observation id 或其他证据

---

## 3. 可编辑字段表

不同 block kind 允许通过 `SetFieldChange` 修改的字段如下。Apply 需按此表拒绝非法字段修改请求。

| block kind | 允许 set_field 的字段 |
|---|---|
| paragraph | `text`, `role`, `style_class`, `cross_page`, `display_lines` |
| heading | `text`, `level`, `id`, `style_class` |
| footnote | `callout`, `text`, `paired`, `orphan`, `ref_bbox` |
| figure | `caption`, `image_ref`, `bbox` |
| table | `html`, `table_title`, `caption`, `continuation`, `multi_page`, `bbox` |
| equation | `latex`, `image_ref`, `bbox` |
| chapter（用 target_uid = chapter.uid）| `title`, `level`, `id` |

**[R1: D3 addressed]** 恢复了 `table.bbox` 字段（`semantic.py` 第 149 行 `Table` 模型含 `bbox: list[float] | None = None`，此前遗漏）。

**不允许** 通过 `set_field` 修改的字段：
- `uid`（任何节点）：uid 变更必须通过 `delete_node` + `insert_node`
- `kind`（任何节点）：kind 变更必须通过 `replace_node`
- `provenance`（任何 block）：provenance 仅可通过 `replace_node` 整体替换
- `merge_record`（Table）：使用专用 `PatchCommand`（Phase 3）处理，Phase 1 通过 `replace_node` 临时覆盖

禁止修改字段列表在 `patches.py` 中以常量维护：

```python
# Fields that cannot be set via SetFieldChange (must use replace_node or insert/delete)
_IMMUTABLE_FIELDS = frozenset({"uid", "kind", "provenance"})

# Per-kind allowed fields for SetFieldChange
_ALLOWED_SET_FIELD: dict[str, frozenset[str]] = {
    "paragraph": frozenset({"text", "role", "style_class", "cross_page", "display_lines"}),
    "heading":   frozenset({"text", "level", "id", "style_class"}),
    "footnote":  frozenset({"callout", "text", "paired", "orphan", "ref_bbox"}),
    "figure":    frozenset({"caption", "image_ref", "bbox"}),
    "table":     frozenset({"html", "table_title", "caption", "continuation", "multi_page", "bbox"}),
    "equation":  frozenset({"latex", "image_ref", "bbox"}),
    "chapter":   frozenset({"title", "level", "id"}),
}

# [R1: S2 addressed] Explicit heading level constraint — Heading.level in IR is int (no Literal
# constraint), so patches.py must enforce this independently of the IR model.
_VALID_HEADING_LEVELS: frozenset[int] = frozenset({1, 2, 3})

# [R1: T2 addressed] Chapter.level is also int with no range constraint in IR — enforce here.
_VALID_CHAPTER_LEVELS: frozenset[int] = frozenset({1, 2, 3})
```

**[R1: S2 addressed]** `Heading.level` 在 `semantic.py` 中声明为 `int = 1`（无 Literal 约束，第 97 行），`Chapter.level` 同理（第 171 行）。不能依赖 IR Pydantic 模型来约束 level 范围——`patches.py` 必须通过 `_VALID_HEADING_LEVELS` 和 `_VALID_CHAPTER_LEVELS` 显式检查。`ops.py` 的 `HeadingPayload` 使用 `Literal[1, 2, 3]`（第 72 行）是 EditOp 层的约束，不影响 IR 层。

---

## 4. Apply 逻辑（事务性 validate+apply 合并）

**[R1: D2 addressed / T4 addressed]** 原计划将 `validate_book_patch` 和 `apply_book_patch` 拆为两个独立函数，但评审 D2 和 T4 共同揭示了一个根本设计问题：

**precondition 歧义问题**：如果 validate 阶段基于原始 book 状态逐一检查 changes 的 precondition，则对同一节点的链式修改（例如先 `set_field(field="text", old="a", new="b")` 再 `set_field(field="text", old="b", new="c")`）会在第二个 change 的 precondition 检查时失败——因为此时原始 book 的 `text` 仍为 `"a"`，而 `change.old="b"` 不匹配。这导致合法的链式修改被错误拒绝。

**解决方案：合并 validate 和 apply 为单一事务性操作。** 不再有独立的全量 validate 阶段——precondition 检查在对副本执行每个 change 之前即时完成（增量式，针对已演化的中间状态），若任何检查失败则整个操作回滚（副本丢弃，原 book 不受影响）。

### 4.1 函数签名

```python
def apply_book_patch(book: Book, patch: BookPatch) -> Book:
    """Validate and apply a BookPatch to a Book in a single transactional operation.

    Performs all precondition checks incrementally against the evolving working copy.
    Returns a new Book on success. <!-- [R2: removed base_version/op_log_version] -->
    Raises PatchError on any validation or apply failure.
    The input book is never modified.
    """

def validate_book_patch(book: Book, patch: BookPatch) -> None:
    """Lightweight static pre-check — no deep copy, no precondition evaluation.

    Only checks: book uid-None guard, scope consistency, and static
    schema-level constraints (uid uniqueness within patch, field name legality, etc.).
    <!-- [R2: removed base_version/op_log_version] -->
    Does NOT verify old/new preconditions (those require transactional apply).
    Raises PatchError if any static check fails.
    """
```

两函数职责分工：
- `validate_book_patch`：轻量级，不做深拷贝，不评估 old/new precondition。适合 agent 提交前的快速预校验（例如检查 UUID 格式、字段名合法性、scope 一致性）。
- `apply_book_patch`：完整事务性执行，内部做一次深拷贝，在副本上按序应用每个 change（包含增量 precondition 检查），成功则返回副本，失败则抛出 PatchError。

### 4.2 apply_book_patch 执行流程

```
1. validate_book_patch(book, patch)  — 静态预检（快速失败，无深拷贝）
2. 前置条件：确认 book 中所有 uid 均非 None（见 §4.3）
3. working = book.model_copy(deep=True)  — 唯一一次深拷贝
4. index = _build_index(working)  — 初始索引
5. 对 patch.changes 中每个 change，按顺序执行：
   a. _check_change_preconditions(working, change, index, patch.patch_id)  — 增量 precondition 检查
   b. _apply_change(working, change, index, patch.patch_id)                — 修改 working
   c. 若 change 涉及结构变化（insert/delete/move），重建 index             — 见 §4.6
6. return working <!-- [R2: removed base_version/op_log_version] -->
```

若步骤 5 中任何 change 的 precondition 检查或 apply 操作失败，立即抛出 `PatchError`，working 副本被丢弃，原 `book` 不受影响。

### 4.3 前置条件：uid=None 守卫

**[R1: S1 addressed]** `semantic.py` 第 82 行 `_UidMixin.uid: str | None = None` 和第 169 行 `Chapter.uid: str | None = None` 表明 Book IR 中合法存在 `uid=None` 的节点。BookPatch 系统无法为无 uid 的节点建立索引，因此在 apply 入口处强制检查：

```python
def _require_all_uids_non_none(book: Book, patch_id: str) -> None:
    """Verify every chapter and block in the book has a non-None uid.

    BookPatch can only operate on fully-initialized books where all nodes
    have been assigned stable uids (typically by uid_init stage).
    """
    for ch_idx, chapter in enumerate(book.chapters):
        if chapter.uid is None:
            raise PatchError(
                f"chapter at index {ch_idx} has uid=None — "
                "BookPatch requires all chapters and blocks to have non-None uids. "
                "Run uid_init stage before applying patches.",
                patch_id,
            )
        for b_idx, block in enumerate(chapter.blocks):
            if block.uid is None:
                raise PatchError(
                    f"block at chapter[{ch_idx}].blocks[{b_idx}] (kind={block.kind}) "
                    f"has uid=None — BookPatch requires all nodes to have non-None uids.",
                    patch_id,
                )
```

这一检查在 `apply_book_patch` 的深拷贝之前执行（对原始 book 直接检查），避免不必要的拷贝开销。同时在 `validate_book_patch` 中也执行此检查，保证轻量级静态预检即可发现该问题。

**设计说明**：BookPatch 系统是面向已初始化书籍（uid_init 阶段完成后）的编辑工具。如果将来希望支持部分初始化的书籍，应在 Phase 4 将 `_UidMixin.uid` 改为 `str`（必填），但那超出 Phase 1 范围。

### 4.4 validate_book_patch 静态预检步骤

```python
def validate_book_patch(book: Book, patch: BookPatch) -> None:
    """Lightweight static pre-check."""
```

按顺序执行以下检查（不做深拷贝，不评估 precondition）：

#### 步骤 1：uid=None 守卫

调用 `_require_all_uids_non_none(book, patch.patch_id)`。

#### 步骤 2：建立只读索引用于静态检查<!-- [R2: removed base_version/op_log_version]，原步骤 3 重编号为步骤 2 -->

```python
block_index: dict[str, tuple[int, int]] = {}   # uid -> (chapter_idx, block_idx)
chapter_index: dict[str, int] = {}             # uid -> chapter_idx
```

遍历 `book.chapters`，填充两个索引。若发现 uid 重复，直接 `PatchError("duplicate uid in book")`（Book IR 内部不变量）。

#### 步骤 3：逐 change 静态检查（不含 precondition）<!-- 原步骤 4 重编号为步骤 3 -->

仅检查 target_uid/parent_uid 的存在性、字段名合法性、scope 一致性等不依赖 old/new 值的规则：

**SetFieldChange 静态检查：**
1. `target_uid` 在 block_index 或 chapter_index 中存在
2. 确定节点类型（block kind 或 chapter）
3. `field` 不在 `_IMMUTABLE_FIELDS`
4. `field` 在 `_ALLOWED_SET_FIELD[kind]` 中

**ReplaceNodeChange 静态检查：**
1. `target_uid` 存在
2. `change.new_node["kind"]` 合法（在 BLOCK_KINDS 中）
3. 使用对应 Pydantic Payload 模型对 `change.new_node` 做 `model_validate`（不含 uid/kind）

**InsertNodeChange 静态检查：**
1. 若 `parent_uid` 非 None：在 chapter_index 中存在
2. `change.node["uid"]` 不在 block_index 或 chapter_index（uid 不重复）
3. `change.node["kind"]` 合法
4. 使用对应模型校验 node 内容

**DeleteNodeChange 静态检查：**
1. `target_uid` 存在

**MoveNodeChange 静态检查：**
1. `target_uid` 存在
2. 若 `to_parent_uid` 非 None：在 chapter_index 中存在
3. 若 `from_parent_uid=None` 且 `to_parent_uid=None`：`target_uid` 必须在 chapter_index 中（只有 chapter 可以做 book-level 移动）

#### 步骤 5：PatchScope 范围检查

当 `patch.scope.chapter_uid` 非 None 时，遍历所有 changes，检查每个 change 涉及的节点是否都属于该 chapter：
- SetFieldChange / ReplaceNodeChange / DeleteNodeChange：`target_uid` 必须属于 `scope.chapter_uid`
- InsertNodeChange：`parent_uid` 必须等于 `scope.chapter_uid`
- MoveNodeChange：`from_parent_uid` 和 `to_parent_uid` 都必须等于 `scope.chapter_uid`（跨章节 move 需要 `scope.chapter_uid=None`）

#### 步骤 6：全局 UID 唯一性（changes 内）

`InsertNodeChange` 的 `change.node["uid"]` 在同一 patch 的所有 changes 中必须唯一，防止两个 insert 使用相同新 uid。

### 4.5 增量 precondition 检查

**[R1: T4 addressed]** precondition 检查在每个 change 执行前、针对 `working`（已应用前序 changes 的副本）进行，而非原始 book。这使链式修改成为可能。

`_check_change_preconditions(working, change, index, patch_id)` 的规则：

**SetFieldChange precondition：**

```python
# Get current field value from working copy (already has prior changes applied)
current_value = _get_node_field_value(working, change.target_uid, change.field, index)
# Serialize current value using same convention as patch submitter
current_serialized = _serialize_field_value(current_value)
if current_serialized != change.old:
    raise PatchError(
        f"set_field precondition mismatch for {change.target_uid}.{change.field}: "
        f"expected old={change.old!r}, got {current_serialized!r}",
        patch_id,
    )
```

其中 `_serialize_field_value` 对复杂类型（Pydantic 模型实例）调用 `.model_dump(mode="json")`，对 Python 原生类型直接返回（`str`、`int`、`float`、`bool`、`None`、`list`、`dict`）。

**额外语义检查（在 precondition 通过后执行）：**

1. 若 `field == "role"`：验证 `change.new in ALLOWED_ROLES`
2. 若 `field == "level"` 且 block kind == "heading"：验证 `change.new in _VALID_HEADING_LEVELS`（**[R1: S2]**）
3. 若 `field == "level"` 且 target 是 chapter：验证 `change.new in _VALID_CHAPTER_LEVELS`（**[R1: T2]**）
4. 若 `field == "paired"` 且 block kind == "footnote"：`change.new` 必须为 bool
5. 若 `field == "orphan"` 且 block kind == "footnote"：`change.new` 必须为 bool
6. footnote 互斥检查：若此 change 将 `paired` 设为 True，检查 working 中同一 footnote 的 `orphan` 字段（包括同一 patch 中先前的修改）是否为 False；反之亦然。

**ReplaceNodeChange precondition：**

```python
current_node = _get_node(working, change.target_uid, index)
current_serialized = current_node.model_dump(mode="python")
if current_serialized != change.old_node:
    raise PatchError(
        f"replace_node old_node precondition mismatch for {change.target_uid}",
        patch_id,
    )
```

**InsertNodeChange precondition：**

若 `after_uid` 非 None：在当前 working 中的对应容器（chapter 的 blocks 或 book.chapters）查找，不存在则拒绝（注意 index 已反映前序 changes，使用 index 查找）。

**DeleteNodeChange precondition：**

```python
current_node = _get_node(working, change.target_uid, index)
current_serialized = current_node.model_dump(mode="python")
if current_serialized != change.old_node:
    raise PatchError(
        f"delete_node old_node precondition mismatch for {change.target_uid}",
        patch_id,
    )
# If target is a chapter, check blocks are empty
if change.target_uid in index.chapter_index:
    ch_idx = index.chapter_index[change.target_uid]
    if working.chapters[ch_idx].blocks:
        raise PatchError(
            f"cannot delete non-empty chapter {change.target_uid} "
            "(delete all blocks first)",
            patch_id,
        )
```

**MoveNodeChange precondition：**

确认节点当前 parent（从 index 查询）与 `change.from_parent_uid` 一致；`after_uid` 非 None 时在目标容器中查找。

### 4.6 索引重建策略

**[R1: S5 addressed]** 每次 `_apply_change` 执行后，按以下策略决定是否重建索引：

- **`SetFieldChange`**：不重建索引。字段修改不改变任何节点的位置，index 保持有效。
- **`InsertNodeChange`**：完整重建索引。新节点需要进入索引，且插入位置会使后续节点的 `b_idx` 全部偏移。
- **`DeleteNodeChange`**：完整重建索引。删除后被删 uid 需要从索引移除，且后续节点的 `b_idx` 全部偏移。
- **`ReplaceNodeChange`**：不重建索引（uid 保持不变，位置不变；kind 变化但 index 不按 kind 索引）。
- **`MoveNodeChange`**：完整重建索引。源容器和目标容器的 `b_idx` 均发生变化。

**推荐实现**：Phase 1 使用完整重建（调用 `_build_index(working)` 替换旧 index），性能可接受。后续 Phase 如有需要，可优化为增量更新。

```python
# After each structural change
if isinstance(change, (InsertNodeChange, DeleteNodeChange, MoveNodeChange)):
    index = _build_index(working)
```

### 4.7 各 Change 类型的 Apply 行为

#### SetFieldChange Apply

```python
def _apply_set_field(
    working: Book, change: SetFieldChange, index: _BookIndex, *, patch_id: str
) -> None:
    if change.target_uid in index.block_index:
        ref = index.block_index[change.target_uid]
        block = working.chapters[ref[0]].blocks[ref[1]]
        working.chapters[ref[0]].blocks[ref[1]] = block.model_copy(
            update={change.field: change.new}
        )
    elif change.target_uid in index.chapter_index:
        ch_idx = index.chapter_index[change.target_uid]
        chapter = working.chapters[ch_idx]
        working.chapters[ch_idx] = chapter.model_copy(
            update={change.field: change.new}
        )
    else:
        raise PatchError(f"target_uid {change.target_uid!r} not found", patch_id)
```

`model_copy(update=...)` 是 Pydantic v2 的不可变更新方式，返回新对象，赋值回对应位置。

#### ReplaceNodeChange Apply

```python
def _apply_replace_node(
    working: Book, change: ReplaceNodeChange, index: _BookIndex, *, patch_id: str
) -> None:
    node_data = dict(change.new_node)
    node_data["uid"] = change.target_uid       # Inject original uid
    kind = node_data["kind"]
    payload = {k: v for k, v in node_data.items() if k not in ("uid", "kind")}
    new_block = _make_block(kind, change.target_uid, payload)
    ref = index.block_index[change.target_uid]
    working.chapters[ref[0]].blocks[ref[1]] = new_block
```

注意：按 §2.4.2 的约定，`ReplaceNodeChange` 仅用于 block，不用于 chapter。

#### InsertNodeChange Apply

```python
def _apply_insert_node(
    working: Book, change: InsertNodeChange, index: _BookIndex, *, patch_id: str
) -> None:
    if change.parent_uid is not None:
        ch_idx = index.chapter_index[change.parent_uid]
        chapter = working.chapters[ch_idx]
        node_data = dict(change.node)
        new_block = _make_block(
            node_data["kind"], node_data["uid"],
            {k: v for k, v in node_data.items() if k not in ("uid", "kind")}
        )
        if change.after_uid is None:
            insert_at = 0
        else:
            insert_at = _block_pos_in_chapter(chapter, change.after_uid, patch_id) + 1
        chapter.blocks = chapter.blocks[:insert_at] + [new_block] + chapter.blocks[insert_at:]
    else:
        # Insert chapter into book.chapters
        node_data = dict(change.node)
        new_chapter = Chapter.model_validate(node_data)
        if change.after_uid is None:
            insert_at = 0
        else:
            if change.after_uid not in index.chapter_index:
                raise PatchError(
                    f"insert_node after_uid {change.after_uid!r} not found in book.chapters",
                    patch_id,
                )
            insert_at = index.chapter_index[change.after_uid] + 1
        working.chapters = working.chapters[:insert_at] + [new_chapter] + working.chapters[insert_at:]
```

#### DeleteNodeChange Apply

```python
def _apply_delete_node(
    working: Book, change: DeleteNodeChange, index: _BookIndex, *, patch_id: str
) -> None:
    if change.target_uid in index.block_index:
        ref = index.block_index[change.target_uid]
        chapter = working.chapters[ref[0]]
        chapter.blocks = [b for i, b in enumerate(chapter.blocks) if i != ref[1]]
    elif change.target_uid in index.chapter_index:
        ch_idx = index.chapter_index[change.target_uid]
        working.chapters = [c for i, c in enumerate(working.chapters) if i != ch_idx]
    else:
        raise PatchError(
            f"delete_node target_uid {change.target_uid!r} not found at apply time",
            patch_id,
        )
```

#### MoveNodeChange Apply

**[R1: D5 addressed]** 完整实现，包含 chapter 级别移动逻辑：

```python
def _apply_move_node(
    working: Book, change: MoveNodeChange, index: _BookIndex, *, patch_id: str
) -> None:
    if change.from_parent_uid is None and change.to_parent_uid is None:
        # Moving a chapter within book.chapters
        ch_idx = index.chapter_index[change.target_uid]
        chapter = working.chapters[ch_idx]
        working.chapters = [c for i, c in enumerate(working.chapters) if i != ch_idx]
        if change.after_uid is None:
            insert_at = 0
        else:
            # Recompute position after removal
            new_positions = {c.uid: i for i, c in enumerate(working.chapters)}
            if change.after_uid not in new_positions:
                raise PatchError(
                    f"move_node after_uid {change.after_uid!r} not found in book.chapters",
                    patch_id,
                )
            insert_at = new_positions[change.after_uid] + 1
        working.chapters = (
            working.chapters[:insert_at] + [chapter] + working.chapters[insert_at:]
        )
    else:
        # Moving a block between chapters (or within the same chapter)
        ref = index.block_index[change.target_uid]
        block = working.chapters[ref[0]].blocks[ref[1]]
        # Remove from source
        working.chapters[ref[0]].blocks = [
            b for i, b in enumerate(working.chapters[ref[0]].blocks) if i != ref[1]
        ]
        # Determine target chapter
        if change.to_parent_uid is None:
            raise PatchError(
                "move_node to_parent_uid=None is only valid when from_parent_uid=None "
                "(chapter-level move)",
                patch_id,
            )
        target_ch_idx = index.chapter_index[change.to_parent_uid]
        chapter = working.chapters[target_ch_idx]
        if change.after_uid is None:
            insert_at = 0
        else:
            insert_at = _block_pos_in_chapter(chapter, change.after_uid, patch_id) + 1
        chapter.blocks = chapter.blocks[:insert_at] + [block] + chapter.blocks[insert_at:]
```

### 4.9 Footnote 不变量检查<!-- [R2: removed base_version/op_log_version]，原 §4.8 版本递增已删除，§4.9 上移 -->

针对 footnote 的特殊规则，在增量 precondition 检查中（§4.5）已逐步检查 paired/orphan 互斥。作为额外的防线，apply 完成后对整个 working 执行最终的 footnote 不变量扫描：

```python
def _check_footnote_invariants(book: Book, patch_id: str) -> None:
    for chapter in book.chapters:
        for block in chapter.blocks:
            if block.kind != "footnote":
                continue
            if block.paired and block.orphan:
                raise PatchError(
                    f"footnote {block.uid} cannot be both paired and orphan",
                    patch_id,
                )
```

在 `apply_book_patch` 的步骤 5 完成后调用。<!-- [R2: removed base_version/op_log_version] -->

### 4.10 错误处理

Apply 中的任何运行时错误均包装为 `PatchError`。由于 precondition 检查是增量进行的（每个 change 执行前），apply 阶段理论上不应出现额外的逻辑错误；但保留运行时 `PatchError` 作为防御（如 target_uid 在索引中不存在时的兜底）。

---

## 5. 内部辅助函数概要

以下辅助函数在 `patches.py` 内部使用，不对外暴露：

```python
# Build UID index from book (used at apply entry and after each structural change)
def _build_index(book: Book) -> _BookIndex: ...

# _BookIndex is a simple dataclass wrapping the two dicts
@dataclasses.dataclass
class _BookIndex:
    block_index: dict[str, tuple[int, int]]   # uid -> (ch_idx, b_idx)
    chapter_index: dict[str, int]             # uid -> ch_idx

# Get a node (Block or Chapter) by uid from working book + index
def _get_node(book: Book, uid: str, index: _BookIndex) -> Block | Chapter: ...

# Get current field value from a node (returns raw Python value, not serialized)
def _get_node_field_value(book: Book, uid: str, field: str, index: _BookIndex) -> Any: ...

# Serialize a field value for precondition comparison
# Uses model_dump(mode="json") for Pydantic model instances,
# returns the value directly for JSON-compatible primitives.
def _serialize_field_value(value: Any) -> Any: ...

# Find the index of a block with given uid within a chapter's blocks list
def _block_pos_in_chapter(chapter: Chapter, uid: str, patch_id: str) -> int: ...

# Construct a Block instance from kind + uid + payload dict
# This is a new helper written in patches.py (NOT imported from apply.py —
# apply.py does not have a _make_block function; it constructs blocks inline).
def _make_block(kind: str, uid: str, data: dict[str, Any]) -> Block: ...
```

**[R1: D7 addressed]** `_make_block` 是 Phase 1 新写的辅助函数，不从 `apply.py` 引入（`apply.py` 中并无对应的独立 `_make_block` 函数，它直接调用 `Paragraph(...)`、`Heading(...)` 等构造器）。`_make_block` 的实现逻辑：根据 `kind` 查找对应的 IR Block 类（`Paragraph`、`Heading` 等），将 `uid` 注入 `data` 后调用 `Model.model_validate(data)`。

---

## 6. 与现有代码的关系

### 6.1 完全独立，不修改现有代码

Phase 1 引入的 `patches.py` 完全独立。现有 `ops.py`、`apply.py` 等文件不受影响。两套系统并存直到 Phase 4。

### 6.2 复用的现有 utilities

- `StrictModel`：直接从 `epubforge.editor._validators` 引入
- `require_non_empty`、`validate_uuid4`、`validate_utc_iso_timestamp`：同上
- `ALLOWED_ROLES`：从 `epubforge.ir.style_registry` 引入
- Block 类型 (`Paragraph`, `Heading`, `Footnote`, `Figure`, `Table`, `Equation`, `Chapter`, `Book`)：从 `epubforge.ir.semantic` 引入
- `BLOCK_PAYLOAD_MODELS`：从 `epubforge.editor.ops` 引入，用于 InsertNodeChange / ReplaceNodeChange 的 Payload 校验

### 6.3 不引入的现有模块

- `leases.py`：Phase 1 不做并发控制（D1 已决定用 Git worktree 替代 lease）
- `memory.py`：Phase 1 的 BookPatch 不含 memory_patches（Phase 2 处理）
- `apply.py` 中的 `apply_envelope`、`apply_log` 等：Phase 1 不依赖

### 6.4 推迟到后续 Phase 的功能

- **V2**：provenance 语义合法性验证（如 page 范围检查）——Phase 1 仅通过 Pydantic 结构校验保证字段存在，语义合法性推迟到 Phase 2 或更晚。
- **V3**：table HTML 合法性检查（如是否包含 `<table>` 标签）——Phase 1 有意推迟，仅检查 `html` 字段非空（由 `TablePayload` 的 field_validator 保证）。完整 HTML 合法性验证在 Phase 3 或专用 LintCommand 中处理。
- **V4**：按 agent_id 的字段修改权限控制——Phase 2 的 AgentOutput 层面处理，Phase 1 中 out-of-scope。

---

## 7. 从现有 EditOp 到 IRChange 的映射

以下映射证明 BookPatch 的表达能力覆盖所有现有 EditOp 语义。这是 Phase 4（删除旧系统）的前提。

### 7.1 字段设置类 Op

| EditOp | 对应 IRChange |
|---|---|
| `SetRole(block_uid, value)` | `SetFieldChange(target_uid=block_uid, field="role", old=current_role, new=value)` |
| `SetStyleClass(block_uid, value)` | `SetFieldChange(target_uid=block_uid, field="style_class", old=current_style_class, new=value)` |
| `SetText(block_uid, field, value)` | `SetFieldChange(target_uid=block_uid, field=field, old=current_field_value, new=value)` |
| `SetHeadingLevel(block_uid, value)` | `SetFieldChange(target_uid=block_uid, field="level", old=current_level, new=value)` |
| `SetHeadingId(block_uid, value)` | `SetFieldChange(target_uid=block_uid, field="id", old=current_id, new=value)` |
| `SetFootnoteFlag(block_uid, paired, orphan)` | 最多两个 `SetFieldChange`（分别对 `paired` 和 `orphan` 字段） |
| `SetParagraphCrossPage(block_uid, value)` | `SetFieldChange(target_uid=block_uid, field="cross_page", old=current_cross_page, new=value)` |

备注：`SetText` 原来支持 `field in ("text", "table_title", "caption", "callout", "html")`，`SetFieldChange` 统一处理，`old` 字段由 patch 提交方在生成时填入当前值。

### 7.2 结构操作类 Op

| EditOp | 对应 IRChange 组合 |
|---|---|
| `InsertBlock(chapter_uid, after_uid, block_kind, new_block_uid, block_data)` | `InsertNodeChange(parent_uid=chapter_uid, after_uid=after_uid, node={uid: new_block_uid, kind: block_kind, ...block_data})` |
| `DeleteBlock(block_uid)` | `DeleteNodeChange(target_uid=block_uid, old_node=current_block_snapshot)` |
| `ReplaceBlock(block_uid, block_kind, block_data, new_block_uid, original_block)` | 若 kind 不变且无 uid 变更：`ReplaceNodeChange(target_uid=block_uid, old_node=original_block, new_node={kind: block_kind, ...block_data})`；若 uid 变更：`DeleteNodeChange` + `InsertNodeChange` |
| `RelocateBlock(block_uid, target_chapter_uid, after_uid)` | `MoveNodeChange(target_uid=block_uid, from_parent_uid=current_chapter_uid, to_parent_uid=target_chapter_uid, after_uid=after_uid)` |

### 7.3 MergeBlocks / SplitBlock

这两个 Op 在 Phase 1 没有直接的原子 IRChange 对应——它们是高层 macro，在 Phase 3 被定义为 `PatchCommand`，编译为多个 IRChange 的组合。

| EditOp | Phase 1 能力 | Phase 3 补全 |
|---|---|---|
| `MergeBlocks(block_uids, join)` | 可用 `SetFieldChange` 更新 text 字段 + `DeleteNodeChange` 删除多余块来手动实现，但繁琐 | Phase 3 定义 `merge_blocks` PatchCommand 编译为 [SetFieldChange(text合并), DeleteNodeChange×N] |
| `SplitBlock(block_uid, strategy, ...)` | 可用 `SetFieldChange` 更新原块 text + `InsertNodeChange` 插入新块来手动实现 | Phase 3 定义 `split_block` PatchCommand |

**Phase 1 完整性**：Phase 1 的 BookPatch 已经具备表达上述操作所需的原语（`SetFieldChange`、`InsertNodeChange`、`DeleteNodeChange`），只是缺少更便捷的宏语法。

### 7.4 Chapter 操作类 Op

| EditOp | 对应 IRChange 组合 |
|---|---|
| `SplitChapter(chapter_uid, split_at_block_uid, new_chapter_title, new_chapter_uid)` | 需要：`InsertNodeChange(parent_uid=None, node={uid: new_chapter_uid, title: new_chapter_title, level: ..., blocks: []})` + N 个 `MoveNodeChange`（移动 split point 之后的所有 blocks 到新 chapter）；Phase 3 定义 `split_chapter` PatchCommand |
| `MergeChapters(source_chapter_uids, new_title, ...)` | 需要多步 MoveNodeChange + DeleteNodeChange + InsertNodeChange；Phase 3 定义 `merge_chapters` PatchCommand |

**说明**：Phase 1 的 IRChange 原语足以表达章节分割和合并，但生成 changes 列表较复杂。Phase 3 会把这些封装成 PatchCommand macro。

### 7.5 FootnoteOp

| FootnoteOp 子类型 | 对应 IRChange 组合 |
|---|---|
| `pair_footnote` | `SetFieldChange(fn_block_uid, "paired", old=False, new=True)` + `SetFieldChange(source_block_uid, "text", old=..., new=text_with_marker)` |
| `unpair_footnote` | `SetFieldChange(fn_block_uid, "paired", old=True, new=False)` + `SetFieldChange(source_block_uid, "text", old=text_with_marker, new=text_without_marker)` |
| `relink_footnote` | 从旧 source 移除 marker + 向新 source 添加 marker = 两个 `SetFieldChange` |
| `mark_orphan` | `SetFieldChange(fn_block_uid, "orphan", old=False, new=True)` + 可选从 source 移除 marker |

**说明**：footnote pairing 的 marker 嵌入逻辑（`make_fn_marker`、`replace_nth_raw` 等）在 Phase 1 的 `SetFieldChange` 中体现为对 text 字段的完整替换（old 是替换前的 text，new 是嵌入 marker 后的 text）。具体 marker 格式计算仍由 Phase 3 的 `pair_footnote` PatchCommand macro 处理。

### 7.6 SetTableMetadata / SplitMergedTable

| EditOp | 对应 IRChange 组合 |
|---|---|
| `SetTableMetadata(...)` | 多个 `SetFieldChange`（分别对 `table_title`、`caption`、`continuation`、`multi_page`、`bbox` 等字段），但注意 `merge_record` 字段在 Phase 1 阶段通过 `ReplaceNodeChange` 整体替换，或在 Phase 3 中用专用 PatchCommand 处理 |
| `SplitMergedTable(...)` | `DeleteNodeChange`(merged table) + N 个 `InsertNodeChange`(各 segment table)；Phase 3 定义 `split_merged_table` PatchCommand |

### 7.7 System Op（NoopOp / CompactMarker / RevertOp）

这三类在新系统中均不在 BookPatch 中表达：
- `NoopOp`：新系统中无对应（milestone 语义改由 Git commit 承载）
- `CompactMarker`：新系统中无对应（op log 概念已被 Git 替代）
- `RevertOp`：新系统中通过 `apply_book_patch(base_book, revert_patch)` 实现（patch 本身就是逆向 changes）

---

## 8. 测试计划

测试文件：`tests/editor/test_patches.py`

所有测试使用简单的 fixture book（2 chapters，每 chapter 3-4 blocks，涵盖 paragraph / heading / footnote / table）。所有 fixture book 中的节点均有非 None uid（通过 `compute_block_uid_init` 生成）。

### 8.1 SetFieldChange 测试

| 测试用例 | 预期结果 |
|---|---|
| 修改 paragraph.text（正常路径） | 成功，版本 +1，text 更新 |
| 修改 heading.level（1→2） | 成功 |
| 修改 heading.level（1→5，非法值）[R1: S2] | PatchError：new value 不合法（_VALID_HEADING_LEVELS 检查） |
| 修改 chapter.level（1→4，非法值）[R1: T2] | PatchError：new value 不合法（_VALID_CHAPTER_LEVELS 检查） |
| 修改 footnote.paired（False→True） | 成功 |
| 同时修改同一 footnote 的 paired=True 和 orphan=True | PatchError：互斥字段 |
| 修改 paragraph.role 为合法值 | 成功 |
| 修改 paragraph.role 为非法值 | PatchError：role not in ALLOWED_ROLES |
| `old` 值与当前值不匹配 | PatchError：precondition mismatch |
| `old == new`（精确相同值） | Pydantic 模型层 ValueError |
| 修改 block.uid（禁止字段） | PatchError：field is immutable |
| 修改 block.kind（禁止字段） | PatchError：field is immutable |
| 修改 paragraph.level（不存在字段） | PatchError：field not editable for kind paragraph |
| target_uid 不存在 | PatchError：target_uid not found |
| 修改 chapter.title | 成功 |
| 修改 table.html | 成功 |
| 修改 table.bbox [R1: D3] | 成功（table.bbox 已加入 _ALLOWED_SET_FIELD） |
| `target_uid` 为纯空白字符串 [R1: T8] | Pydantic 模型层 ValueError（require_non_empty） |
| `field` 为纯空白字符串 [R1: T8] | Pydantic 模型层 ValueError（require_non_empty） |

### 8.2 ReplaceNodeChange 测试

| 测试用例 | 预期结果 |
|---|---|
| 将 paragraph 替换为内容更新的同类型 paragraph | 成功，uid 保留 |
| 将 paragraph 替换为 heading（kind 变更） | 成功，uid 保留，kind 更新 |
| `old_node` 不匹配当前状态 [R1: S4] | PatchError：old_node precondition mismatch（model_dump(mode="python") 比较） |
| `new_node` 包含 uid 键 | Pydantic 模型层 ValueError |
| `new_node` 缺少 kind 键 | Pydantic 模型层 ValueError |
| `new_node` 的 kind 合法但 level=5（heading）[R1: S2] | PatchError：new_node Payload 校验失败（HeadingPayload.level Literal[1,2,3]） |
| target_uid 不存在 | PatchError：target_uid not found |

### 8.3 InsertNodeChange 测试

| 测试用例 | 预期结果 |
|---|---|
| 在 chapter 末尾插入新 block（after_uid=last_block） | 成功，block 出现在末尾 |
| 在 chapter 开头插入新 block（after_uid=None） | 成功，block 出现在第一位 |
| 在 chapter 中间插入新 block | 成功，位置正确 |
| 插入新 chapter（parent_uid=None，含 title，blocks 为空） [R1: T5] | 成功 |
| 插入新 chapter（parent_uid=None，含 blocks）[R1: T5] | 成功，blocks 一并写入 |
| `node["uid"]` 与已有 block uid 冲突 | PatchError：uid collision |
| `parent_uid` 不存在 | PatchError：parent_uid chapter not found |
| `after_uid` 不属于 parent_uid chapter | PatchError：after_uid not found in parent |
| `node` 缺少 uid | Pydantic 模型层 ValueError |
| `node` 缺少 kind | Pydantic 模型层 ValueError |
| footnote node 的 paired=True 且 orphan=True | PatchError（FootnotePayload 校验捕捉） |

### 8.4 DeleteNodeChange 测试

| 测试用例 | 预期结果 |
|---|---|
| 删除存在的 block | 成功，block 从章节移除 |
| 删除空 chapter | 成功 |
| 删除非空 chapter（未预先删除 blocks） | PatchError：cannot delete non-empty chapter |
| `old_node` 不匹配当前状态 [R1: S4] | PatchError：old_node precondition mismatch |
| target_uid 不存在 | PatchError：target_uid not found |

### 8.5 MoveNodeChange 测试

| 测试用例 | 预期结果 |
|---|---|
| 章内移动 block（改变顺序） | 成功，顺序更新 |
| 跨章节移动 block | 成功，block 从源章删除，出现在目标章 |
| 移动到目标章的末尾（after_uid=None） | 成功 |
| 移动到目标章的特定位置 | 成功，位置正确 |
| 章内移动到当前位置（no-op move）[R1: T6] | 成功，apply 后 book 内容不变，版本 +1 |
| chapter 在 book 内移动（from_parent_uid=None, to_parent_uid=None）[R1: D5] | 成功，chapter 顺序更新 |
| `from_parent_uid` 不匹配当前实际父章节 | PatchError：from_parent_uid mismatch |
| `to_parent_uid` 不存在 | PatchError：to_parent_uid chapter not found |
| `after_uid == target_uid` | Pydantic 模型层 ValueError |
| `after_uid` 不属于 `to_parent_uid` | PatchError：after_uid not found in target parent |
| target_uid 不存在 | PatchError：target_uid not found |

### 8.6 PatchScope 测试

| 测试用例 | 预期结果 |
|---|---|
| scope.chapter_uid 为 ch-1，change 操作 ch-2 的 block | PatchError：change target out of scope |
| scope.chapter_uid 为 ch-1，跨章节 move_node | PatchError：change target out of scope |
| scope.chapter_uid=None，跨章节 move_node | 成功 |

### 8.7 BookPatch 模型层测试

| 测试用例 | 预期结果 |
|---|---|
| `patch_id` 非法（不是 UUID4） | Pydantic 模型层 ValueError |
| `agent_id` 为空字符串 | Pydantic 模型层 ValueError |
| `agent_id` 为纯空白字符串 [R1: T8] | Pydantic 模型层 ValueError（require_non_empty 使用 .strip()） |
| `rationale` 为空字符串 | Pydantic 模型层 ValueError |
| `rationale` 为纯空白字符串 [R1: T8] | Pydantic 模型层 ValueError |
| `changes` 为空列表 | Pydantic 模型层 ValueError（min_length=1） |
<!-- [R2: removed base_version/op_log_version]：删除 `base_version < 0` 测试行及原 §8.8 base_version 整节 -->

### 8.8 Apply 结果验证测试<!-- 原 §8.9 重编号为 §8.8 -->

| 测试用例 | 预期结果 |
|---|---|
| apply 后原 book 对象不被修改（不可变语义） | 通过 |
| apply 失败（PatchError），原 book 不变 | 通过 |
<!-- [R2: removed base_version/op_log_version]：删除 op_log_version 递增测试行 -->

### 8.9 多 Change 组合测试<!-- [R2: removed base_version/op_log_version]，原 §8.10 重编号为 §8.9 -->

| 测试用例 | 预期结果 |
|---|---|
| 同一 patch 中先 InsertNodeChange 后 SetFieldChange 对新节点 | 成功（增量 precondition 检查支持 order dependency） |
| 同一 patch 中两个 InsertNodeChange 使用相同 uid | PatchError：uid collision（validate_book_patch 阶段捕捉） |
| 同一 patch 中先 DeleteNodeChange 再对该 uid 做 SetFieldChange | PatchError（delete 后 index 重建，uid 不再存在） |
| **对同一节点的链式 SetFieldChange**（T4）[R1: T4] | 见下方详细说明 |

**[R1: T4 addressed]** 链式 SetFieldChange 测试：

```python
# Patch: first change text "a" -> "b", then change text "b" -> "c"
# This is valid because preconditions are checked incrementally
changes = [
    SetFieldChange(op="set_field", target_uid=block_uid, field="text", old="a", new="b"),
    SetFieldChange(op="set_field", target_uid=block_uid, field="text", old="b", new="c"),
]
# Expected: success, final text == "c"
```

测试应验证：
- 上述 patch 成功 apply，最终 text 为 `"c"`。
- 若将两个 change 的 `old` 都设为 `"a"`（不链式，模拟基于原始状态的两次独立修改），第二个 change 在增量检查时失败，PatchError。

### 8.10 uid=None 守卫测试<!-- 原 §8.11 重编号为 §8.10 -->

**[R1: S1 addressed / T1 addressed]**

| 测试用例 | 预期结果 |
|---|---|
| book 中存在 chapter.uid=None | PatchError：book contains nodes with uid=None |
| book 中存在 block.uid=None（某 chapter 内） | PatchError：block has uid=None |
| book 中所有 uid 均非 None | validate_book_patch 通过此步骤 |
| `validate_book_patch` 在 uid=None 时也触发此错误 | PatchError（静态预检包含此步骤） |

### 8.11 old/new 复杂类型比较测试<!-- 原 §8.12 重编号为 §8.11 -->

**[R1: T7 addressed / S3 addressed]**

| 测试用例 | 预期结果 |
|---|---|
| `old=None, new=None` | Pydantic 模型层 ValueError（old == new） |
| `old=[1.0, 2.0], new=[1.0, 2.0]` | Pydantic 模型层 ValueError（old == new） |
| `old={"a": 1}, new={"a": 1}` | Pydantic 模型层 ValueError（old == new） |
| precondition 比较：当前字段是 Pydantic 模型（如 `Provenance`），old 是 JSON dict | 使用 `model_dump(mode="json")` 序列化后比较，匹配则通过，不匹配则 PatchError |
| precondition 比较：当前字段是 `list[float]`（如 bbox），old 是 `[1.0, 2.0]` | 直接 `==` 比较，匹配则通过 |

### 8.12 Book IR 不变量测试<!-- 原 §8.13 重编号为 §8.12 -->

| 测试用例 | 预期结果 |
|---|---|
| 将 footnote 的 paired 和 orphan 同时设为 True | PatchError（增量 precondition 检查的 footnote 互斥校验捕捉） |
| heading level 通过 set_field 设为非法值（5）[R1: S2] | PatchError（_VALID_HEADING_LEVELS 检查） |
| heading level 通过 replace_node 新节点设为非法值（5）[R1: S2] | PatchError（HeadingPayload Payload 校验，Literal[1,2,3]） |

### 8.13 ReplaceNodeChange 与 Chapter 相关测试<!-- 原 §8.14 重编号为 §8.13 -->

**[R1: T3 addressed]** 按 §2.4.2 的修订，`ReplaceNodeChange` 不用于 chapter 替换。因此：

| 测试用例 | 预期结果 |
|---|---|
| 用 ReplaceNodeChange 的 `target_uid` 指向 chapter uid | PatchError：replace_node target must be a block, not a chapter |
| chapter metadata 修改通过 SetFieldChange | 成功（`title`、`level`、`id` 均可 set_field） |

---

## 9. 后续 Phase 的衔接提示

- **Phase 2**：AgentOutput 的 `patches: list[BookPatch]` 字段直接使用 Phase 1 的 `BookPatch` 模型；`validate` 命令内部调用 `validate_book_patch`（静态预检）；`submit --apply` 调用 `apply_book_patch`（事务性执行）。
- **Phase 3**：每个 `PatchCommand` macro 的 `compile()` 方法返回 `BookPatch`；编译后立即调用 `apply_book_patch` 确认语义。batch change（如 `move_block_range`、`split_chapter`、`merge_chapters`）均走 macro 路线，不扩展 IRChange union（**[R1: V1]**）。
- **Phase 4**：删除旧 `ops.py` / `apply.py` 时，确认 `patches.py` 的 `_make_block` 是唯一 block 构造入口。uid=None 的 IR 字段在此阶段可考虑改为 `str`（必填），消除 BookPatch 的 uid 守卫前置条件（**[R1: S1]** 的根本修复）。
- **Phase 6**：`diff_books(base, proposed)` 的返回值是 `BookPatch`；`apply_book_patch(base, diff_books(base, proposed)) == proposed` 是 round-trip 不变量。注意 diff_books 生成的 BookPatch 中，old/new 值通过 `model_dump(mode="json")` 序列化，与 precondition 比较约定一致（**[R1: S3]**）。
