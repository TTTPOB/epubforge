# Phase 2 实施计划评审 (R2)

> 评审人：架构评审
> 评审对象：`phase2-agent-output.md`（R1 修订版）
> 参考文档：`phase2-review-r1.md`（R1 评审）、`phase1-bookpatch.md`（R1 修订版）

---

## R1 问题跟踪

### 严重问题 (S)

**S1. `validate_book_patch` 签名不匹配** — ✅ resolved

修订版 §5.4 正确使用 `try/except PatchError` 包装，参数顺序修正为 `(book, patch)`。代码示例准确匹配 Phase 1 接口。

**S2. `apply_book_patch` 返回类型假设模糊** — ✅ resolved

修订版 §6.1 `apply_patches_sequentially` 改用 `try/except PatchError` 模式，不再假设 `.book`/`.error` 属性。参数顺序 `(current, patch)` 正确。

**S3. `save_book` 函数签名假设错误** — ✅ resolved

修订版 §6 步骤 11 改用 `atomic_write_model(paths.book_path, new_book)`，不再调用 `save_book`。归档也改用 `atomic_write_text`（§6.4）。

**S4. MemoryPatch 验证遗漏 convention/pattern UID 校验** — ✅ resolved

修订版 §5.6 补充了 `mp.conventions[*].evidence_uids` 和 `mp.patterns[*].affected_uids` 的存在性校验代码。测试计划 §8.8 也新增了对应测试用例。

**S5. memory merge 调用方式与实际 API 不匹配** — ✅ resolved

修订版 §6.2 `apply_memory_patches_sequentially` 正确使用 `merge_edit_memory` 签名，返回 `MemoryMergeResult`，逐 MemoryPatch 连续 merge，第 i+1 次的输入是第 i 次的输出。

### 设计建议 (D)

**D1. 文件命名混淆** — ✅ resolved

`PatchCommand` 模型放 `patch_commands.py`，CLI 放 `agent_output_cli.py`。§1 文件布局表与后文引用一致。

**D2. scope 一致性检查逻辑漏洞** — ✅ resolved

修订版 §5.7 引入 `_is_book_wide(scope)` 辅助函数，同时检查 `scope.book_wide or scope.chapter_uid is None`，覆盖了 `PatchScope()` 默认值等同 `book_wide=True` 的情况。§5.8 scanner/fixer 权限检查也统一使用此函数。

**D3. archive 文件操作非原子** — ✅ resolved

修订版 §6.4 改用 `atomic_write_text(archive_path, content)` 后再 `src.unlink()`。并在末尾说明了极端情况（`unlink` 失败时 `load_agent_output` 检查是否已有同 id 归档文件）。

**D4. reviewer 是否允许 `replace_node`** — ✅ resolved

修订版 §5.8 reviewer 部分明确只允许 `set_field`，不允许 `replace_node` 和所有 topology 操作。设计说明也解释了理由。

**D5. `evidence_refs` 只声明不校验** — ✅ resolved

修订版 §2.3 字段说明和 §10 遗留问题表中均明确标注为 Phase 9 VLM 系统的预留字段，Phase 2 有意不校验。

**D6. `begin` 返回值缺少 `base_version`** — ✅ resolved

修订版 §4.2 步骤 9 的 stdout JSON 已包含 `base_version` 字段。

**D7. 并发 agent output 无互斥** — ✅ resolved

修订版 §10 遗留问题表明确记录了"Phase 2 仅支持单 agent 串行工作模式"，并说明 `base_version` 校验作为乐观锁机制。

### Phase 1 接口假设 (A)

**A1. `validate_book_patch` 签名** — ✅ resolved（同 S1）

**A2. `apply_book_patch` 返回类型** — ✅ resolved（同 S2）

**A3. `PatchScope` book_wide 默认语义** — ✅ resolved（同 D2，`_is_book_wide` 辅助函数）

**A4/A5/A6. 其他接口假设** — ✅ R1 已确认匹配，修订版未引入偏差。

### 与总体设计的偏差 (V)

**V1. scanner 必须更新 `read_passes`** — ✅ resolved

修订版 §5.8 scanner 部分新增了 `has_read_pass_update` 检查逻辑。

**V2. `submit --stage` 模式缺失** — ✅ resolved

修订版 §4.9 新增 `--stage` 参数，Phase 2 作占位实现（返回 `staged: false` 信息），并在说明中解释了不在 Phase 2 实现的原因（旧 staging 格式不兼容）。

**V3. topology patch 权限模型与总体设计偏差** — ✅ resolved

修订版 §5.8 fixer 部分明确禁止直接通过 BookPatch 提交 topology 操作（`insert_node`/`delete_node`/`move_node`），要求必须走 PatchCommand（Phase 3 编译）。设计说明引用总体设计文档保持一致。

**V4. submit 后续租** — 无需修改（R1 已确认 Phase 2 正确跳过）。

### 测试遗漏 (T)

**T1. 并发 output 冲突测试** — ✅ resolved

修订版 §8.10 新增 `test_concurrent_submit_base_version_conflict`。

**T2. add-* 重复调用幂等性** — ✅ resolved

修订版 §8.3 新增 `test_add_note_idempotent_append`（append 语义，不去重）。

**T3. 大 output 性能边界** — ✅ resolved

修订版 §8.12 新增 `test_validate_large_output_smoke`（50 patches + 50 memory_patches，2 秒内完成）。

**T4. memory merge 失败时的回滚行为** — ✅ resolved

修订版 §6 步骤顺序说明明确了 book.json 写入在 memory merge 之后（步骤 11 在步骤 10 之后），memory merge 失败时 book.json 尚未被写入。同时承认 book 写入后进程崩溃的已知限制，记录在 §10。

**T5. archive 目标文件已存在** — ✅ resolved

修订版 §8.10 新增 `test_archive_target_already_exists`。

**T6. output 文件损坏** — ✅ resolved

修订版 §8.10 新增 `test_load_output_corrupted_json`。

**T7. `asked_by` 字段自动填充** — ✅ resolved

修订版 §4.4 步骤 3 明确 `asked_by` 强制使用 `output.agent_id`，§8.4 新增 `test_add_question_asked_by_is_agent_id`。

**T8. submit 后再次 submit 同一 output** — ✅ resolved

修订版 §8.9 新增 `test_submit_apply_second_submit_fails`。

**T9. `PatchScope(chapter_uid=None, book_wide=False)` scanner 绕过** — ✅ resolved（同 D2/A3，`_is_book_wide` 统一处理）

修订版 §8.8 新增 `test_validate_scope_book_wide_false_chapter_none_treated_as_book_wide`。

**T10. reviewer 提交 `replace_node`** — ✅ resolved（同 D4）

修订版 §8.8 新增 `test_validate_reviewer_replace_node_rejected`。

---

## Phase 1/Phase 2 接口对齐

逐项核对修订后的 Phase 2 调用点与修订后的 Phase 1 导出接口：

### 1. `validate_book_patch(book, patch) -> None`（抛 `PatchError`）

- Phase 2 §5.4 调用方式：`validate_book_patch(book, patch)`，`try/except PatchError as e`，读取 `e.reason` — **匹配**
- Phase 1 §4.4 签名：`validate_book_patch(book: Book, patch: BookPatch) -> None`，失败时 `raise PatchError(reason, patch_id)` — **匹配**
- 注意：Phase 1 的 `validate_book_patch` 是轻量级静态预检（不验证 old/new precondition），Phase 2 在 validate 阶段调用它来收集静态错误是合理的——但这意味着 Phase 2 的 validate 命令**无法检测到 precondition 不匹配问题**（需要 `apply_book_patch` 才能发现）。Phase 2 §4.8 的说明"收集所有 errors，统一返回"在 precondition 层面是有限的。这不是 bug，但实施者应注意 validate 命令给出的 `valid: true` 不等于 apply 一定成功。

### 2. `apply_book_patch(book, patch) -> Book`（抛 `PatchError`）

- Phase 2 §6.1 调用方式：`current = apply_book_patch(current, patch)`，`except PatchError as e`，读取 `e.reason` — **匹配**
- Phase 1 §4.1 签名：`apply_book_patch(book: Book, patch: BookPatch) -> Book` — **匹配**

### 3. `PatchError(reason, patch_id)` 属性

- Phase 2 访问 `e.reason` — **匹配**
- Phase 1 §2.2：`self.reason = reason; self.patch_id = patch_id` — **匹配**

### 4. `PatchScope(chapter_uid, book_wide)` 语义

- Phase 2 `_is_book_wide(scope)` 实现：`scope.book_wide or scope.chapter_uid is None` — **匹配** Phase 1 §2.3 "两者都为 falsy：等同 `book_wide=True`"

### 5. `BookPatch` 字段

- Phase 2 引用 `patch.patch_id`、`patch.scope`、`patch.changes`、`patch.changes[i].op` — 均与 Phase 1 §2.6 定义一致
- Phase 2 引用 `change.op` 值集合 `{"set_field", "replace_node", "insert_node", "delete_node", "move_node"}` — 与 Phase 1 §2.4 的五种 IRChange 匹配

### 6. `BookPatch.base_version` 校验语义差异（信息性说明，非问题）

- Phase 1 `validate_book_patch` §4.4 步骤 2：仅拒绝 `base_version > book.op_log_version`（未来版本），允许 stale（`<`）
- Phase 2 `validate_agent_output` §5.2：拒绝 `base_version != book.op_log_version`（精确匹配）

两者并不矛盾——Phase 1 的检查在 BookPatch 级别做宽容处理（stale patch 可能通过 precondition 检查），Phase 2 的检查在 AgentOutput 级别更严格（要求 output 的 base_version 精确匹配当前 book 版本）。Phase 2 的严格检查在 validate 阶段先于 Phase 1 的宽容检查执行，整体逻辑正确。

### 7. Phase 1 头部依赖声明一致性

Phase 2 文件头部（第 7-10 行）列出的 Phase 1 接口规范与 Phase 1 修订版完全一致：
- `validate_book_patch(book: Book, patch: BookPatch) -> None` — 匹配
- `apply_book_patch(book: Book, patch: BookPatch) -> Book` — 匹配
- `PatchError(reason: str, patch_id: str)` — 匹配
- `PatchScope(chapter_uid=None, book_wide=False)` 语义 — 匹配

**结论：Phase 1/Phase 2 接口完全对齐，无不匹配。**

---

## 新引入的问题

### N1. `_is_book_wide` 对 supervisor 的 scope 检查缺少对称性（轻微）

§5.7 的 scope 一致性校验只在 `output.chapter_uid is not None` 时检查。当 `output.chapter_uid is None`（全书 output）时，不检查 patch scope——这意味着全书 output 可以包含 chapter-scoped patches（`scope.chapter_uid` 非 None）。这本身语义上可接受（全书 output 权限更大），但没有明确说明这是有意的。

**影响**：极轻微。supervisor 全书 output 包含 chapter-scoped patch 不会导致问题，因为 `validate_book_patch` 仍会检查 scope 内的 UID 存在性。无需修改，但实施时可加一行注释说明。

### N2. submit 步骤 7-8 的 commands 编译与 patches 合并顺序

§6 步骤 7-8：`compiled_patches + output.patches`。这意味着 commands 编译出的 patches 先于用户手写的 patches 执行。虽然 Phase 2 中 `compile_commands` 返回空列表不影响行为，但这个顺序选择应明确记录为设计决策，以免 Phase 3 实现编译器时产生歧义。

**影响**：Phase 2 无实际影响（compiled 为空）。Phase 3 实施时需确认顺序。

---

## 内部一致性

### C1. §4.7 add-memory-patch 的 UID 验证时机说明略有矛盾

§4.7 步骤 4 先说"对 MemoryPatch 中的 UID 引用做即时预验证"，紧接着又说"Phase 2 选择推迟到 validate"。两句话放在一起读起来自相矛盾，虽然最终结论是明确的（推迟到 validate）。

**影响**：无功能影响，仅表述不够简洁。实施者能从上下文理解意图。

### C2. 其余部分内部一致性良好

- §2.2 模型定义与 §5 validate 规则中引用的字段完全一致
- §4 CLI 命令的参数与 §7.3 tool_surface 函数签名对应
- §8 测试计划覆盖了 §5 中所有 validate 规则和 §6 中所有 submit 步骤
- §10 遗留问题表与正文中的 TODO/Phase 3 注释一致

---

## 结论

**Yes，计划可以进入实施阶段。**

R1 提出的全部 5 个严重问题、7 个设计建议、3 个偏差、10 个测试遗漏均已正确修复。Phase 1/Phase 2 接口完全对齐。修订过程引入的新问题（N1/N2）均为信息性说明，不影响正确性。内部一致性唯一的瑕疵（C1）是表述问题，不影响实施。

实施时建议关注的两点（非阻塞）：
1. Phase 2 的 `validate` 命令返回 `valid: true` 不代表 `apply` 一定成功（Phase 1 的 precondition 检查只在 apply 时增量执行）。可在 validate 命令的 CLI help 或返回 JSON 中加一句提示。
2. §6 步骤 7-8 的 `compiled_patches + output.patches` 顺序在 Phase 3 编译器实现前确认。
