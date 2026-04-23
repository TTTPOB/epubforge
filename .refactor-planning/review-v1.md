# Review of refactor-plan-v1.md — Round 1

**Verdict**: NEEDS_REVISION  (2 Blocker + 4 Major)

## Strong Points
1. 绝大多数 file:line 引用都能定位到位（R1/R2/R4/R6/R10/R13 的行号实测全部吻合）
2. commit 分组合理：机械重构 → 语义修正 → 文档 顺序让风险递增
3. §3.1（放弃 pydantic-settings）、§3.5（放弃 apply-queue batch atomicity）判断正确

## Issues

### [Blocker] B1 — R3 的 `CLEAN_SYSTEM` 删除方案危险，须先切分片段
**问题**: §R3 把"4 个 `_*_RULES` 片段是否共享"的判断推给执行者。
**证据**: `src/epubforge/llm/prompts.py:273-279` 四处 `{_LINE_BREAK_RULES}` / `{_PARAGRAPH_BOUNDARY_RULES}` / `{_POETRY_RULES}` / `{_CROSS_PAGE_CONT_RULES}` 与 CLEAN_SYSTEM（line 182-188）对称出现——**这 4 个片段同时被 VLM_SYSTEM 引用**。
**建议**: R3 方案改为明确"**删除 `CLEAN_SYSTEM` 字符串定义（line 160-~250 区块），保留 4 个 `_*_RULES` 片段不动**"。§7.1 的待调研点降级为"已确认：4 片段必须保留"。

### [Blocker] B2 — R9 的 `python -m` kebab-case 前提错误
**问题**: §R9 和 §7.2 断言 "`apply-queue` 非法 identifier，`importlib` 会拒"，实际错误。**实测** `uv run python -m epubforge.editor.apply-queue` 正常加载；`tests/test_editor_tool_surface.py:225,232` 的 subprocess 调用就是 `python -m epubforge.editor.propose-op` / `apply-queue` 并 CI 通过。Python `-m` 对**脚本式子模块**（末段）只要求文件存在，不做 identifier 校验。
**证据**: 实测输出 `{"error": "the following arguments are required: work"}`；`tests/test_editor_tool_surface.py:225-232`。
**建议**: R9 仍可做，但重写动机——不是"当前用法破损"而是"便于未来跨脚本 import + 统一风格"；删除 §7.2 内容。重命名后必须把测试文件里 subprocess 字符串（`propose-op` → `propose_op` 等）**一并**改掉（计划未提测试字符串同步）。

### [Major] M1 — 计划遗漏 Report 4 #2 (audit HTML regex 重复)
**问题**: Report 4 明确列"严重 #2：HTML 表格 regex 在两个 audit 模块重复"，计划 §3 也**没**说为什么不做。
**证据**: `audit/tables.py:13-17`（ROW_RE/TBODY_RE/CELL_RE/COLSPAN_RE/ROWSPAN_RE）和 `audit/table_merge.py:19-23`。其中 `ROW_RE`/`TBODY_RE`/`COLSPAN_RE` 三个完全重复（`CELL_RE` 两处稍有差异）。
**建议**: 补一个 R14：把完全重复的 regex 抽到 `audit/_html.py`，不处理有差异的 `CELL_RE`。归入 Commit 2。

### [Major] M2 — R4 dispatch 的类型标注方案不可行，须主计划中明示替代
**问题**: §7.5 把类型问题丢给执行者实测，但主体代码直接用 `dict[type[EditOp], Callable[[Book, EditOp, str], Book]]`——这是函数参数逆变问题，mypy/pyrefly 会报错。
**建议**: R4 示例改为"**handler 签名全部用 `Book, EditOp, str`（宽类型），函数体内 `assert isinstance(op, SetRole)` 做 narrow**"。备选：`Callable[..., Book]`（弱类型）。必须在主计划里定下来。

### [Major] M3 — R6 改 CLI `--ttl` 可能破坏内部调用点
**问题**: R6 建议让 `leases.py:106` 的 `ttl: int = 1800` "不设默认值或标注必填"。但 `LeaseState.acquire_chapter` 不仅被 CLI 调，还被内部其它地方调。如果改必填，所有内部调用点都要补参数。
**证据**: `leases.py:100-108` 和 `141-148`；`LeaseState` 在 `state.py`、`apply.py`、`tool_surface.py` 都被用到。
**建议**: R6 明确"**保留 `acquire_chapter`/`acquire_book_exclusive` 的 `ttl` 默认值作为最终兜底，只消除 CLI 层的独立硬编码**"。CLI 读 Config 覆盖，leases 方法默认值保持——双保险无回归风险。

### [Major] M4 — R5 "等价 concat" 叙述不准确
**问题**: §R5 说 "`_join_text` 接受 `"cjk"` 但 `return "".join(parts)`——与 `"concat"` 完全相同"。实际 `"cjk"` 走的是**默认 fallthrough**，是双层问题：既是功能缺失也是静默 fallback。
**证据**: `apply.py:196-201`:
```
if join == "newline": return "\n".join(parts)
if join == "concat": return "".join(parts)
return "".join(parts)
```
**建议**: 修正叙述为"'cjk' 走隐式 catch-all 的 concat 分支"。新实现用 `match`/`Literal` exhaustive match，或 if-elif 加 `raise AssertionError` 兜底。

### [Minor] m1 — R8 的 fixture 文件数量前后不一致
§R8 正文写"9 个"但列 10 条。改为"10"。

### [Minor] m2 — R13 "崩溃半写文件"风险表述过强
Python `pathlib.write_text` 在 Linux ext4 journal 下不会有 torn write。动机应为"一致性——其他地方都用 `atomic_write_*`，唯独 log.py 3 处不是"。修复行为不变。

### [Minor] m3 — R7 遗漏 `EPUBFORGE_EDITOR_NOW` 等 scratch env vars
应在 AGENTS.md 重写中补齐 `EPUBFORGE_EDITOR_NOW`、`EPUBFORGE_PROJECT_ROOT`、`EPUBFORGE_WORK_DIR`、`EPUBFORGE_EDIT_STATE_DIR`（`editor/scratch.py:28,99-101`），或明确标注"test-only / subprocess-injection"。

## 遗漏的重构项
1. audit HTML regex 抽取（见 M1）——应加 R14。
2. `extract.py` 裸 json.loads/dumps（Report 4 #3）——计划放弃，理由勉强；建议至少补一行说明为什么 log.py 修（R13）而 extract.py 不修。
3. `EditorModel` 等 4 基类删除前应 grep 外部引用（实测 `__init__.py` 未导出，但未做全项目 grep 收尾）。

## 决策点再审
- **D1 `StrictModel`**：支持。
- **D2 彻底删除 CLEAN_SYSTEM**：支持。但"彻底删除"仅指字符串本身，不含 `_*_RULES` 片段（见 B1）。
- **D3 只改 `_apply_op`**：支持。
- **D4 新建 `text_utils.py`**：支持。
- **D5 保留 dataclass**：支持。
- **D6 改为 Stage 5**：支持。但同步点不只 2 处——`pipeline.py:119` console 字符串、`pipeline.py:120` stage_timer "8 build"、`cli.py:151` docstring "Stage 8" 都要改。
- **D7 module-level helper vs fixture**：**弱反对原推荐**。pytest fixture (选项 A) 更符合 pytest 惯例，测试 `def test_x(prov)` 比 `from tests.conftest import make_provenance` 对新读者更直观。切换成本等价。
- **D8 仅改文件名**：支持。

## 新事实（reviewer 复核时发现）
1. **`_cjk_join` 对首尾空白有 `lstrip/rstrip` 副作用**（`assembler.py:610-611`）：`MergeBlocks(join="cjk")` 接收 `["hello ", " world"]` 得到 `"hello world"`（一个空格）而非两个。R5 抽取时将继承这一行为。建议 R5 加 note 或提供 `strip=True/False` 参数。
2. **`apply_envelope` 的事务性靠 deep copy 支撑**（`apply.py:1065` 附近）：Report 3 #4 的"memory_patches 事务依赖 deep copy"未进入计划也未放入 §3 放弃列表。建议 R7 AGENTS.md 新节明确此 invariant。
3. **`Book.version` 语义"op 日志版本"应在 AGENTS.md 新节文档化**（Report 3 #8），§3.7 放弃重命名没问题，但文档要补一句。

## Final Directive
**NEEDS_REVISION**

v2 必须处理：**B1, B2, M1, M2, M3, M4**。
Minor (m1/m2/m3) 和 D7 反对可吸收或不吸收，不阻塞 v2。
