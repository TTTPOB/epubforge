# Phase 1 BookPatch 实现计划评审 — R2

> 评审人：架构评审  
> 评审日期：2026-04-24  
> 对应文档：phase1-bookpatch.md（R1 修订版）  
> 参考：phase1-review-r1.md、semantic.py、ops.py

---

## R1 问题跟踪

### 严重问题 (S)

#### S1. IR 模型中 `uid` 是 `str | None`，但 BookPatch 假设 uid 非空

✅ **已解决。** §4.3 新增 `_require_all_uids_non_none` 函数，在 `apply_book_patch` 和 `validate_book_patch` 入口处均执行检查。错误信息包含位置信息（chapter index / block index）。设计说明中也正确指出 Phase 4 可将 `uid` 改为 `str`（必填）。

#### S2. `Heading.level` 在 IR 模型中无上界约束

✅ **已解决。** §3 新增 `_VALID_HEADING_LEVELS` 和 `_VALID_CHAPTER_LEVELS` 常量（均为 `frozenset({1, 2, 3})`），并明确说明 `semantic.py` 的 `int` 类型不能提供约束。§4.5 的增量 precondition 检查中显式列出 level 校验逻辑。测试计划 §8.1 和 §8.13 均覆盖。

#### S3. `SetFieldChange` 的 `old`/`new` 类型和比较语义

✅ **已解决。** 类型注解改为 `Any`，§2.4.1 新增详细的序列化约定：提交方通过 `model_dump(mode="json")` 获取字段值填入 `old`，precondition 比较时系统端同样序列化后比较。`_serialize_field_value` 辅助函数的行为也明确定义。

#### S4. `ReplaceNodeChange` 的 `old_node` 序列化约定

✅ **已解决。** §2.4.2 明确：`old_node` 包含 `uid` 和 `kind`，使用 `model_dump(mode="python")` 序列化和比较。同样的约定适用于 `DeleteNodeChange.old_node`（§2.4.4）。

#### S5. 索引重建策略缺失

✅ **已解决。** §4.6 详细定义了每种 change 类型后是否需要重建索引：`SetFieldChange` 和 `ReplaceNodeChange` 不重建，`InsertNodeChange`/`DeleteNodeChange`/`MoveNodeChange` 完整重建。代码示例清晰。

### 设计建议 (D)

#### D1. `PatchScope` 语义含糊（chapter_uid=None 且 book_wide=False）

⚠️ **部分解决。** §2.3 补充了语义说明："两者都为 falsy（默认值状态）：等同 `book_wide=True`"。但 R1 建议的核心是**减少状态空间**（去掉 `book_wide` 字段或强制二选一），修订版保留了原设计。这不是错误——两种 falsy 状态的等价行为已明确记录，但仍存在两种方式表达同一语义的冗余。考虑到这是新系统无历史包袱，保留冗余是有意的设计选择，可以接受。

#### D2. validate 与 apply 双重深拷贝

✅ **已解决。** §4 做了根本性重构：合并 validate 和 apply 为单一事务性操作 `apply_book_patch`（一次深拷贝），`validate_book_patch` 降级为轻量级静态预检（不做深拷贝，不评估 precondition）。这正是 R1 建议的方向。

#### D3. 可编辑字段表缺少 `table.bbox`

✅ **已解决。** §3 的 `_ALLOWED_SET_FIELD["table"]` 已包含 `bbox`，并标注 `[R1: D3 addressed]`。

#### D4. `InsertNodeChange` 校验路径歧义

✅ **已解决。** §2.4.3 明确区分 block 和 chapter 的校验路径：block 使用 `BLOCK_PAYLOAD_MODELS[kind].model_validate()`（去除 uid/kind），chapter 使用 `Chapter.model_validate(node)`。

#### D5. `MoveNodeChange` 缺少 chapter 级别移动定义

✅ **已解决。** §2.4.5 补充了完整语义说明，§4.7 的 `_apply_move_node` 包含完整的 chapter 移动实现代码（包括移除后重新计算位置的逻辑）。

#### D6. `ReplaceNodeChange` 用于 chapter 时语义不明

✅ **已解决。** §2.4.2 明确限制 `ReplaceNodeChange` 仅用于 block 级别，chapter 修改通过 `SetFieldChange`（metadata）或 `delete_node` + `insert_node` 组合。测试 §8.14 验证对 chapter uid 使用 `ReplaceNodeChange` 被拒绝。

#### D7. `_make_block` 复用策略不明

✅ **已解决。** §5 明确 `_make_block` 是 Phase 1 新写的辅助函数，不从 `apply.py` 引入，并描述了实现逻辑。

### 测试遗漏 (T)

#### T1. uid=None 的 Book 上执行 patch
✅ **已解决。** §8.11 新增 4 个测试用例。

#### T2. `chapter.level` 范围校验
✅ **已解决。** §3 的 `_VALID_CHAPTER_LEVELS`，§8.1 的测试用例。

#### T3. `ReplaceNodeChange` 替换 chapter
✅ **已解决。** §8.14 明确测试 `ReplaceNodeChange` 指向 chapter uid 被拒绝。

#### T4. 同一节点多次 `SetFieldChange` 的 precondition 基准
✅ **已解决。** 这是 R1 中最关键的测试遗漏/设计缺陷。修订版通过 D2 的合并方案从根本上解决——precondition 检查在每个 change 执行前、针对已演化的 working 副本进行。§8.10 包含链式 SetFieldChange 的详细测试和代码示例。

#### T5. `InsertNodeChange` 插入 chapter
✅ **已解决。** §8.3 包含两个相关测试用例（空 chapter 和含 blocks 的 chapter）。

#### T6. no-op move
✅ **已解决。** §8.5 包含测试用例，预期成功且版本 +1。

#### T7. `old == new` 复杂类型比较
✅ **已解决。** §8.12 包含 `None`/`list`/`dict` 的相等性测试。

#### T8. 空白字符边界
✅ **已解决。** §2.4.1 的 `SetFieldChange` 对 `target_uid` 和 `field` 均使用 `require_non_empty` 校验。§8.1 和 §8.7 包含纯空白字符串的测试用例。

#### T9. 超大 patch 性能测试
⚠️ **未解决，可接受。** 修订版未添加性能测试。考虑到 Phase 1 是新代码且索引策略选择了简单的完整重建，性能测试不是阻塞项。可在实现完成后根据实际需要补充。

### 总体设计偏差 (V)

#### V1. batch change 扩展点
✅ **已解决。** §2.5 明确声明 batch change 走 Phase 3 的 PatchCommand macro 路线，不扩展 IRChange union。

#### V2. provenance 语义合法性
✅ **已解决。** §6.4 明确记录为有意推迟。

#### V3. table HTML 合法性
✅ **已解决。** §6.4 明确记录为有意推迟。

#### V4. agent 字段修改权限
✅ **已解决。** §6.4 明确记录为 Phase 2 的 AgentOutput 层面处理。

---

## 新引入的问题

### N1. `SetFieldChange` 与 `ReplaceNodeChange`/`DeleteNodeChange` 使用不同的序列化模式

`SetFieldChange` 的 `old`/`new` 使用 `model_dump(mode="json")` 序列化（§2.4.1，第 116 行），而 `ReplaceNodeChange`/`DeleteNodeChange` 的 `old_node` 使用 `model_dump(mode="python")` 序列化（§2.4.2，第 144 行）。

这是**有意的**还是疏忽？两种模式的区别：
- `mode="json"`：将所有值转为 JSON 基本类型（如 `datetime` → `str`，枚举 → 值）
- `mode="python"`：保留 Python 原生类型（如 `Provenance` 对象保留为 `Provenance` 实例？不对——`model_dump(mode="python")` 仍然返回 dict，但嵌套类型保留为 Python 原生类型而非 JSON string）

实际问题：对于当前 IR 模型中的字段类型（`str`、`int`、`float`、`bool`、`None`、`list[float]`、`Literal`），`mode="json"` 和 `mode="python"` 的输出几乎没有差异。唯一可能的差异在于 `Provenance.source` 是 `Literal["llm", "vlm", ...]`，两种模式下都输出 `str`。所以**功能上不会出错**，但认知负担增加——维护者需要记住不同 change 类型使用不同的序列化模式。

**严重程度**：低。建议在实现时统一为一种模式（`mode="python"` 即可，因为 IR 模型不含需要 JSON 特殊处理的类型），但不阻塞实现。

### N2. `validate_book_patch` 的静态检查对 insert 做 `model_validate` 但不检查 heading level

§4.4 步骤 4 中，`InsertNodeChange` 静态检查包含 "使用对应模型校验 node 内容"。对于 heading 类型的 insert，`BLOCK_PAYLOAD_MODELS["heading"]` 是 `HeadingPayload`，其 `level` 字段是 `Literal[1, 2, 3]`（ops.py 第 72 行），所以 `model_validate` 本身就能捕获非法 level。但对于 `ReplaceNodeChange`，§4.4 步骤 4 同样说 "使用对应 Pydantic Payload 模型对 `change.new_node` 做 `model_validate`"，所以非法 heading level 也会被 Payload 模型捕获。

这意味着 `_VALID_HEADING_LEVELS` 常量实际上只在 `SetFieldChange` 的增量 precondition 检查中被使用（§4.5 第 2 条）。这不是问题——只是实际的约束路径比计划文档暗示的更分散：`SetFieldChange` 由 `_VALID_HEADING_LEVELS` 约束，`InsertNodeChange`/`ReplaceNodeChange` 由 `HeadingPayload.level: Literal[1,2,3]` 约束。

**严重程度**：无。只是实现时需注意约束路径的差异，不是 bug。

---

## 内部一致性

### C1. `_serialize_field_value` 与 `SetFieldChange` 序列化约定的微妙不一致

§2.4.1 说提交方使用 `node.model_dump(mode="json")` 获取字段值填入 `old`，这意味着 `old` 中的值是 JSON-compatible 类型。§4.5 的 `_serialize_field_value` 说"对 Pydantic 模型实例调用 `.model_dump(mode="json")`，对 Python 原生类型直接返回"。

考虑一个场景：字段值是 `list[float]`（如 `bbox = [1.0, 2.0, 3.0, 4.0]`）。
- 提交方通过 `node.model_dump(mode="json")["bbox"]` 获取 → 结果是 `[1.0, 2.0, 3.0, 4.0]`（list of float）
- 系统端 `_serialize_field_value([1.0, 2.0, 3.0, 4.0])` → `list` 是原生类型，直接返回 → 结果是 `[1.0, 2.0, 3.0, 4.0]`

两者匹配。再考虑 `Provenance` 字段：
- 提交方：`node.model_dump(mode="json")["provenance"]` → `{"page": 1, "bbox": null, "source": "llm", ...}`
- 系统端：`_serialize_field_value(provenance_instance)` → `provenance_instance.model_dump(mode="json")` → `{"page": 1, "bbox": null, "source": "llm", ...}`

也匹配。**结论：逻辑一致，无问题。** 但 `_serialize_field_value` 的描述可以更精确——当字段值本身是 `dict`（已经通过前序 change 被设置为 dict 而非 Pydantic 对象）时，直接返回也是正确的。

### C2. `ReplaceNodeChange` 仅用于 block，但 precondition 检查使用 `index.block_index` 直接查找

§4.5 和 §4.7 的 `_apply_replace_node` 直接使用 `index.block_index[change.target_uid]`。如果 `target_uid` 是 chapter uid，会触发 `KeyError` 而非可读的 `PatchError`。

§4.4 的静态预检步骤 4 只检查 `target_uid` 存在性，没有检查它是否在 `block_index`（而非 `chapter_index`）中。

**建议**：在 `validate_book_patch` 的 `ReplaceNodeChange` 静态检查中加一条：`target_uid` 必须在 `block_index` 中（不能是 chapter）。这样 §8.14 测试用例（"用 ReplaceNodeChange 的 target_uid 指向 chapter uid"被拒绝）能在静态预检阶段就被捕获，而不是在 apply 阶段抛出 `KeyError`。

**严重程度**：低。测试 §8.14 的预期行为是 `PatchError`，但按当前实现路径，如果只走 `apply_book_patch` 且跳过静态预检中的这个检查，实际会得到 `KeyError`（或者在增量 precondition 检查的 `_get_node` 中被包装为 `PatchError`，取决于 `_get_node` 的实现）。实现时补充此检查即可。

### C3. 无其他内部矛盾

计划各部分之间的引用一致：
- §2.4.2 限制 `ReplaceNodeChange` 仅用于 block ↔ §4.7 的 apply 逻辑只查 `block_index` ↔ §8.14 测试验证 chapter uid 被拒绝 ✓
- §4.2 调用 `validate_book_patch` 作为第一步 ↔ §4.4 定义轻量级静态预检 ✓
- §4.5 增量 precondition 在 working 副本上检查 ↔ §8.10 链式 SetFieldChange 测试 ✓
- §6.2 列出的引入依赖与实际代码中的模块位置一致 ✓

---

## 结论

**Yes，可以进入实现。**

R1 提出的 5 个严重问题和 9 个测试遗漏全部得到实质性解决。D2（合并 validate 和 apply）的采纳是修订版最重要的结构改进，同时解决了 T4（链式修改 precondition 基准）的设计缺陷。

实现时注意两个小项：
1. **C2**：`validate_book_patch` 的 `ReplaceNodeChange` 静态检查中应加 `target_uid in block_index` 断言，确保 §8.14 测试在正确的阶段触发正确的错误类型。
2. **N1**：考虑统一 `SetFieldChange` 和 `ReplaceNodeChange`/`DeleteNodeChange` 的序列化模式（均用 `mode="python"` 或均用 `mode="json"`），减少维护者的认知负担。

两者均不阻塞实现，可在编码过程中顺手处理。
