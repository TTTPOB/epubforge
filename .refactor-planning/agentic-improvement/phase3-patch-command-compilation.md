# Phase 3 实施计划：PatchCommand 到 BookPatch 编译

> 状态：评审完成，已纳入修订  
> 范围：仅规划 Phase 3，不实现代码  
> 依赖：Phase 1 BookPatch 原子层；Phase 2 AgentOutput 模型与 CLI 命令组

---

## 1. Phase 3 目标

Phase 3 要把 Phase 2 中暂存于 `AgentOutput.commands` 的高层 `PatchCommand` 变成可验证、可提交、可应用的 `BookPatch`。

本阶段只实现新 `AgentOutput.commands` 的**最小可用 macro compiler**。不以覆盖既有 `EditOp` surface 为目标，不做兼容层，不迁移既有操作语义；既有系统的删除与替换仍属于后续 Phase。

当前状态是：

- Phase 1 已提供 `BookPatch`、`PatchScope`、五种 `IRChange`，以及 `validate_book_patch(book, patch)` / `apply_book_patch(book, patch)`。相关计划见 `phase1-bookpatch.md`，核心约束见该文 §2.3、§2.4、§4。
- Phase 2 已提供 `AgentOutput.commands: list[PatchCommand]` 和 `agent-output add-command`，但 `PatchCommand` 仍是宽松占位模型；`validate_agent_output` 目前会把任意 command 判为 “compilation is not implemented”。相关计划见 `phase2-agent-output.md` §2.5、§5.4、§6、§10。
- 总体设计要求 `PatchCommand -> BookPatch -> validate/apply`，并把 `PatchCommand` 定位为高层宏，而不是另一套 apply 系统。相关设计见 `agentic-improvement.md` §6、§7、§Implementation Phases / Phase 3。

Phase 3 完成后，agent 可以通过 `agent-output add-command` 提交高层意图，`agent-output validate` 能编译并验证这些命令，`agent-output submit --apply` 能把编译产物和直接提交的 `BookPatch` 一起按确定顺序应用。

---

## 2. 范围

### 2.1 范围内

Phase 3 只处理 `PatchCommand` 编译和 AgentOutput 集成：

- 收紧 `src/epubforge/editor/patch_commands.py` 中的 `PatchCommand` schema。
- 定义每个 high-level command 的参数模型、语义规则、权限规则和编译函数。
- 将 `validate_agent_output` 中的 command 占位错误替换为真实编译与验证。
- 将 `submit_agent_output` 中的 `_compile_commands` no-op stub 替换为真实编译。
- 增加覆盖 command 编译、validate、submit 的测试。
- 保持 `AgentOutput` 存储 JSON 外形不变：仍然是 `{"command_id", "op", "agent_id", "rationale", "params"}`。

### 2.2 范围外

Phase 3 不做以下事项：

- 不删除 `EditOp` / `OpEnvelope` / `apply.py`，这些是 Phase 4 范围。
- 不实现 `--stage` 的新 staging 格式。Phase 2 已明确 `--stage` 是占位，正式 staging 等 Phase 4。
- 不实现 projection export、Book diff、Git worktree workflow 或 VLM evidence system。这些分别属于后续 Phase。
- 不改变 `BookPatch` 的五种原子 change 集合。Phase 1 明确 batch change 通过 Phase 3 macro 编译，不扩展 `IRChange` union。
- 不引入历史操作到 `BookPatch` 的转换层，不迁移既有 `EditOp` surface。

---

## 3. 前置条件

实现 Phase 3 前应确认：

1. Phase 1 测试通过，尤其是 `tests/editor/test_patches.py` 中 `apply_book_patch` 的事务性、scope、precondition 和拓扑操作测试。
2. Phase 2 测试通过，尤其是 `tests/test_agent_output.py` 中 `add-command`、`validate`、`submit` 的当前占位行为。
3. `AgentOutput` 仍保持 Phase 2 的文件布局：`edit_state/agent_outputs/<output_id>.json` 和 `archives/`。
4. 所有要编译的命令必须能展开为 Phase 1 已存在的五种 change：`set_field`、`replace_node`、`insert_node`、`delete_node`、`move_node`。
5. `Chapter`（`ir/semantic.py`）已增加 `kind: Literal["chapter"] = "chapter"` 字段。这是 Phase 3 的前置 IR 变更，确保 `InsertNodeChange.node` 的 `kind` 不被 `Chapter.model_validate` 静默丢弃。旧 JSON 无 `kind` 字段时使用默认值，向后兼容。

---

## 4. 设计约束与衔接

### 4.1 来自 Phase 1 的约束

| 约束 | Phase 3 影响 | 来源 |
|---|---|---|
| `PatchScope(chapter_uid=None)` 表示 book-wide | 任何跨章节或 chapter topology command 必须编译为 book-wide patch | `phase1-bookpatch.md` §2.3 |
| `SetFieldChange.old` 使用 JSON-compatible 当前值作为 precondition | 编译命令时必须从当前 Book 读取旧值并序列化 | `phase1-bookpatch.md` §2.4.1 |
| `ReplaceNodeChange.new_node` 不包含 uid，apply 时保留 target uid | Phase 3 macro 如需保留原 uid 的整体替换可用 `replace_node`；本阶段 9 个 macro 中暂无这类命令 | `phase1-bookpatch.md` §2.4.2 |
| `InsertNodeChange` 可插入 block 或 chapter | `split_block`、`split_chapter`、`merge_chapters` 的新增节点都走 insert_node | `phase1-bookpatch.md` §2.4.3 |
| `DeleteNodeChange` 删除 chapter 前要求 chapter 为空 | `merge_chapters` 必须先移动/删除源 chapter blocks，再删除源 chapter | `phase1-bookpatch.md` §2.4.4 |
| `MoveNodeChange` 可移动 block 或重排 chapter | `relocate_block`、`split_chapter`、`merge_chapters` 优先用 move_node，而不是复制已有 uid 的 node | `phase1-bookpatch.md` §2.4.5 |
| `IRChange` union 保持封闭 | 不新增 `move_block_range` 等 batch change；如需要批量移动，由 compiler 生成多个 `MoveNodeChange` | `phase1-bookpatch.md` §2.5 |

### 4.2 来自 Phase 2 的约束

| 约束 | Phase 3 影响 | 来源 |
|---|---|---|
| `PatchCommand` 当前是占位结构 | Phase 3 可以收紧 `op` 和 `params`，但不改变外层 JSON 字段 | `phase2-agent-output.md` §2.5 |
| `agent-output add-command` 从 JSON 文件读取单个 `PatchCommand` | 新 schema 失败仍应由 add-command 报 `PatchCommand validation failed` | `phase2-agent-output.md` §4.5 |
| `validate_agent_output` 应收集错误，不 fail-fast | command 编译错误要按 `commands[i] (<command_id>): ...` 收集 | `phase2-agent-output.md` §4.8、§5 |
| submit 顺序为 `compiled_patches + output.patches` | Phase 3 不改变这个顺序；命令先编译并应用，再应用直接 patches | `phase2-agent-output.md` §6 |
| scanner/reviewer 权限保守；fixer 直接 BookPatch 不允许 topology | Phase 3 需要明确 command 权限，特别是 fixer 可通过 command 做受限拓扑 | `phase2-agent-output.md` §5.7、§10 |
| `PatchCommand.agent_id` vs `AgentOutput.agent_id` 完全缺失检查 | Phase 2 代码未实现此检查（不是宽松，是缺失）；Phase 3 需从零新增 `command.agent_id == output.agent_id` 的 validation error，同时也应对 `patch.agent_id` 做同样检查 | `phase2-agent-output.md` §10 |

### 4.3 来自总体设计的约束

- 高层命令只是 macro，最终统一落到 `BookPatch`，见 `agentic-improvement.md` §6。
- `BookPatch` validator 仍是 Book 语义正确性的唯一底线，见 `agentic-improvement.md` §7。
- 不保留 lease/lock 作为长期并发模型；Phase 3 只做 scope 和 permission 校验，不引入 lease 续租，见 `agentic-improvement.md` D1。
- 不重新引入 `base_version` / `op_log_version` 依赖，见 `agentic-improvement.md` D6。

---

## 5. 目标命令集

### 5.1 Phase 3 必做命令

这些命令来自总体设计的 Phase 3 列表，应在本阶段完整支持：

| op | 作用 | patch scope |
|---|---|---|
| `split_block` | 将一个 text-bearing block 拆成原 block + 新 block 序列 | 原 block 所在 chapter |
| `merge_blocks` | 将同一 chapter 内多个 text-bearing block 合并到第一个 block，并删除其余 block | 源 blocks 所在 chapter |
| `split_chapter` | 从指定 block 起把 chapter 尾部移动到新 chapter | book-wide |
| `merge_chapters` | 将多个 chapter 合并为一个新 chapter，并在各源章内容前插入 section heading | book-wide |
| `relocate_block` | 移动 block 到目标 chapter 的指定位置 | 同章移动为 chapter scope；跨章为 book-wide |
| `pair_footnote` | 把 source block 中的 raw callout 替换成 marker，并设置 footnote paired | 源 block 与 footnote 同章时为 chapter scope，否则 book-wide |
| `unpair_footnote` | 找到 marker，恢复 raw callout，并设置 footnote unpaired | 同章时为 chapter scope，否则 book-wide |
| `mark_orphan` | 移除现有 marker（如有），恢复 raw callout，并设置 footnote orphan | 同章时为 chapter scope，否则 book-wide |
| `split_merged_table` | 把 `multi_page=True` 的 table 拆回多个 table segment | table 所在 chapter |

### 5.2 范围边界

Phase 3 只实现 §5.1 的 9 个 macro。除此之外的 command、既有操作覆盖、修复入口、迁移入口都不属于本阶段。这个边界是范围决策，不是技术遗漏；Phase 3 的验收只看 9 个 macro 是否能作为 `AgentOutput.commands` 的最小可用编译层运行。

---

## 6. PatchCommand Schema 方案

### 6.1 保持存储外形

保留 Phase 2 的 JSON 外形：

```json
{
  "command_id": "<uuid4>",
  "op": "split_block",
  "agent_id": "fixer-ch1",
  "rationale": "Split paragraph at a visible heading boundary.",
  "params": {}
}
```

这样 `AgentOutput` 文件和 `agent-output add-command` 的使用方式不变。

### 6.2 收紧 op

`PatchCommand.op` 从自由字符串收紧为 `PatchCommandOp`：

```python
PatchCommandOp = Literal[
    "split_block",
    "merge_blocks",
    "split_chapter",
    "merge_chapters",
    "relocate_block",
    "pair_footnote",
    "unpair_footnote",
    "mark_orphan",
    "split_merged_table",
]
```

### 6.3 params 校验

推荐在 `patch_commands.py` 中定义每个 op 对应的 params 模型，再由 `PatchCommand` 的 `model_validator` 根据 `op` 校验并规范化 `params`。

保持 `PatchCommand.params: dict[str, Any]` 的外部类型，避免改动 `AgentOutput` schema；内部提供：

```python
def command_params(command: PatchCommand) -> PatchCommandParams:
    ...
```

这样 compiler 可以获得 typed params，同时 JSON 文件仍保持 Phase 2 形状。

### 6.4 错误类型

新增：

```python
class PatchCommandError(RuntimeError):
    def __init__(self, reason: str, command_id: str) -> None: ...
```

所有 compiler 错误统一携带 `command_id`，由 `validate_agent_output` 包装成 `commands[i] (<command_id>): <reason>`。

---

## 7. 编译 API

### 7.1 推荐公开函数

在 `patch_commands.py` 暴露：

```python
PatchCommandAgentKind = Literal["scanner", "fixer", "reviewer", "supervisor"]

@dataclass(frozen=True)
class CompiledCommands:
    patches: list[BookPatch]
    book_after_commands: Book

def compile_patch_command(
    book: Book,
    command: PatchCommand,
    *,
    output_kind: PatchCommandAgentKind,
    output_chapter_uid: str | None,
) -> BookPatch: ...

def compile_patch_commands(
    book: Book,
    commands: list[PatchCommand],
    *,
    output_kind: PatchCommandAgentKind,
    output_chapter_uid: str | None,
) -> CompiledCommands: ...
```

不要让 `patch_commands.py` 反向 import `AgentKind` from `agent_output.py`，否则 `AgentOutput -> PatchCommand -> AgentOutput` 容易形成循环 import。第一版直接在 `patch_commands.py` 定义本地 `PatchCommandAgentKind` Literal；如果后续还有第三处需要复用，再把 kind Literal 抽到小模块，例如 `editor/agent_kinds.py`。

`compile_patch_commands` 必须维护一个 evolving book：

1. 以当前 `book` 开始。
2. 编译 `commands[0]` 得到 `patch0`。
3. 立即用 `apply_book_patch(current, patch0)` 验证并得到下一版 `current`。
4. 用演化后的 `current` 编译下一个 command。
5. 返回 `CompiledCommands(patches=[...], book_after_commands=current)`。

这样 command 链可以合法依赖前一个 command 生成的新 uid 或新文本，同时任何编译产物都经过 BookPatch 真实 apply 验证。

### 7.2 patch_id 与 rationale

每个 command 编译成一个 `BookPatch`：

- `patch_id`：建议直接使用 `command.command_id`。它已经是 UUID4，可保持 command 与 patch 的一一审计关系。
- `agent_id`：必须等于 `command.agent_id`。
- `rationale`：使用 `command.rationale`。
- `evidence_refs`：Phase 3 可先留空；如果未来 `PatchCommand.params` 携带 evidence refs，再透传。

### 7.3 与 AgentOutput 集成

替换 `agent_output.py` 当前 `_compile_commands` stub：

```python
compiled = compile_patch_commands(
    book,
    output.commands,
    output_kind=output.kind,
    output_chapter_uid=output.chapter_uid,
)
compiled_patches = compiled.patches
book_after_commands = compiled.book_after_commands
```

`validate_agent_output` 的推荐顺序：

1. 校验顶层 `chapter_uid`。
2. 校验 `command.agent_id == output.agent_id`（Phase 2 代码完全缺失此检查，需从零新增）。同时校验 `patch.agent_id == output.agent_id`。
3. 编译 commands，收集错误。
4. 若 commands 编译成功，得到 `CompiledCommands.patches` 和 `CompiledCommands.book_after_commands`。
5. 若 commands 编译失败（任意一个 command 编译或 apply 失败）：将失败 command 及后续 command 的错误记录到 `errors` 中，标记 `book_after_commands = None`，**不再验证 `output.patches`**，并在 errors 中追加 `"skipped output.patches validation because command compilation failed"`。这与 WP2 验收要求一致（"第一条 command 编译失败时，后续 command 不继续编译"）。在 commands 失败时继续验证 patches 只会产生依赖 command 产物的级联噪音错误。
6. 若步骤 4 成功，在 `book_after_commands` 上继续验证 `output.patches`，保持 Phase 2 的 `compiled_patches + output.patches` 顺序。
7. 对 `compiled_patches + output.patches` 统一执行 scope 和 kind permission 检查。
8. 再检查 memory patches、open questions、scanner read_passes。

`submit_agent_output` 的行为：

- 不再二次走独立逻辑；应复用同一套 validation helper。
- 如果 validate 已经失败，返回 `SubmitResult(submitted=False, errors=[...])`，不写 `book.json`、不归档。
- 如果 validate 通过，`all_patches = compiled_patches + output.patches`，按 Phase 2 既定顺序应用。

实现时应避免 “validate 编译一次，submit 又以不同路径编译一次” 导致行为漂移。推荐在 `agent_output.py` 引入唯一内部 helper，并让 `validate_agent_output` 与 `submit_agent_output` 都调用它：

```python
@dataclass(frozen=True)
class AgentOutputValidationResult:
    errors: list[str]
    compiled_patches: list[BookPatch]
    book_after_commands: Book | None

def validate_agent_output_for_submit(output: AgentOutput, book: Book) -> AgentOutputValidationResult:
    ...
```

`validate_agent_output(output, book) -> list[str]` 保持现有外部 API，只返回 `validate_agent_output_for_submit(...).errors`。`submit_agent_output` 使用同一个 result 中的 `compiled_patches`，不得重新走另一套 command 编译逻辑；如果实现上为了获得最新状态必须重新调用，也必须调用同一个 helper，并且 compiler 不得使用随机值。

---

## 8. 权限规则

### 8.1 总规则

- `command.agent_id` 必须等于 `output.agent_id`。`agent_id` 应携带随机 3-4 字符后缀以保证唯一性（如 `fixer-ch1-a3x`）。
- `patch.agent_id` 也必须等于 `output.agent_id`（Phase 2 代码完全缺失此检查，Phase 3 一并补上）。
- `scanner` 和 `reviewer` 在 Phase 3 不允许提交任何 `PatchCommand`。它们可继续使用 notes、open_questions、memory_patches，以及 Phase 2 允许的直接 `set_field` patch。
- `fixer` 只能提交同章 command。fixer 必须有 `output.chapter_uid`，且 command 编译出的 patch scope 必须等于该 chapter。
- `supervisor` 可以提交任意合法 command。

### 8.2 fixer 允许的 command

当 `output.kind == "fixer"` 且 `output.chapter_uid` 非空时，允许：

- `split_block`
- `merge_blocks`
- `relocate_block`，仅限同一 chapter 内移动
- `pair_footnote`，仅限 source block 与 footnote 均在该 chapter
- `unpair_footnote`，仅限 marker source 与 footnote 均在该 chapter
- `mark_orphan`，仅限 marker source（如有）与 footnote 均在该 chapter
- `split_merged_table`

不允许 fixer 提交：

- `split_chapter`
- `merge_chapters`
- 跨 chapter 的 `relocate_block`
- 跨 chapter 的 footnote command
- 任何 book-wide command

这些必须升级为 `supervisor`。

### 8.3 scope 检查

compiler 生成 patch 后，不仅依赖 `BookPatch.validate`，还应在 AgentOutput 层检查：

- chapter-scoped output 不得包含 book-wide compiled patch。
- compiled patch 的 `scope.chapter_uid` 必须与 `output.chapter_uid` 一致。
- supervisor with `chapter_uid=None` 可产生 book-wide patch。
- supervisor with `chapter_uid=<uid>` 仍应遵守该 chapter scope，除非用户显式创建 book-wide supervisor output。

---

## 9. 各命令编译规格

### 9.1 `split_block`

参数：

```json
{
  "block_uid": "...",
  "strategy": "at_marker | at_line_index | at_text_match | at_sentence",
  "marker_occurrence": 1,
  "line_index": 0,
  "text_match": "...",
  "max_splits": 1,
  "new_block_uids": ["..."]
}
```

编译规则：

1. 查找 `block_uid`，要求 block 有 `text` 字段且当前值是字符串。
2. 复用当前 editor split 语义：`at_marker`、`at_line_index`、`at_text_match`、`at_sentence`。实现时应将 `editor/apply.py::_split_text` 提取为公开辅助函数（建议放在 `editor/text_split.py`），签名改为 `(text: str, strategy: str, params: dict) -> list[str]`，不依赖 `SplitBlock` 或 `EditOp` 类型。相关的正则常量 `_SENTENCE_SPLIT_RE` 和 `FN_MARKER_FULL_RE` 也需要一并迁移到可共享的位置。编译后应验证每个 segment 非空，避免 CJK/unicode 边界产生空 segment。
3. 生成一个 `SetFieldChange`：原 block `text` 从旧完整文本改为第一段。
4. 对每个后续 segment 生成 `InsertNodeChange`，新 node 从原 block 深拷贝而来，替换 `uid` 和 `text`。
5. 每个新 block 插入在前一个 segment block 后面。

验收重点：

- 非 text-bearing block 报 command error。
- `new_block_uids` 数量必须等于 `max_splits`。
- 新 uid 不得与现有 uid 或同命令其他 uid 冲突。
- 生成 patch 可由 `apply_book_patch` 成功应用，结果符合上述 split 规则。

### 9.2 `merge_blocks`

参数：

```json
{
  "block_uids": ["..."],
  "join": "concat | cjk | newline",
  "target_field": "text"
}
```

编译规则：

1. 要求至少两个 block。
2. 要求所有 block 在同一 chapter，且都有 `text` 字段。
3. 第一版要求 `block_uids` 在 chapter 中连续，且顺序必须与 chapter 当前顺序一致。
4. 用 `cjk_join` / concat / newline 生成 merged text。
5. 生成 `SetFieldChange` 修改第一个 block 的 `text`。
6. 对其余 block 按从后到前或原顺序生成 `DeleteNodeChange`。由于 delete 使用 uid 定位，顺序不影响定位，但从后到前更接近列表删除直觉。

验收重点：

- 跨 chapter 报错。
- 非连续 block 或顺序与 chapter 不一致时报错。
- 非 text-bearing block 报错。
- stale text precondition 由 `apply_book_patch` 捕获。

### 9.3 `relocate_block`

参数：

```json
{
  "block_uid": "...",
  "target_chapter_uid": "...",
  "after_uid": null
}
```

编译规则：

1. 查找 block 当前 parent chapter。
2. 验证 `target_chapter_uid` 存在。
3. 如果 `after_uid` 非空，必须属于目标 chapter 且不能等于 `block_uid`。
4. 生成一个 `MoveNodeChange`，`from_parent_uid` 为当前 chapter uid，`to_parent_uid` 为目标 chapter uid。

验收重点：

- 同章移动可由 fixer 在 chapter scope 中提交。
- 跨章移动必须 book-wide，第一版仅 supervisor 可提交。

### 9.4 `split_chapter`

参数：

```json
{
  "chapter_uid": "...",
  "split_at_block_uid": "...",
  "new_chapter_title": "...",
  "new_chapter_uid": "..."
}
```

编译规则：

1. 要求 `split_at_block_uid` 属于 `chapter_uid`。
2. 要求 split point 不是 chapter 第一个 block，避免原 chapter 被拆为空。
3. 生成 `InsertNodeChange`，在原 chapter 后插入空的新 chapter。
4. 将 split point 起到原 chapter 末尾的 blocks 逐个 `MoveNodeChange` 到新 chapter。
5. 新 chapter 的 `level` 可继承原 chapter，`id` 默认为 `None`。

`InsertNodeChange.node` 必须包含 `kind`。Phase 3 前置工作需给 `Chapter`（`ir/semantic.py`）增加 `kind: Literal["chapter"] = "chapter"` 字段，使其与 Block 的 discriminator 模式一致。当前 `Chapter` 继承 `BaseModel`（`extra="ignore"`），会静默丢弃 `kind`，导致 `InsertNodeChange` 要求的 `kind` 在 `Chapter.model_validate` 后消失。增加此字段后旧 JSON 无 `kind` 也会用默认值，向后兼容。chapter 插入 node 推荐形状：

```json
{
  "uid": "<new_chapter_uid>",
  "kind": "chapter",
  "title": "<new_chapter_title>",
  "level": 1,
  "id": null,
  "blocks": []
}
```

验收重点：

- patch scope 必须 book-wide。
- 原 chapter 保留 head blocks，新 chapter 获得 tail blocks。
- 不复制 block uid，只移动已有 block。

### 9.5 `merge_chapters`

参数：

```json
{
  "source_chapter_uids": ["...", "..."],
  "new_title": "...",
  "new_chapter_uid": "...",
  "sections": [
    {"text": "...", "id": null, "style_class": null, "new_block_uid": "..."}
  ]
}
```

编译规则：

1. 要求至少两个 source chapters，uid 唯一且存在。
2. 要求 `sections` 数量等于 source chapters 数量，`new_block_uid` 唯一且无碰撞。
3. 在最早 source chapter 的位置前后确定插入点：建议在 `min(source_indexes)` 的前一个 chapter 后插入新空 chapter；若为书首则 `after_uid=None`。
4. 对每个 source chapter：
   - 插入一个 heading block 到新 chapter，`level=2`，provenance 取 source chapter 第一个 block 的 provenance；若 source chapter 为空，使用 passthrough provenance。
   - 将 source chapter 的 blocks 逐个 move 到新 chapter，接在该 heading 后。
5. 所有 source chapters 被清空后，用 `DeleteNodeChange` 删除空 source chapters。

新 chapter 的 `InsertNodeChange.node` 同样包含 `kind: "chapter"`（`Chapter` 已有 `kind` 字段，见 §9.4 说明）：

```json
{
  "uid": "<new_chapter_uid>",
  "kind": "chapter",
  "title": "<new_title>",
  "level": 1,
  "id": null,
  "blocks": []
}
```

验收重点：

- 不通过插入包含现有 block uid 的 chapter 来制造临时重复 uid。
- source chapters 可不连续，但输出顺序遵守 `source_chapter_uids`。
- 删除 chapter 前必须已经移走所有 blocks。

### 9.6 footnote commands

`pair_footnote` 参数：

```json
{
  "fn_block_uid": "...",
  "source_block_uid": "...",
  "occurrence_index": 0
}
```

`unpair_footnote` / `mark_orphan` 参数：

```json
{
  "fn_block_uid": "...",
  "occurrence_index": 0
}
```

编译规则：

- `pair_footnote`：
  1. `fn_block_uid` 必须是 Footnote。
  2. `source_block_uid` 必须存在，且其 text-bearing fields 中有 raw callout。
  3. 生成 `SetFieldChange` 修改 source field：raw callout -> `make_fn_marker(fn.provenance.page, fn.callout)`。
  4. 如 footnote 当前 `orphan=True`，先生成 `SetFieldChange(orphan: True -> False)`。
  5. 生成 `SetFieldChange(paired: old -> True)`。

- `unpair_footnote`：
  1. 根据 marker 在全书查找 source field。
  2. 生成 source field `SetFieldChange`：marker -> raw callout。
  3. 生成 `SetFieldChange(paired: old -> False)`。

- `mark_orphan`：
  1. 如果 marker 已存在，先恢复 source field 为 raw callout。
  2. 若 `paired=True`，先设置 `paired=False`。
  3. 设置 `orphan=True`。

验收重点：

- 必须复用 `epubforge.markers` 中的 marker/raw callout helper，避免重新实现正则。
- 可复用 `epubforge.query` 的查询逻辑，以及实现时抽出的 text-field helper（例如 `fields.py`）；不要在 compiler 内复制 marker 正则或分散维护字段遍历规则。
- 必须覆盖 Paragraph、Heading、Footnote、Figure caption、Table html/title/caption 等当前文本字段支持范围。
- paired/orphan 的 change 顺序要避免 `apply_book_patch` 在中间状态拒绝：设置 `orphan=True` 前必须先将 `paired=False`，设置 `paired=True` 前必须先将 `orphan=False`。

### 9.7 `split_merged_table`

参数：

```json
{
  "block_uid": "...",
  "segment_html": ["...", "..."],
  "segment_pages": [1, 2],
  "new_block_uids": ["...", "..."]
}
```

Phase 3 不应在 compiler 中调用随机 `uuid4()`，否则 validate 与 submit 可能生成不同 uid。必须由 command params 显式提供 `new_block_uids`。

编译规则：

1. `block_uid` 必须是 Table，且 `multi_page=True`。
2. `segment_html` 与 `segment_pages` 长度一致且至少为 2。
3. `new_block_uids` 长度必须等于 segment 数量且无碰撞。
4. 记录原 table 在 chapter 中的前一个 block uid：若原 table 是 chapter 首个 block，则 `previous_uid=None`。
5. 生成 `DeleteNodeChange` 删除原 merged table。
6. 生成第一个 `InsertNodeChange`：
   - `parent_uid=<table 所在 chapter uid>`
   - `after_uid=previous_uid`
   - `node=<第一个 segment table>`
7. 后续 segment 依次生成 `InsertNodeChange`，每个 `after_uid` 指向前一个新 segment 的 uid。
8. 每个 segment 生成一个 Table node：
   - `html=segment_html[i]`
   - `table_title` 继承原 table
   - `caption` 仅最后一个 segment 继承原 caption，其余为空
   - `continuation=(i > 0)`
   - `multi_page=False`
   - `merge_record=None`
   - `bbox` 继承原 table
   - `provenance` 继承原 table，但 page 改为对应 `segment_pages[i]`

验收重点：

- 不允许随机 uid。
- split 后原 merged table 不存在，segment table 按原位置连续出现。

---

## 10. 具体工作包

### WP1：命令 schema 与错误模型

修改：

- `src/epubforge/editor/patch_commands.py`
- `tests/test_agent_output.py` 或新增 `tests/editor/test_patch_commands.py`

内容：

- 定义 `PatchCommandOp`。
- 定义每个 command 的 params 模型。
- 在 `PatchCommand` 中校验 `op` 和 `params`。
- 新增 `PatchCommandError`。
- 保持 `model_dump_json` 外形与 Phase 2 一致。

验收：

- 未知 `op` 在 `PatchCommand.model_validate` 阶段失败。
- params 缺必填字段、字段类型错误、extra 字段时报 ValidationError。
- Phase 2 中合法的空 params 测试需要更新：只有不需要 params 的命令才允许空 params；当前 `split_block` 空 params 应改为非法。

### WP2：compiler 基础设施

修改：

- `src/epubforge/editor/patch_commands.py`

内容：

- UID/chapter/block 查找 helper。
- 当前字段序列化 helper，确保 `SetFieldChange.old` 与 Phase 1 规则一致。
- `compile_patch_command` 和 `compile_patch_commands`。
- 编译后立即 `apply_book_patch` 到 evolving book。

验收：

- 两个命令连续修改同一 block 时，第二个命令基于第一个命令后的状态编译。
- 第一条 command 编译失败时，后续 command 不继续编译，但 validate 能保留其他非 command 错误。

### WP3：文本与 block 拓扑命令

实现：

- `split_block`
- `merge_blocks`
- `relocate_block`

验收：

- 每个 command 都有直接 `compile_patch_command + apply_book_patch` 单元测试。
- 每个 command 都有 `AgentOutput.validate` 测试。
- 至少 `split_block`、`merge_blocks`、`relocate_block` 有 `submit --apply` 测试，证明归档和 book 写入正确。

### WP4：chapter topology 命令

实现：

- `split_chapter`
- `merge_chapters`

验收：

- 仅 supervisor / book-wide output 可通过。
- fixer chapter-scoped output 使用这些命令失败。
- `merge_chapters` 不产生重复 uid。
- 删除 source chapters 前已清空 blocks。

### WP5：footnote 命令

实现：

- `pair_footnote`
- `unpair_footnote`
- `mark_orphan`

验收：

- raw callout 替换与 marker 恢复使用 `epubforge.markers` helper，并优先复用查询/text-field helper。
- `paired` 和 `orphan` invariant 在整个 patch apply 过程中不被破坏。
- 跨章 footnote command 只有 supervisor book-wide output 可通过。
- command 编译结果可被 `apply_book_patch` 重放。

### WP6：table 命令

实现：

- `split_merged_table`

验收：

- `split_merged_table` 使用显式 `new_block_uids`，不使用随机 uuid。
- 原 table 被删除后，segments 精确插回原位置。

### WP7：AgentOutput validate/submit 集成

修改：

- `src/epubforge/editor/agent_output.py`

内容：

- 删除 `_compile_commands` no-op stub 或改为调用 `compile_patch_commands`。
- `validate_agent_output` 不再对所有 command 报 “not implemented”。
- `command.agent_id != output.agent_id` 报错。
- 编译产物参与 scope 和 kind permission 检查。
- `submit_agent_output` 使用同一编译路径。

验收：

- 原先 `test_validate_rejects_uncompiled_commands` 和 `test_submit_dry_run_rejects_uncompiled_commands` 应改为 unknown/invalid command 失败测试。
- 合法 command output 的 dry-run validate 成功。
- 合法 command output 的 `submit --apply` 会修改 book 并归档 output。
- validate 失败不会写 book/memory，也不会归档。

### WP8：测试整理与回归

建议测试文件布局：

- `tests/editor/test_patch_commands.py`：compiler 纯单元测试。
- `tests/test_agent_output.py`：只保留 AgentOutput/CLI 集成测试。
- 保留 `tests/editor/test_patches.py` 不动，除非 Phase 3 暴露出 Phase 1 bug。

最低质量门：

- `uv run pytest tests/editor/test_patches.py tests/editor/test_patch_commands.py tests/test_agent_output.py`
- 若项目当前习惯不是 `uv run`，按现有仓库工具链执行等价 pytest 命令。

---

## 11. 验收标准

Phase 3 可视为完成，必须同时满足：

1. `PatchCommand.op` 不再是任意字符串；未知 command 在 schema 或 validate 阶段失败。
2. `agent-output add-command` 仍接受 Phase 2 约定的 JSON 文件外形。
3. `agent-output validate` 对合法 commands 返回 `{"valid": true, ...}`，不再报 “PatchCommand compilation is not implemented”。
4. `agent-output submit --apply` 能应用 command 编译出的 patches，随后归档 AgentOutput。
5. `command.agent_id != output.agent_id` 被拒绝。
6. scanner/reviewer command 被拒绝。
7. fixer chapter-scoped command 不能越过 `output.chapter_uid`。
8. supervisor book-wide command 可执行 chapter topology。
9. 所有 command 编译产物都能被 `apply_book_patch` 验证并应用。
10. 所有 command 生成的 `BookPatch` 都包含有效 `old` / `old_node` precondition。
11. 不引入随机 uid；需要新 uid 的 command 都从 params 接收。
12. 每个 command op 至少有一个 success-path 测试和一个 precondition-failure 测试（参见各 §9.x "验收重点"中列出的 error case）。
13. 权限 error path 测试：scanner 提交 command 被拒、fixer 提交 book-wide command 被拒、fixer 提交跨章 footnote command 被拒。
14. 命令链 error path 测试：两个 command 序列中第一个 command 失败时，第二个 command 不被编译，且 errors 中包含失败信息和 patches 跳过说明。
15. `SetFieldChange.old` 的序列化方式必须使用与 Phase 1 apply 比较逻辑一致的 `_serialize_field_value`。
16. Phase 1/2 既有测试经更新后通过。

---

## 12. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| validate 与 submit 分别编译导致 uid 或 patch 内容漂移 | dry-run 通过但 submit 失败或改出不同结果 | compiler 禁止随机 uid；validate/submit 复用同一 helper；新 uid 全由 params 提供 |
| `merge_chapters` 临时复制现有 block uid 造成重复 uid | BookPatch validator 可能漏检或后续状态损坏 | 只插入空新 chapter，再 move 现有 blocks，最后 delete 空源章 |
| footnote paired/orphan change 顺序错误 | `apply_book_patch` 中间状态拒绝 | 明确先解除冲突 flag，再设置目标 flag |
| fixer command 权限过宽 | chapter agent 可能修改全书结构 | 第一版只允许 fixer chapter-scoped、同章 command；book-wide 全部要求 supervisor |
| Phase 2 旧测试假设 command 未实现 | 测试失败但不是功能回归 | 更新为新行为测试，并保留 invalid command 失败覆盖 |
| 范围外命令被误当作 Phase 3 缺口 | 计划膨胀，影响最小 macro compiler 交付 | 验收标准只覆盖 9 个 macro；其他能力另开后续计划 |
| evolving book 深拷贝开销 | 9 commands × `apply_book_patch`（每次 `model_copy(deep=True)`），加上 validate/submit 可能重复编译 = 18+ 次深拷贝；5000+ block 的大型书籍可能导致 validate/submit 延迟数秒 | 第一版接受此开销，正确性优先于性能。WP8 中用 5000+ block fixture 做性能基线测试。如果超过 2 秒，可考虑：(a) `compile_patch_commands` 内部的 evolving book 用 mutable apply；(b) validate 和 submit 通过 `AgentOutputValidationResult` 共享同一次编译结果，避免重复编译（§7.3 已推荐此方案） |
| `split_block` 的 text split 跨 CJK/unicode 边界 | `at_line_index` 和 `at_text_match` 策略在多行文本、CJK 零宽字符场景中可能产生空 segment 或意外切分 | compiler 在 split 后验证每个 segment 非空 |

---

## 13. 推荐执行顺序

1. 先做 WP1 和 WP2，建立 typed params、错误模型、compiler 框架、evolving book 编译。
2. 做 WP3，覆盖最常用且局部的 block/text 命令，先打通 `validate` 和 `submit --apply`。
3. 做 WP7 的 AgentOutput 集成，把 no-op stub 替换掉，并更新 Phase 2 command 相关测试。
4. 做 WP5 footnote 命令，因为它最容易触发 paired/orphan invariant 和 marker 细节。
5. 做 WP6 table 命令，精确处理原位置替换和显式 uid。
6. 做 WP4 chapter topology 命令，最后处理 book-wide 结构变更和权限。
7. 做 WP8 测试整理与全量相关测试。

这个顺序让实现代理尽早得到一条可运行的 command submit 路径，同时把高风险的 footnote/table/chapter 拓扑放到有基础设施之后。

---

## 14. 评审重点

以下问题已经评审并解决：

1. `PatchCommand.params` 保持 dict 外形但内部 typed helper 的方案是否足够清晰 —— **已确认**：方案可行，比 discriminated union 更灵活且避免改 JSON 外形。
2. `CompiledCommands` 与 `AgentOutputValidationResult` 是否足以避免 validate/submit 漂移 —— **已确认**：设计有效。§7.3 已补充编译失败时跳过 patches 验证的规则。
3. `merge_chapters` 的 move-based 编译是否能被当前 `MoveNodeChange` 正确处理 —— **已确认**：与当前语义兼容。
4. chapter `InsertNodeChange.node` 的 `kind: "chapter"` 是否需要同步回源码 —— **已解决**：§3 前置条件新增第 5 条，要求给 `Chapter` 增加 `kind: Literal["chapter"] = "chapter"` 字段。
5. `split_merged_table` 的 delete + insert 是否覆盖所有位置 —— **已确认**：首位（`after_uid=None`）、中间、末尾三种位置均正确。

追加的评审发现（已纳入修订）：

6. **commands 编译失败后 patches 验证基准** —— §7.3 步骤 5 已明确：编译失败则跳过 `output.patches` 验证，在 errors 中追加说明。
7. **`command.agent_id` 检查完全缺失** —— §4.2、§7.3、§8.1 已修正为"从零新增"，并扩展到 `patch.agent_id` 检查。
8. **`split_block` 复用路径不明** —— §9.1 已指明需提取 `apply.py::_split_text` 为公开 API。
9. **evolving book 深拷贝性能风险** —— §12 已新增风险条目和缓解策略。
10. **验收标准模糊** —— §11 第 12 条已拆分为 12-16 条具体可验证标准。
