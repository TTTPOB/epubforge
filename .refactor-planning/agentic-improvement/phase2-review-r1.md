# Phase 2 实施计划评审 (R1)

> 评审人：架构评审  
> 评审对象：`phase2-agent-output.md`  
> 参考文档：`agentic-improvement.md`、`phase1-bookpatch.md`、`editor/app.py`、`editor/state.py`、`editor/memory.py`

---

## 严重问题 (Must fix before implementation)

### S1. `validate_book_patch` 签名不匹配 — Phase 2 假设与 Phase 1 定义矛盾

Phase 2 第 598-604 行调用方式：

```python
patch_errors = validate_book_patch(patch, book)
```

返回 `list[str]`，收集错误后继续。

但 Phase 1 第 306 行明确定义：

```python
def validate_book_patch(book: Book, patch: BookPatch) -> None:
    # Raises PatchError if any check fails.
```

**两处冲突**：
1. **参数顺序**：Phase 1 是 `(book, patch)`，Phase 2 写的是 `(patch, book)`。
2. **返回类型**：Phase 1 抛出 `PatchError`（fail-fast），Phase 2 期望返回 `list[str]`（收集所有错误）。

Phase 2 的 `validate_agent_output` 需要在 single pass 中收集多个 patch 的所有错误（见 §5 "不 fail-fast，尽量多报错"），但 Phase 1 的设计是单个 patch 级别 fail-fast。

**建议修复**：在 Phase 2 中包装调用——对每个 patch 捕获 `PatchError` 后追加到 errors 列表。不要修改 Phase 1 签名（Phase 1 的 fail-fast 在 apply 路径中是合理的）。在 Phase 2 的 §5.4 中明确写出包装逻辑：

```python
for i, patch in enumerate(output.patches):
    try:
        validate_book_patch(book, patch)  # 注意参数顺序
    except PatchError as e:
        errors.append(f"patches[{i}] ({patch.patch_id}): {e.reason}")
```

### S2. `apply_book_patch` 返回类型假设模糊，会导致实现时出错

Phase 2 第 818-823 行假设 `apply_book_patch` 返回一个有 `.book` 和 `.error` 属性的对象：

```python
result = apply_book_patch(current, patch)
if result.error:
    return book, f"patch {patch.patch_id} failed: {result.error}"
current = result.book
```

但 Phase 1 第 437-442 行明确定义：

```python
def apply_book_patch(book: Book, patch: BookPatch) -> Book:
    # Raises PatchError on failure. Returns an immutable copy.
```

`apply_book_patch` 返回 `Book`，失败时抛出 `PatchError`，没有 `.error` 属性。

**建议修复**：将 `apply_patches_sequentially` 改写为 try/except 模式：

```python
def apply_patches_sequentially(patches: list[BookPatch], book: Book) -> tuple[Book, str | None]:
    current = book
    for patch in patches:
        try:
            current = apply_book_patch(current, patch)
        except PatchError as e:
            return book, f"patch {patch.patch_id} failed: {e.reason}"
    return current, None
```

### S3. `save_book` 函数签名假设错误

Phase 2 第 785 行：

```python
save_book(new_book, paths.work_dir)
```

但实际 `save_book`（在 `epubforge/io.py` 第 40 行）接受的 `path` 参数会被 `resolve_book_path` 解析，该函数期望的是 work_dir 或直接的 book.json 路径。同时，`save_book` 不是原子写入——它直接调用 `book_path.write_text()`，而 submit 的原子性语义需要原子写入。

**建议修复**：明确使用 `atomic_write_model(paths.book_path, new_book)` 或确认 `save_book` 的实际行为（它会做 `_validate_editable_book` 检查，这可能是需要的）。如果需要 validation + atomic write，应在计划中说明使用哪个函数并解释原因。

### S4. MemoryPatch 验证逻辑假设了不存在的字段

Phase 2 §5.6（第 634-636 行）的 MemoryPatch 校验代码：

```python
for status in mp.chapter_status:
```

但根据 `memory.py` 第 242-246 行，`MemoryPatch.chapter_status` 的类型是 `list[ChapterStatus]`，这部分是正确的。

然而，同一段代码（第 639-647 行）还检查了 `mp.open_questions`：

```python
for q in mp.open_questions:
    for uid in q.context_uids:
```

这里虽然类型正确，但计划遗漏了对 `mp.conventions` 和 `mp.patterns` 中 UID 引用的校验。`ConventionNote` 有 `evidence_uids` 字段（可能引用 block UID），`PatternNote` 有 `affected_uids` 字段——这些 UID 同样应该检查存在性。

**建议修复**：在 §5.6 中补充对 `mp.conventions[*].evidence_uids` 和 `mp.patterns[*].affected_uids` 的存在性校验。

### S5. submit --apply 步骤 10 中 memory merge 的调用方式与实际 API 不匹配

Phase 2 第 782 行描述：

```python
merge_edit_memory(memory, mp, updated_at=now, updated_by=output.agent_id)
```

但实际 `merge_edit_memory` 签名（`memory.py` 第 536-543 行）：

```python
def merge_edit_memory(
    memory: EditMemory,
    patch: MemoryPatch,
    *,
    updated_at: str,
    updated_by: str,
    question_id_factory: Callable[[], str] | None = None,
) -> MemoryMergeResult:
```

返回的是 `MemoryMergeResult`，其中包含 `.memory`（更新后的 EditMemory）和 `.decisions`。Phase 2 步骤 10 说"逐个 merge（不 abort on error，收集 decisions）"，但实际上 `merge_edit_memory` 接受的是整个 `MemoryPatch`（已包含多个 conventions/patterns/chapter_status/open_questions），不需要逐个调用。

**问题**：Phase 2 的 `AgentOutput.memory_patches` 是 `list[MemoryPatch]`，即多个 MemoryPatch。每个 MemoryPatch 本身已经是多个 convention/pattern 的集合。如果一个 AgentOutput 包含多个 MemoryPatch，需要连续 merge，第二次 merge 的输入 memory 应该是第一次 merge 的输出。

**建议修复**：明确步骤 10 的伪代码：

```python
current_memory = memory
all_decisions = []
for mp in output.memory_patches:
    result = merge_edit_memory(current_memory, mp, updated_at=now, updated_by=output.agent_id)
    current_memory = result.memory
    all_decisions.extend(result.decisions)
new_memory = current_memory
```

---

## 设计建议 (Should consider)

### D1. 文件布局不一致：§1 列了 `commands.py`，但 §4.1 又提到了 `agent_output_commands.py`

§1 的新建文件表列出了 `src/epubforge/editor/commands.py`（PatchCommand 模型）。§4.1 提到新增 `agent_output_commands.py`（CLI Typer 命令组）。两个名字太相似，容易混淆。

**建议**：将 PatchCommand 模型文件改名为 `patch_commands.py`，或将 CLI 命令文件叫 `agent_output_cli.py`，拉开命名距离。

### D2. `add-patch` 时的 scope 一致性检查（§4.6 步骤 4）有逻辑漏洞

§5.7（第 651-664 行）的 scope 校验允许 `patch.scope.chapter_uid == None` 的 patch 通过（即使 `output.chapter_uid` 非 None）。但如果一个 scanner 的 output 绑定到 `ch-001`，它提交了一个 `scope.chapter_uid = None`（book-wide）的 patch，按 §5.7 的代码不会报错（因为条件中 `patch.scope.chapter_uid is not None` 为 False），但 §5.8 scanner 规则只检查 `patch.scope.book_wide`，不检查 `chapter_uid == None` 的情况。

**具体问题**：当 `PatchScope(chapter_uid=None, book_wide=False)` 时，Phase 1 §2.3 说"两者都为 falsy：允许，等同 `book_wide=True`"。这意味着 scanner 可以通过 `scope = PatchScope()` 绕过 book_wide 检查。

**建议修复**：在 §5.7 中明确：当 `output.chapter_uid` 非 None 时，`patch.scope.chapter_uid` **必须**等于 `output.chapter_uid`（不允许 None）。或者在 §5.8 scanner 检查中将 `patch.scope.chapter_uid is None and not patch.scope.book_wide` 也视为越权。

### D3. `archive_agent_output` 的文件操作不是原子的

§6.3（第 846-848 行）使用"先写副本再删原文件"模式：

```python
archive_path.write_text(src.read_text(...), ...)
src.unlink()
```

如果在 `write_text` 后、`src.unlink()` 前进程崩溃，会留下重复文件（原文件和归档副本同时存在）。下次 agent 如果对同一 `output_id` 再执行操作，可能会加载到已 submit 过的 output。

**建议修复**：在归档写入完成后，先在 output 文件中写入一个 `"submitted": true` 标记（原子写入），再删除源文件。或在 `load_agent_output` 时检查同一 output_id 是否已有归档副本。

### D4. reviewer 不允许 `replace_node`，但 §5.8 没有明确

§5.8 reviewer 规则（第 713-723 行）只禁止了 `insert_node`、`delete_node`、`move_node`，但允许 `replace_node`。根据 agentic-improvement.md，reviewer 的角色更接近 scanner——以观察和标注为主。允许 reviewer 使用 `replace_node`（可以完全替换一个 block 的内容和 kind）似乎权限过大。

**建议**：明确 reviewer 是否允许 `replace_node`。如果允许，在 §5.8 reviewer 注释中说明理由。如果不允许，加入 `topology_ops` 集合或单独检查。

### D5. `evidence_refs` 字段只声明不校验

`AgentOutput.evidence_refs` 和 `BookPatch.evidence_refs` 在整个 validate 流程中没有任何校验。虽然 Phase 2 可能不需要 VLM evidence 系统（Phase 9），但至少应该说明这是有意留空的 TODO。

### D6. `begin` 命令应返回完整 output 内容，而非仅 output_id

§4.2 步骤 8 只返回 `{"output_id": "<uuid>", "path": "<absolute_path>"}`。但 agent 可能需要知道 `base_version` 以便后续构造 patch。

**建议**：在 `begin` 的返回中增加 `base_version` 字段，减少 agent 需要额外读取 book.json 的步骤。

### D7. 并发 agent output 没有互斥机制

多个 agent 可以同时对同一 book 创建 output 并各自 add-patch、submit。虽然 `base_version` 校验会捕获过期提交，但如果两个 agent 的 base_version 相同且同时 submit，只有一个能成功（取决于谁先写 book.json）。计划中没有说明这种竞争情况。

在 Git worktree 模式下这不是问题（每个 worktree 是独立的），但 Phase 2 还没有引入 Git worktree（Phase 7），所以 Phase 2-6 期间存在这个窗口期。

**建议**：在 §10 遗留问题中明确记录此限制，说明 Phase 2 仅支持单 agent 串行工作模式，多 agent 并发需要等 Phase 7。

---

## Phase 1 接口假设 (Assumptions about Phase 1 that need verification)

### A1. `validate_book_patch` 签名和返回类型

如 S1 所述。Phase 2 假设返回 `list[str]`，Phase 1 定义为 `-> None`，抛 `PatchError`。**需要修改 Phase 2 调用方式**。

### A2. `apply_book_patch` 返回类型

如 S2 所述。Phase 2 假设返回带 `.book` 和 `.error` 的 result 对象，Phase 1 定义为 `-> Book`，抛 `PatchError`。**需要修改 Phase 2 调用方式**。

### A3. `BookPatch.scope.book_wide` 字段的默认语义

Phase 2 §5.8（第 684 行）检查 `patch.scope.book_wide`，但 Phase 1 §2.3 说"两者都为 falsy 时等同 `book_wide=True`"。这意味着 `PatchScope()` 默认就是 book_wide。Phase 2 的 scanner 权限检查只检查 `patch.scope.book_wide` 为 True 的情况，会漏掉 `PatchScope()` 的默认值。

**需要验证**：Phase 1 的 `PatchScope` 默认行为是否真的是"两者都 falsy = book_wide"，还是在 validator 层面另行处理。如果确认如此，Phase 2 需要调整检查逻辑为：

```python
is_book_wide = patch.scope.book_wide or patch.scope.chapter_uid is None
```

### A4. `BookPatch` 没有 `patch_id` 以外的标识字段

Phase 2 §4.6 步骤 8 输出 `patch_id`：`{"output_id": "...", "patch_id": "...", "patches_count": <n>}`。这假设 `BookPatch` 有 `patch_id` 字段。Phase 1 §2.6 确认有 `patch_id: str`，此处匹配。

### A5. Phase 1 的 `PatchError` 包含 `reason` 和 `patch_id` 属性

Phase 1 §2.2 定义 `PatchError(reason, patch_id)`。Phase 2 的错误包装需要访问 `e.reason`。**已确认匹配**。

### A6. `IRChange` union 中 change 的 `op` 字段值集合

Phase 2 §5.8 scanner 权限检查使用 `change.op != "set_field"`（第 679 行）和 reviewer 检查使用 `topology_ops = {"insert_node", "delete_node", "move_node"}`（第 715 行）。这些值需要与 Phase 1 的 5 种 IRChange discriminator 值完全一致。Phase 1 定义了 `"set_field"`, `"replace_node"`, `"insert_node"`, `"delete_node"`, `"move_node"`——**已确认匹配**。

---

## 测试遗漏 (Missing test scenarios)

### T1. 并发 output 文件冲突

没有测试两个 agent 同时 begin 再各自 submit 的场景。即使 Phase 2 只支持串行，也应有一个测试验证第二个 submit 因 base_version 不匹配而被拒绝。

### T2. `add-*` 命令的幂等性 / 重复调用

没有测试：对同一 output 连续添加两个相同的 patch/note/question 会怎样。当前设计直接 append，不去重。应明确这是预期行为并写测试。

### T3. 极大 output 文件的性能边界

没有测试：一个 output 包含 100+ patches 时 validate 的性能。虽然 Phase 2 可能不需要严格的性能指标，但应至少有一个 smoke test 验证不会因 O(n^2) 的 UID 检查导致超时。

### T4. `submit --apply` 中 memory merge 失败的行为

§8.9 的测试表只覆盖了 book patch apply 失败的回滚，没有覆盖 memory patch merge 失败时的行为。如果第一个 memory_patch merge 成功但第二个失败，book.json 已经被 patches 修改（步骤 9 和 11 在步骤 10 之前完成），此时系统处于不一致状态。

**建议**：要么把 memory merge 放到 book save 之前（全部成功才写 book + memory），要么在测试中明确 memory merge 永远不会失败（但 `merge_edit_memory` 中的 convention conflict 处理可能抛异常）。

### T5. `archive_agent_output` 目标文件已存在

如果因某种原因（进程崩溃后重试）归档目标已存在，当前逻辑会直接覆盖。应测试此场景。

### T6. output 文件被外部篡改或损坏

没有测试：output JSON 文件被手动修改为非法内容后，`load_agent_output` 是否能给出清晰错误信息。

### T7. `add-question` 中 `asked_by` 字段自动填充

§4.4 步骤 3 构建 `OpenQuestion(asked_by=output.agent_id)`。应测试：手动指定一个与 output.agent_id 不同的 asked_by 是否被拒绝（当前设计是强制用 output.agent_id，不允许指定）。

### T8. `submit --apply` 成功后再次 submit 同一 output_id

output 已被移到 archives，再次 submit 应报 "output not found"。应有此测试。

### T9. `PatchScope(chapter_uid=None, book_wide=False)` 的 scanner 权限校验

如 D2 所述，这种 scope 配置在 Phase 1 中被视为等价于 book_wide=True，但 Phase 2 的 scanner 检查只看 `patch.scope.book_wide`。需要一个测试验证 scanner 不能通过默认 scope 绕过权限检查。

### T10. reviewer 提交 `replace_node` 类型的 patch

如 D4 所述，当前 reviewer 权限规则没有限制 `replace_node`。需要一个测试明确预期行为。

---

## 与总体设计的偏差 (Deviations from agentic-improvement.md)

### V1. scanner 必须更新 `chapter_status.read_passes` 的要求被遗漏

agentic-improvement.md §3（Validate AgentOutput before submission）明确要求：

> scanner 完成扫描时必须更新对应 `chapter_status.read_passes`

Phase 2 的 validate 逻辑（§5）中没有对 scanner kind 的 output 检查是否包含对应 chapter 的 `read_passes` 更新。

**建议修复**：在 §5.8 scanner 规则中增加：

```python
if output.kind == "scanner" and output.chapter_uid is not None:
    has_read_pass_update = any(
        cs.chapter_uid == output.chapter_uid and cs.read_passes > 0
        for mp in output.memory_patches
        for cs in mp.chapter_status
    )
    if not has_read_pass_update:
        errors.append("scanner output must include chapter_status.read_passes update")
```

### V2. `submit --stage` 模式完全缺失

agentic-improvement.md §4 定义了两种 submit 模式：

> `--stage`：validate output → 将合法 commands/patches 追加到 staging → 归档 AgentOutput → 不修改 `book.json`  
> `--apply`：validate output → apply patches → apply memory → 归档

Phase 2 §4.9 只实现了 `--apply` 和 dry-run（不传 flag），完全没有 `--stage` 模式。虽然 §10 遗留问题表说"Phase 7 Git workspace 集成时补充"，但 `--stage` 不依赖 Git——它只是写 staging 文件，不修改 book.json。Phase 4 删除旧系统前，`--stage` 可以作为旧 `propose-op` 的替代入口。

**建议**：至少在 §10 遗留问题中说明为什么不在 Phase 2 实现 `--stage`，以及 Phase 4 删除旧 staging 系统后这个模式的定位。

### V3. topology patch 权限模型与总体设计不完全一致

agentic-improvement.md §3 要求：

> topology patch 必须由 supervisor 或明确授权流程提交

但 Phase 2 §5.8 允许 fixer 提交所有类型的 BookPatch（包括 topology 操作如 `insert_node`/`delete_node`/`move_node`），只要 fixer 的 `chapter_uid` 为 None（全书 fixer）就没有任何 scope 限制。

这与总体设计"topology patch 必须由 supervisor 或明确授权流程提交"有偏差。当前 Phase 2 的 fixer 权限过于宽泛。

**建议**：在 §5.8 fixer 部分增加注释说明偏差原因（例如："Phase 2 暂不限制 fixer topology 权限，Phase 3+ 可通过 PatchCommand 编译阶段收紧"），或直接限制 fixer 的 topology 操作只能通过 PatchCommand（而非直接 BookPatch）提交。

### V4. 总体设计提到 submit 成功后应"续租"，Phase 2 已正确跳过

agentic-improvement.md §4 说 `--apply` 成功后续租。但 D1 已决定移除 lease 系统，所以 Phase 2 没有续租逻辑是正确的。但 agentic-improvement.md §4 中的这句话应该标注为已过时，避免后续实施者混淆。

---

## 总结

Phase 2 计划整体结构清晰，CLI 命令设计合理，validate 规则覆盖面广，测试矩阵丰富。但存在 **3 个必须修复的接口不匹配问题**（S1/S2/S5），会直接导致实现时编译/运行失败：

1. `validate_book_patch` 的签名和返回类型与 Phase 1 不一致。
2. `apply_book_patch` 的返回类型与 Phase 1 不一致。
3. `merge_edit_memory` 的调用方式描述不精确，且多 MemoryPatch 的连续 merge 逻辑缺失。

此外，`PatchScope` 默认值的 book_wide 语义漏洞（D2/A3）会导致 scanner 权限绕过，属于安全风险。

**结论**：计划需要一轮修订后再进入实现。修订范围不大——主要是修正 Phase 1 接口调用处的代码示例、补充 scope 检查的边界条件、明确 submit --apply 中 book save 与 memory merge 的顺序。预计修订工作量 1-2 小时。
