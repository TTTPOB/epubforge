# epubforge 修复方案（v3 收口修订版）

## 本轮修订目的

- 主计划只保留经多轮审查后仍成立的“类型1：无可争议的错误，需要修复”。
- 本轮 beads 追踪 epic：`epubforge-k6c`
- 文档回写 gate：`epubforge-k6c.9`
- 已降级的问题统一移到文末；它们要么只是表述不精确，要么是回归风险/覆盖不足/待补证，不再作为当前修复计划的 gating 条件。

当前已建的类型1子 issue：

- `epubforge-k6c.1` `fix-plan-v3/1.1: run-script 拒绝路径测试错误地断言 stderr`
- `epubforge-k6c.2` `fix-plan-v3/1.1: 删除或替换不可稳定触发的 allocate_script_path assertion 测试`
- `epubforge-k6c.3` `fix-plan-v3/1.2: import-legacy/init 测试使用错误的 edit log 文件名`
- `epubforge-k6c.4` `fix-plan-v3/1.5: table merge detector 的遍历条件无法覆盖 orphan_continuation`
- `epubforge-k6c.5` `fix-plan-v3/1.5: assembler 阶段无法记录 constituent_block_uids`
- `epubforge-k6c.6` `fix-plan-v3/1.6: memory merge hook 引用了不存在的 apply_memory_patch`
- `epubforge-k6c.7` `fix-plan-v3/1.6: failure retry 语义与 clear_staging 方案自相矛盾`
- `epubforge-k6c.8` `fix-plan-v3/1.6: memory_patches 同时写入 OpEnvelope 与 sidecar 导致双写`

## 修复顺序与 PR 划分

先修类型1，再继续原本的功能改动。推荐拆成 8 个 PR：

| PR | 范围 | 关联 beads | 说明 |
|----|------|------------|------|
| PR-A | `1.1 + 1.7` | `epubforge-k6c.1` `epubforge-k6c.2` | 先把 `run-script` 的测试契约修正到真实 CLI 行为；`propose-op` 全或无保持独立推进。 |
| PR-B | `1.2` | `epubforge-k6c.3` | 先修 log 文件名与测试断言，再决定是否继续做 `write_initial_state` 的职责收敛。 |
| PR-C | `1.5a` | `epubforge-k6c.4` | 先修 detector 遍历面，让 `orphan_continuation` 可被真实检测。 |
| PR-D | `1.5b` | `epubforge-k6c.5` | 重写 merge provenance 设计，去掉“assembler 阶段拿到 constituent 原 uid”的错误前提。 |
| PR-E | `1.6a` | `epubforge-k6c.8` | 先确定 `memory_patches` 的单一真值来源；本版建议放弃 sidecar，改为 envelope-only。 |
| PR-F | `1.6b + 1.6c` | `epubforge-k6c.6` `epubforge-k6c.7` | 在 PR-E 之后再接真实 merge hook，并把失败语义写成单一方案。 |
| PR-G | `1.3` | 无类型1阻塞 | `_cjk_join` 可以保留为纯函数改动，等核心 blocker 清完后并行发。 |
| PR-H | `1.4` | 无类型1阻塞 | 表格标题/来源 hard-cut 继续排在 `1.5` 稳定之后；测试策略已修订。 |

显式依赖：

- `epubforge-k6c.8` block `epubforge-k6c.6`
- `epubforge-k6c.8` block `epubforge-k6c.7`
- 全部 8 个类型1子 issue block `epubforge-k6c.9`

---

## 1.1 `run-script` 沙箱（修订）

**现状**：`run_script` 仍未校验脚本是否落在 `paths.scratch_dir` 内，相对路径仍以 `PROJECT_ROOT` 为基准，未拒 symlink 逃逸或 `..`。

**本轮必须修正的类型1问题**：

1. 拒绝路径测试不能再断言 `stderr`。editor CLI 的错误契约是 `CommandError -> stdout JSON`。
2. 删掉 `test_allocate_script_path_assertion` 这一不可稳定触发的测试要求，改测真实输入面。

**修改文件**：

- `src/epubforge/editor/scratch.py`
  - `run_script()`：改为 `_resolve_within_scratch(raw, scratch_dir)`。
  - `allocate_script_path()`：函数内部的防御式 `assert` 语句可以保留，但**不在测试文件中**为其单独构造 `test_allocate_script_path_assertion` 用例——该函数没有可注入外部路径的接口，该断言在当前 API 下无法被外部合法触发。
- `src/epubforge/editor/tool_surface.py`
  - `run_run_script()`：将 path validation failure 统一翻成 `CommandError`。
- `tests/test_editor_tool_surface.py`
  - 更新拒绝路径用例，全部断言 `returncode != 0` 且 stdout JSON 的 `error` 字段包含目标信息。

**修改要点**：

- `_resolve_within_scratch(raw, scratch_dir)`：
  - `Path(raw).expanduser()`
  - 相对路径挂到 `scratch_dir`
  - 拒绝 `suffix != ".py"`
  - `resolve(strict=True)` 解 symlink
  - `is_relative_to(scratch_dir.resolve())` 校验
  - 失败抛 `ValueError`
- `run_script()` 删除旧的 `PROJECT_ROOT / script_path` 回退逻辑。
- `tool_surface` 保持现有 CLI 契约：path validation failure 走 `CommandError`，因此错误信息出现在 stdout JSON，而不是 stderr。

**测试**：

以下拒绝路径用例均断言 `returncode != 0`，并解析 stdout JSON，检查 `error` 字段包含目标信息（如 `must reside under scratch_dir`）。**不要断言 stderr**，因为 editor CLI 的错误契约是 `CommandError -> stdout JSON`，拒绝路径不写 stderr。

- `test_run_script_rejects_absolute_outside_scratch`
- `test_run_script_rejects_dotdot_escape`
- `test_run_script_rejects_symlink_escape`
- `test_run_script_rejects_non_py_suffix`
- `test_run_script_accepts_relative_inside_scratch`

删除：

- `test_allocate_script_path_assertion`

**兼容性 / 文档**：

- `docs/agentic-editing-howto.md` 的 `run-script` 段补一句：“`--exec` 仅接受 `scratch_dir` 内的 `.py` 文件；拒绝路径错误会通过 stdout JSON 返回。”

---

## 1.2 `write_initial_state` 双写解耦（修订）

**现状**：

- `write_initial_state()` 内部确实会 `save_book(...)`。
- `run_import_legacy()` 在生成 noop baseline 后还会对 `imported_book` 再次 `save_book(...)`。
- `run_init()` 当前没有额外第二次 `save_book(...)`。

**本轮必须修正的类型1问题**：

- 所有测试和文档都必须改用真实的 edit log 文件名：`edit_log.jsonl`，或者直接使用 `paths.current_log_path`。

**原 refactor 仍可继续，但正文已校正前提**：

- `write_initial_state` 是否继续收敛为“只建目录骨架 + 写 meta/memory/leases + 清空 log/staging，不落 `book.json`”，可以保留为 PR-B 的功能改动。
- 但相关描述不能再写成“`init` / `import-legacy` 的 caller 都会再次 save”，因为当前只有 `import-legacy` 真正存在第二次落盘。

**修改文件**：

- `src/epubforge/editor/state.py`
- `src/epubforge/editor/tool_surface.py`
- `tests/test_editor_tool_surface.py`

**测试**：

- `test_write_initial_state_does_not_touch_book_json`
- `test_run_init_persists_book`
- `test_run_import_legacy_persists_book_and_log`

其中日志断言统一改为：

- `paths.current_log_path.exists() is True`

不要硬编码文件名（正确常量为 `edit_log.jsonl`，通过 `CURRENT_LOG` 定义）。

---

## 1.3 `_cjk_join`

这一项本轮未发现类型1问题，原计划可以保留。

建议仍按原方案推进：

- 扩展 CJK 范围到扩展汉字、全角符号、平假名/片假名、韩文 Hangul
- 处理 Latin hyphen continuation
- 在 `tests/test_assembler.py` 中补充独立 `TestCjkJoin`

这一项与当前类型1清理正交，可在核心 blocker 清理后单独发。

---

## 1.4 表格标题 / 来源 → 硬切（修订）

这一项本轮没有保留类型1问题，但测试策略需要修正。

**保留目标**：

- 删除 `assembler._absorb_table_text` 的中文 regex fallback
- 把表格标题 / 来源完全交给 VLM 分类

**修订后的测试策略**：

- 保留黑盒行为测试：
  - `test_assemble_respects_vlm_table_title_caption`
- 删除源码 grep 测试：
  - 不再使用 `test_no_table_title_regex_remaining`

**原因**：

- 该测试只能锁定实现细节，不证明行为正确。
- `1.4` 的真正行为源头是 `src/epubforge/llm/prompts.py` 中的 `VLM_SYSTEM` 规则，而不是“源码里是否还出现某个 regex 变量名”。

**非 CJK 回归样本**：

- 继续建议后续逐步补充 `en_academic.pdf` / `jp_nonfiction.pdf` / `multicolumn_journal.pdf`
- 但不再把这些样本作为当前文档回写的 gating 条件

---

## 1.5 table merge audit detector（修订）

### 1.5a detector 输入面修正（`epubforge-k6c.4`）

**现状（已校正）**：

- 现有 `detect_table_issues` 不只覆盖 `table.double_tbody`，还会报 `table.split_row_suspected` 与 `table.column_count_mismatch`。
- 新增 merge detector 的目标不是替代它，而是补上 cross-page merge 的专属审计。

**本轮必须修正的类型1问题**：

- detector 不能只遍历 `multi_page=True` 的表；否则永远看不到“找不到前驱表、但 `continuation=True` 仍然留在结果里”的 orphan continuation case。

**修订后的 detector 入口**：

- 遍历所有 `Table`
- 对两类状态分别检查：
  - `multi_page=True`：merge 后结构审计
  - `continuation=True`：孤儿 continuation 审计

**建议 issue codes**：

- `table.merge_width_drift`
- `table.merge_header_reintroduced`
- `table.merge_orphan_continuation`
- `table.merge_record_incomplete`

备注：原来的 `table.merge_orphan_multipage` 命名可以保留或调整，但不要再把“只要 `merge_record is None` 就报错”写死成默认行为，见文末降级项。

### 1.5b merge provenance 结构修正（`epubforge-k6c.5`）

**本轮必须修正的类型1问题**：

- 不能再要求 assembler 在 `_merge_continued_tables()` 阶段记录 `constituent_block_uids`。
- 因为这个阶段拿到的块尚未完成 `initialize_book_state()`，uid 还不是稳定真值。

**修订后的设计**：

- `Table.merge_record` 只记录当前阶段真实可得的信息，例如：
  - `segment_html`
  - `segment_pages`
  - `segment_order`
  - `tbody_boundaries`
  - `column_widths`
- 不承诺“恢复 constituent 原 uid”。
- 若后续确实需要拆回多个块：
  - 由 `SplitMergedTable` 在 apply 阶段生成新的 runtime uid
  - 或者把 merge provenance 的记录点后移到 uid 已初始化之后

**`SplitMergedTable` 修订建议**：

不要再以 `constituent_block_uids` 作为 assembler 阶段的必填输入。更合理的方向是：

```text
op: "split_merged_table"
block_uid: str
segment_html: list[str]
segment_pages: list[int]
multi_page_was: bool
```

如果后续需要 `new_block_uids`，应在 apply 时生成，而不是伪装成“恢复原 uid”。

**测试**：

- `tests/test_audit_table_merge.py`
  - `test_detect_width_drift`：fixture 为 `multi_page=True` 的 Table，列宽偏移超过阈值
  - `test_detect_header_reintroduced`：fixture 为 `multi_page=True` 的 Table，合并后仍含重复 `<thead>`
  - `test_detect_orphan_continuation`：fixture 为 `continuation=True` 且 `multi_page=False` 的 Table（即 assembler `_merge_continued_tables()` 找不到前驱表、原样保留的孤儿块）；不可使用 `multi_page=True` 的 Table 作为输入，否则永远触发不到 orphan 路径
- `tests/test_editor_ops.py`
  - 只验证修订后 op schema 的长度/必填字段约束
- `tests/test_editor_apply.py`
  - 验证拆分后新块顺序与 payload 恢复正确
  - 不再把“恢复 constituent 原 uid”当作默认断言

---

## 1.6 `memory_patches` 嵌入 propose / apply-queue（修订）

这一节是本轮改动最大的地方。原方案的 sidecar 双文件设计已经被判定存在类型1矛盾，本版直接改方向。

### 1.6a 单一真值来源（`epubforge-k6c.8`）

**现状（已校正）**：

- `OpEnvelope` 当前未定义 `memory_patches`
- 如果 agent 真的把 `memory_patches` 塞进 envelope，当前行为不是“静默丢弃”，而是 validation error

**修订决策**：

- 放弃 `staging_memory.jsonl`
- 不再走“双文件 staging”方案
- 采用 **envelope-only**：
  - `OpEnvelope` 增 `memory_patches: list[MemoryPatch] | None = None`
  - `append_staging()` 仍然只写 `staging.jsonl`
  - 不再引入额外 sidecar durable store

这样可以一次性消除：

- 双写
- 双真值来源
- “全或无”同时作用两份文件的额外复杂度

### 1.6b 真实 merge hook（`epubforge-k6c.6`）

**本轮必须修正的类型1问题**：

- 不再引用不存在的 `apply_memory_patch`
- 统一使用 `merge_edit_memory()`

**修订后的 apply-queue 语义**：

- `apply_envelope(...)` 先算出 `book` 变更
- 若 envelope 带 `memory_patches`，逐个调用 `merge_edit_memory(...)`
- 两部分都成功后，才：
  - append accepted log
  - save `book.json`
  - save `memory.json`

### 1.6c 失败语义（`epubforge-k6c.7`）

**修订决策**：

- 删除“patch 留到下轮重试（不回滚已 accept 的 envelope）”这一说法
- 改成单一语义：
  - `memory_patches` merge 失败 == 整个 envelope apply 失败
  - 记录 rejected log
  - 不写 accepted log
  - 不保存变更后的 book/memory

这样可以避免：

- 部分 accept / 部分失败
- staging cleanup 的歧义
- retry side channel 的额外状态机

### 1.6 测试（修订）

- `tests/test_editor_ops.py`
  - `test_op_envelope_accepts_memory_patches`
  - `test_op_envelope_rejects_unknown_patch_field`
- `tests/test_editor_tool_surface.py`
  - `test_propose_op_accepts_memory_patches_in_envelope`
  - `test_apply_queue_merges_memory_patch_via_merge_edit_memory`
  - `test_apply_queue_rejects_envelope_when_memory_merge_fails`

删除：

- 所有围绕 `staging_memory.jsonl` 的测试和文档设计

---

## 1.7 `propose-op` 全或无

这一项仍然保留，且在本轮修订后反而更简单。

因为 `1.6` 已放弃 sidecar 双文件，`1.7` 只需保证：

- 任意一条 envelope 校验失败 -> 整批拒收，不写 `staging.jsonl`
- 全部合法 -> 一次性 append 到 `staging.jsonl`

无需再额外讨论：

- `staging_memory.jsonl` 与 `staging.jsonl` 的双文件原子性

---

## 跨条目共同变化（修订）

1. **`OpEnvelope` schema 升级**（PR-E / PR-F）：只新增 `memory_patches: list[MemoryPatch] | None = None`；不引入 sidecar staging。
2. **`Table` IR schema 升级**（PR-D / 1.5）：`merge_record` 仅承载 assembler 当前真实可得的信息；不承诺 `constituent_block_uids`。
3. **`AuditIssue.payload`**：可以继续作为 detector 内部结构化信息，但不再把“必须穿透 doctor/prompt 全链路”作为当前 gating 条件。
4. **staging 模型**：只保留 `staging.jsonl`，`1.7` 的全或无只作用这一份文件。
5. **prompt 文档**：各 PR 同步更新 `docs/agentic-editing-howto.md` / `docs/table-audit-process.md`；不堆到最后。

---

## 用户/上游需要决策的剩余开放问题

1. **非 CJK 回归样本补充计划**：建议逐步采集 `en_academic.pdf` / `jp_nonfiction.pdf` / `multicolumn_journal.pdf`，但不作当前 gate。
2. **`Table.merge_record` 记录粒度**：正文已明确“不要在 assembler 阶段记录 constituent 原 uid”，但 `segment_html/page/order` 是否足够，仍可在 PR-D 中微调。
3. **`memory_patches` 与更高层 prompt/wrapper 的接口**：本版只先保证 tool-surface 内部计划自洽；若后续发现上游 agent wrapper 仍按其他 JSON 形状投喂，再另开 issue 处理。

---

## 降级项（移到文件尾，不作为当前 gate）

下面这些问题在审查中被认为“值得记录，但不够格列为当前类型1 blocker”：

1. **`1.2` 原文把 `init` 也写成双写 caller**  
   这属于现状表述不精确，不会直接导致计划落地失败。正文已改正为“只有 `import-legacy` 当前存在第二次 `save_book(...)`”。

2. **`1.5` 原文写“现有 `detect_table_issues` 只覆盖 `table.double_tbody`”**  
   这是现状描述错误，不是阻断实现的类型1问题。正文已改正为“已覆盖 `double_tbody` / `split_row_suspected` / `column_count_mismatch`”。

3. **`1.6` 原文写“`memory_patches` 会被 `model_validate_json` 默默丢弃”**  
   实际当前行为是 validation error。这个错误已在正文更正，但它本身不足以单独阻断改动。

4. **`1.4` 的 `test_no_table_title_regex_remaining`**  
   这是低价值、实现耦合的测试，不是类型1 blocker。正文已删除该测试方案。

5. **`1.5` 若把 `multi_page=True && merge_record is None` 一律报 orphan，可能误报 legacy 数据**  
   这是回归风险，值得后续单独评估，但不属于“按当前计划原文一定直接失败”的类型1。

6. **`1.5` 的 `AuditIssue.payload` 目前不会结构化穿透 doctor/prompt 全链路**  
   这会影响后续 agent 体验与自动修复便利性，但不阻断 detector 最小实现。已从当前 gate 中移除。

7. **`1.6` prompt/CLI 契约可能不匹配**  
   当前仓内证据不闭合：`editor/prompts.py` 的输出形状与 `propose-op` 的 stdin 形状确实不同，但还缺中间 orchestrator/wrapper 的实现证据。先列为待补证风险，不纳入本轮类型1 gate。
