# epubforge Refactor Plan v2

**Status**: revised, awaiting user decisions
**Scope**: 优雅重构——消除重复、统一命名、清理死代码、修正文档。无功能性变更。
**Authorization**: 用户已授权 break 向后兼容（zxgb fixture 已 build，无历史包袱）。
**Changelog vs v1**: 响应 review-v1 的 B1/B2/M1/M2/M3/M4，新增 R14（audit HTML regex 抽取），D7 推荐改为 "pytest fixture"，修正 R3/R9 的错误叙述，清理 §7 已经确认的调研点。

---

## 0. Response to Review v1

| 编号 | 类型 | 处理 |
|---|---|---|
| B1 | Blocker | **接受** → R3 正文明确"保留 4 个 `_*_RULES` 片段"，§7.1 从"待调研"移至"已确认事实"。已 grep 验证：`prompts.py:273-279` 由 `VLM_SYSTEM` 引用 4 个片段。 |
| B2 | Blocker | **接受** → R9 动机改写，删除"`python -m` 拒载 kebab-case"错误叙述；§7.2 待调研点废除；R9 工作清单显式加入"同步更新 `tests/test_editor_tool_surface.py` 19 处 subprocess 字符串 + `docs/usage.md` / `docs/agentic-editing-howto.md` / `editor/prompts.py` / `editor/__main__.py`"。 |
| M1 | Major | **接受** → 新增 R14（audit HTML regex 抽取到 `audit/_html.py`），归入 Commit 2。 |
| M2 | Major | **接受** → R4 "建议方案" 更新为 "handler 签名统一 `(Book, EditOp, str) -> Book`，函数体内 `assert isinstance(op, SetRole)` 做 narrow"，§7.5 相应删除。 |
| M3 | Major | **接受** → R6 正文修正：`leases.py` 两个方法的 `ttl` 默认值（1800/300）**保留作为最终兜底**；只消除 `tool_surface.py` 两处 CLI argparse 的独立硬编码 `default=`，改为运行时 `load_config()`。 |
| M4 | Major | **接受** → R5 问题叙述改写为 "'cjk' 走隐式 catch-all 的 concat 分支"；"建议方案" 加入 `match`/exhaustive 兜底（`raise AssertionError` 或 `assert_never`）。 |
| m1 | Minor | **接受** → R8 正文 "9 个" 改为 "10 个"。 |
| m2 | Minor | **接受** → R13 "问题描述" 改写为 "一致性 — 同包其余 3 处用 `atomic_write_*`，log.py 3 处裸 `write_text` 是瑕疵"，去掉"半写文件"措辞。 |
| m3 | Minor | **接受** → R7 AGENTS.md 环境变量清单新增 `EPUBFORGE_EDITOR_NOW`/`EPUBFORGE_PROJECT_ROOT`/`EPUBFORGE_WORK_DIR`/`EPUBFORGE_EDIT_STATE_DIR`（标 "test-only / scratch subprocess injection"）。 |
| D7 反对 | 决策点再审 | **接受 reviewer 意见** → D7 推荐从 "B. module-level helper" 改为 "A. pytest fixture"。理由采纳——pytest 惯例、读者体感、迁移成本对等。R8 "建议方案" 相应调整。 |
| 新事实 1 | `_cjk_join` 的 lstrip/rstrip 副作用 | **接受** → R5 "建议方案" 补一行 note，明确保留 strip 语义（new n-ary `cjk_join` 调用 `cjk_join_pair` 时自然继承）。不加参数。 |
| 新事实 2 | `apply_envelope` 事务性靠 deep copy | **接受** → R7 AGENTS.md "Editor 子系统" 节新增 invariant 文档化一行。 |
| 新事实 3 | `Book.version` 应在 AGENTS.md 说明是"op 日志版本" | **接受** → R7 Semantic IR 节补一句 "注：`Book.version` 是 op 日志版本，每次 `apply_envelope` +1，不是 IR schema 版本"。 |
| Review §遗漏 #3 | 删 4 基类前 grep 外部引用 | **接受** → R2 "风险" 节加一条 commit-前 grep 检查 `EditorModel|MemoryModel|DoctorModel|LeaseModel` 确保无外部 re-export。 |

---

## 1. Executive Summary

本轮重构聚焦 4 条主线：(a) **editor 子包内部整洁**——消除 4 份复制的 `_require_non_empty`/`_validate_uuid4`/`_validate_utc_iso_timestamp` 和 4 个等价 `extra="forbid"` 基类，并用注册表替换 `_apply_op` 的 16 路 `isinstance` 分支；(b) **config 统一**——删除死配置节（`[proofread]`、`[footnote_verify]`）、将 CLI 层的 `editor_lease_ttl_seconds`/`book_exclusive_ttl_seconds` 独立硬编码 `default=` 改为运行时读 Config（保留 `leases.py` 两个方法的兜底默认值），消除硬编码 `vlm_max_tokens=16384`；(c) **清除死代码**——`CLEAN_SYSTEM` 字符串 + `TocRefineOutput`/`CleanOutput`/`TocRefineItem`/`CleanBlock`，全部无 src import；保留被 `VLM_SYSTEM` 共用的 4 个 `_*_RULES` 片段；(d) **文档同步**——AGENTS.md 对 editor/audit/BookMemory 零描述，`--from max=4` 与文档冲突，必须重写。

预计 **14** 个工作项、~850 行代码改动（多为删除），覆盖 7 个文件为主，外加 9 个 editor CLI 文件重命名。不引入新依赖。

---

## 2. 重构工作项

按"高价值低风险优先"排序。

---

#### [R1] 抽取 editor 共享 validator 到 `editor/_validators.py`

**问题描述**：`_require_non_empty` 在 4 个文件各有一份独立实现（行为一致）：`editor/ops.py:35-38`、`editor/memory.py:36-39`、`editor/doctor.py:20-23`、`editor/leases.py:11-14`。`_validate_uuid4` 和 `_validate_utc_iso_timestamp` 在 `ops.py:41-63` 和 `memory.py:42-64` 双份。`leases.py:17-27` 另有 `_parse_utc_iso`——语义略异（返回 `datetime` 而非 `str`），但共享同样的正则与错误路径。

**建议方案**：
新建 `src/epubforge/editor/_validators.py`，导出：
- `require_non_empty(value: str, *, field_name: str) -> str`
- `validate_uuid4(value: str, *, field_name: str) -> str`
- `validate_utc_iso_timestamp(value: str, *, field_name: str) -> str`
- `parse_utc_iso(value: str, *, field_name: str) -> datetime`（保留 leases 专用语义）

4 个现有模块改为 `from epubforge.editor._validators import ...`，删除各自本地定义（总计删除约 80 行）。命名去掉前导下划线——它们现在是包内公用，而非文件私有。

**影响面**：`ops.py`、`memory.py`、`doctor.py`、`leases.py`；新增 `_validators.py`（~60 行）。无外部接口变化。

**人决策点**：无（几何等价替换）。

**风险**：低。Pydantic `field_validator` 闭包内调用保持不变。测试覆盖已有。

**依赖**：无。

---

#### [R2] 统一 editor 基类为 `StrictModel`

**问题描述**：四个等价基类：`EditorModel`（`ops.py:75`）、`MemoryModel`（`memory.py:32`）、`DoctorModel`（`doctor.py:16`）、`LeaseModel`（`leases.py:41`），实现全部都是 `class X(BaseModel): model_config = ConfigDict(extra="forbid")`。

**建议方案**：
在 R1 同文件 `editor/_validators.py` 或独立新文件 `editor/_models.py` 中定义：
```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
```
4 个模块 import 并继承 `StrictModel`，删除本地 4 个基类定义。`EditorModel`/`MemoryModel` 等名称不再使用（`__init__.py` 未导出它们）。

**影响面**：4 个 editor 文件，每个文件 3 行增减。测试/外部接口无影响。

**人决策点**：
- **D1**: 基类命名（详见 §6）。

**风险**：低。**commit 前强制检查**：`grep -rn -E 'EditorModel|MemoryModel|DoctorModel|LeaseModel' src/ tests/` 必须仅在被删行和本次新增 import 中出现，确认无外部使用（当前 `__init__.py` 未导出已有初步佐证）。

**依赖**：可与 R1 合并。

---

#### [R3] 删除死代码 `CLEAN_SYSTEM` / `TocRefineOutput` / `CleanOutput` 等

**问题描述**：grep 验证结果——
- `llm/prompts.py:160-223` 定义的 `CLEAN_SYSTEM`，在 `src/` 中**零 import**。
- `ir/semantic.py:186-194`（`TocRefineItem`、`TocRefineOutput`）、`ir/semantic.py:199-209`（`CleanBlock`、`CleanOutput`），均无 `src/` 引用。
- `io.py:13` 的 `LEGACY_BOOK_FILENAMES` 中 `"07_footnote_verified.json"`、`"06_proofread.json"` 对应的 refine-toc/proofread stage 早已删除。

**已确认事实（原 §7.1 待调研点）**：`_LINE_BREAK_RULES` / `_PARAGRAPH_BOUNDARY_RULES` / `_POETRY_RULES` / `_CROSS_PAGE_CONT_RULES` 在 `prompts.py:182-188`（CLEAN_SYSTEM）**和** `prompts.py:273-279`（VLM_SYSTEM）两处同时引用；还被 `_FOOTNOTE_CORE_RULES`/`_SPACING_RULES`/`_PENDING_*` 相邻片段共用。**必须保留这 4 个片段不动**。

**建议方案**：
1. 删除 `llm/prompts.py:160-223`（仅 `CLEAN_SYSTEM` 三引号字符串本身）。**保留** `prompts.py:9-90` 的 7 个 `_*_RULES` 片段（`_LINE_BREAK_RULES`/`_PARAGRAPH_BOUNDARY_RULES`/`_POETRY_RULES`/`_CROSS_PAGE_CONT_RULES`/`_SPACING_RULES`/`_FOOTNOTE_CORE_RULES`/`_PENDING_*`）—— `VLM_SYSTEM` 仍需要它们。
2. 删除 `ir/semantic.py:184-209` 的 4 个类及其上方 `# --- stage 5.5 / stage 3` 注释区。
3. 清理 `io.py:11-16` 的 `LEGACY_BOOK_FILENAMES`：保留 `05_semantic_raw.json`/`05_semantic.json`（仍为 assemble 输出 → build 输入），删除 `06_proofread.json` 和 `07_footnote_verified.json`。

**影响面**：`llm/prompts.py`（~64 行删除，远小于 v1 的"~90 行"估计——因保留片段）、`ir/semantic.py`（~25 行删除）、`io.py`（2 行删除）。无测试覆盖这些符号。

**人决策点**：
- **D2**: 是否保留 `CLEAN_SYSTEM` 作为未来 stage 重开的参考？（详见 §6——推荐彻底删除）

**风险**：低。

**依赖**：无。

---

#### [R4] `_apply_op` 改 dispatch 表

**问题描述**：`editor/apply.py:478-688`，`_apply_op` 是 16 个独立 `if isinstance(op, X): ... return book` 分支。每新增一种 op 需在 `_apply_op`、`_check_new_uid_collisions`、`_resolve_intra_chapter_uid`、`_target_effect_preconditions` 四处同时加分支。

**建议方案**：
将逐 op 的 apply 逻辑提为小函数（同文件内），**handler 签名统一为宽类型 `(book: Book, op: EditOp, op_id: str) -> Book`**，函数体内用 `assert isinstance(op, SetRole)` 做 narrow。dispatch 字典 value 就是 `Callable[[Book, EditOp, str], Book]`，pyrefly/mypy 不会报函数参数逆变问题：
```python
def _apply_set_role(book: Book, op: EditOp, op_id: str) -> Book:
    assert isinstance(op, SetRole)
    ...  # narrow 之后 pyrefly 认 op 为 SetRole
    return book

_APPLY_DISPATCH: dict[type[EditOp], Callable[[Book, EditOp, str], Book]] = {
    SetRole: _apply_set_role,
    SetStyleClass: _apply_set_style_class,
    # ... 共 13 个 text-mutating ops
}

def _apply_op(book: Book, op: EditOp, *, op_id: str) -> Book:
    if isinstance(op, NoopOp | CompactMarker | RevertOp):
        return book
    handler = _APPLY_DISPATCH.get(type(op))
    if handler is None:
        raise AssertionError(f"unsupported op type {type(op)!r}")
    return handler(book, op, op_id)
```

**影响面**：`editor/apply.py`（重构 ~210 行为若干短函数 + 一个 dispatch dict）。纯代码重排，不改语义。

**人决策点**：
- **D3**: dispatch 是否也覆盖 `_check_new_uid_collisions`、`_resolve_intra_chapter_uid`、`_target_effect_preconditions`？（详见 §6）

**风险**：中。重构面较大，需完整跑 `tests/test_editor_apply.py` 覆盖所有 op 类型。

**依赖**：无，但建议在 R1/R2 后进行。

---

#### [R5] 修正 `_join_text("cjk")` 隐式 catch-all → concat + 共享 CJK-join

**问题描述**：
- `editor/apply.py:196-201`：`_join_text` 的 `"cjk"` 分支**走的是默认 fallthrough `return "".join(parts)`**——既不是 CJK 逻辑（与 `assembler._cjk_join` 不等价），也不是显式的 concat 分支：
  ```python
  if join == "newline": return "\n".join(parts)
  if join == "concat": return "".join(parts)
  return "".join(parts)       # "cjk" 静默落到这里
  ```
  `MergeBlocks` 用 `join="cjk"` 时行为与 `"concat"` 相同，CJK 语义丢失。
- `assembler.py:601` 有完整 `_cjk_join(prev: str, cont: str) -> str`，处理 CJK/kana/hangul/hyphen 软连接；但它签名是二元（prev, cont），与 `_join_text` 的 n-ary 需求不匹配。

**建议方案**：
1. 新建 `src/epubforge/text_utils.py`，迁入：
   - `is_no_space_char(c: str) -> bool`（来自 `assembler._is_no_space_char`）
   - `cjk_join_pair(prev: str, cont: str) -> str`（来自 `assembler._cjk_join`，改公开）
   - `cjk_join(parts: list[str]) -> str`——新增 n-ary 版：`reduce(cjk_join_pair, parts, "")`
   - **Note**: `cjk_join_pair` 保留原 `_cjk_join` 的 `prev.rstrip()/cont.lstrip()` 语义（`assembler.py:610-611`）——即 `["hello ", " world"]` 得 `"hello world"`。此行为由 n-ary 版自然继承（reduce 每对调用时 strip）。不增加 `strip=True/False` 参数，保持单一语义。
2. `assembler.py` 保留 `_cjk_join` 作为 thin alias 或直接改 import。
3. `apply._join_text` 改写为 exhaustive `match`（或显式分支 + 兜底 `assert_never`）：
   ```python
   def _join_text(parts: list[str], join: Literal["concat", "cjk", "newline"]) -> str:
       match join:
           case "newline": return "\n".join(parts)
           case "cjk":     return cjk_join(parts)
           case "concat":  return "".join(parts)
           case _:         raise AssertionError(f"unreachable join kind {join!r}")
   ```
   消除 v1 代码里的隐式 catch-all。

**影响面**：新增 `text_utils.py`（~30 行）；`assembler.py:577-640` 局部改 import；`apply.py:196-201` 4-7 行改。

**人决策点**：
- **D4**: 放 `text_utils.py` 还是复用 `fields.py`？（详见 §6——推荐独立文件）

**风险**：低-中。`MergeBlocks` 行为变化意味着**zxgb fixture 若有 merge_blocks op 之前以 "cjk" 模式 apply 过且生成了错误 concat 结果**，将与新结果不一致。用户已授权 break，但需注意 fixture 重建。详见 §7.1。

**依赖**：无。

---

#### [R6] Config 统一：删除死 section + 消除 CLI 硬编码默认

**问题描述**：
- `config.local.toml:32-44` 有 `[proofread]` 和 `[footnote_verify]` 节，`config.py:load_config` **完全不读取**（grep 确认）。是 zxgb 测试运行遗留的死配置。
- `editor/tool_surface.py:337` 的 `--ttl default=1800`、`tool_surface.py:378` 的 `--ttl default=300` 与 `config.py:37` 的 `editor_lease_ttl_seconds: int = 1800` 是独立硬编码。CLI 子进程从不读 `Config`，用户改 TOML 无效。`leases.py:106` 和 `:146` 也有 `ttl: int = 1800`/`300`——这属于**内部方法的最终兜底**，保留。
- `llm/client.py:122-123`：`if use_vlm and self.max_tokens is None: self.max_tokens = 16384`——该兜底对 Config 不可见。
- `cli.py:43`：`_log_level = log_level or os.environ.get("EPUBFORGE_LOG_LEVEL", "INFO")`——绕过 config.py，无 TOML 支持。

**建议方案**：
1. **删除死 section**：从 `config.local.toml` 和 `config.example.toml` 删除 `[proofread]` 和 `[footnote_verify]` 两个节（除非 D2 决定保留 stub）。
2. **Config 默认注入 CLI**：`config.py` 新增 `book_exclusive_ttl_seconds: int = 300` 字段；在 `editor/tool_surface.py` 中 `acquire-lease`/`acquire-book-lock` 的参数解析改为运行时读取 `load_config()`：
   ```python
   cfg = load_config()
   parser.add_argument("--ttl", type=int, default=cfg.editor_lease_ttl_seconds)
   ```
   **不改** `leases.py:106`/`:146` 的方法签名默认值——保留 `ttl: int = 1800`/`300` 作为非 CLI 调用路径的最终兜底（双保险无回归风险）。注：当前 `state.acquire_chapter`/`state.acquire_book_exclusive` 虽只被 `tool_surface.py` 直接调用，保留方法级默认值仍避免未来新增内部调用遗漏 ttl 参数。
3. **`vlm_max_tokens` 默认值移出 `LLMClient`**：在 `Config` dataclass 中将 `vlm_max_tokens: int | None = None` 改为 `vlm_max_tokens: int = 16384`，删除 `client.py:122-123` 的条件赋值。
4. **`EPUBFORGE_LOG_LEVEL` 进入 Config**：新增 `log_level: str = "INFO"` 字段；在 `config.py:load_config` 中加一行 env 覆盖 `if v := os.environ.get("EPUBFORGE_LOG_LEVEL"): cfg.log_level = v`；`cli.py:43` 改为 `_log_level = log_level or cfg.log_level`。

**影响面**：
- `config.py`（~5 行增）
- `config.local.toml`/`config.example.toml`（~20 行删）
- `editor/tool_surface.py`（~10 行改，2 个 `default=` 改为动态 Config 读取）
- `editor/leases.py`（**不改**）
- `llm/client.py`(2 行删)
- `cli.py`（5 行改）

**人决策点**：
- **D5**: 是否迁到 pydantic-settings？（详见 §6——推荐**不迁**，维持 dataclass 但消除 ad-hoc）

**风险**：低-中。CLI 子进程加载 `Config` 意味着每次 `python -m epubforge.editor.acquire_lease` 要读 config.toml，有 1 次文件 I/O——可接受。详见 §7.2（启动开销抽查）。

**依赖**：无。

---

#### [R7] AGENTS.md 重写

**问题描述**：
- `AGENTS.md:6` 声称"Seven-stage pipeline: parse → classify → extract → assemble → refine-toc → proofread → build"，但 `refine-toc` / `proofread` **命令不存在**（`cli.py:80-155` 只注册 6 个子命令：run/parse/classify/extract/assemble/build）。`cli.py:84` 的 `--from max=4`。
- `AGENTS.md:13-22` 的 Pipeline 表格第 5/6/7 行完全虚构。
- `AGENTS.md` 对 `editor/` 子系统（`OpEnvelope`/`apply_envelope`/`memory_patches`/`split_merged_table`）、`audit/` 子系统、`BookMemory` 机制零描述。
- `AGENTS.md:50` 未提 `Table.multi_page`、`Table.merge_record`、`TableMergeRecord`。
- `AGENTS.md:51` 未提 `VLMPageOutput.updated_book_memory`。
- `AGENTS.md:55-64` Config env vars 清单漏掉 `EPUBFORGE_LLM_TIMEOUT`、`EPUBFORGE_VLM_TIMEOUT`、`EPUBFORGE_LLM_MAX_TOKENS`、`EPUBFORGE_VLM_MAX_TOKENS`、`EPUBFORGE_ENABLE_BOOK_MEMORY`、`EPUBFORGE_EDITOR_*` 全套，也漏掉 `EPUBFORGE_EDITOR_NOW`、`EPUBFORGE_PROJECT_ROOT`、`EPUBFORGE_WORK_DIR`、`EPUBFORGE_EDIT_STATE_DIR`（test-only / scratch subprocess injection）。
- `AGENTS.md:41` 提"Gemini 2.5/3"——版本号含糊；默认 VLM 是 `google/gemini-flash-3`（`config.py:24`）。

**建议方案**：
重写 `AGENTS.md`，结构：
1. **Project Overview**：改为"5-stage ingestion pipeline + editor subsystem"。
2. **Pipeline Stages**：表格只列 parse/classify/extract/assemble/build；stage 编号按 D6 决议（推荐 1-5）。
3. **Editor Subsystem**（全新节）：简述 `edit_state/` 目录结构（`book.json`、`edit_log.jsonl`、`memory.json`、`leases.json`、`staging.jsonl`、`scratch/`、`snapshots/`）；`OpEnvelope`/`apply_envelope`/`memory_patches` 语义；所有 `python -m epubforge.editor.<cmd>` 命令清单及输入/输出 JSON 契约要点。**补写一条 invariant**: "`apply_envelope` 的事务性依赖 `working = book.model_copy(deep=True)`（`apply.py:1065` 附近）——任何 op/memory_patches 失败都回滚到原 `book`，无需显式 transaction 栈"（响应新事实 2）。
4. **Audit Subsystem**（全新节）：列举 `detect_structure_issues`/`detect_table_merge_issues`/`detect_footnote_issues`/`detect_dash_inventory`/`detect_table_issues`/`detect_invariant_issues`。
5. **Semantic IR**：补 `TableMergeRecord`、`Table.multi_page`、`Table.merge_record`；`VLMPageOutput.updated_book_memory`；`BookMemory` 用途。**补写一句**: "注：`Book.version: int` 是 op 日志版本号（每次 `apply_envelope` +1），不是 IR schema 版本"（响应新事实 3）。
6. **Config**：全量 env vars 清单（从 `config.py:135-176` 生成）+ `EPUBFORGE_EDITOR_NOW`/`EPUBFORGE_PROJECT_ROOT`/`EPUBFORGE_WORK_DIR`/`EPUBFORGE_EDIT_STATE_DIR`（标 "test-only / scratch subprocess injection"，响应 m3）。
7. **保留** Beads/shell commands 段落不动。

**影响面**：仅 `AGENTS.md`（~160 行改动）。不影响代码。

**人决策点**：
- **D6**: `build` 命令该标 "Stage 5" 还是保留 "Stage 8"？（详见 §6——推荐 Stage 5）

**风险**：低。纯文档。

**依赖**：建议放在所有代码重构之后，以反映最终状态。

---

#### [R8] 测试 `_prov` fixture 统一

**问题描述**：`_prov` helper 在 **10 个**测试文件各自独立定义：`tests/test_audit_table_merge.py:9`、`test_editor_log.py:22`、`test_ir_semantic.py:33`、`test_editor_apply.py:35`、`test_editor_ops.py:11`、`test_architecture_migration.py:18`、`test_epub_builder.py:14`、`test_audit_detectors.py:15`、`test_editor_tool_surface.py:28`、`test_foundations_helpers.py:22`。无 `conftest.py`。

**建议方案（按 D7 新推荐）**：
新建 `tests/conftest.py`，提供 pytest fixture：
```python
@pytest.fixture
def prov():
    def _make(page: int = 1, source: Literal["llm", "vlm", "passthrough"] = "passthrough") -> Provenance:
        return Provenance(page=page, bbox=None, source=source)
    return _make
```
各测试文件的测试函数签名加入 `prov` 参数，调用点从 `_prov(...)` 改为 `prov(...)`。删除 10 处本地定义。

**影响面**：新建 `tests/conftest.py`（~15 行）；10 个测试文件各删 3-5 行、改所有 `_prov()` 调用点并在相关测试函数签名加入 `prov` 参数。

**人决策点**：
- **D7**: 用 pytest fixture（函数参数注入）还是 module-level helper（import 使用）？（详见 §6——**推荐从 v1 的"B. module-level helper"改为"A. pytest fixture"**，详情 D7 小节）

**风险**：低。

**依赖**：无。

---

#### [R9] 统一 editor CLI 脚本命名：kebab-case → snake_case

**问题描述**：`editor/` 下有 9 个 kebab-case 文件名：`acquire-book-lock.py`、`acquire-lease.py`、`apply-queue.py`、`import-legacy.py`、`propose-op.py`、`release-book-lock.py`、`release-lease.py`、`render-prompt.py`、`run-script.py`。同目录其他文件是 snake_case：`cli_support.py`、`tool_surface.py` 等。

**动机修正（响应 B2）**：v1 错误地声称 "`python -m epubforge.editor.apply-queue` 无法执行、必须用 `python src/.../apply-queue.py`"。经 reviewer 实测 + grep 确认：`tests/test_editor_tool_surface.py` 多处使用 `_run_module("epubforge.editor.propose-op", ...)` 形式 subprocess 调用并在 CI 中通过——Python `-m` 对**脚本式末端模块**不做 identifier 校验，kebab-case 文件名确实能执行。

真正的动机有三点：
1. **风格一致性**：同目录已有 `cli_support.py`/`tool_surface.py` 等 snake_case 文件，kebab-case 混搭是瑕疵。
2. **跨脚本 import 需要**：未来若 `apply-queue.py` 的某函数要被其他模块 import，kebab-case 会挡住（`from epubforge.editor.apply-queue import ...` 是语法错误）。当前这些文件都是 argparse + thin wrapper 无可 import 内容，但未来演进需 snake_case。
3. **Python 生态约定**：PEP 8 明确模块名应 snake_case。

**建议方案**：
重命名 9 个文件：`acquire_book_lock.py`、`acquire_lease.py`、`apply_queue.py`、`import_legacy.py`、`propose_op.py`、`release_book_lock.py`、`release_lease.py`、`render_prompt.py`、`run_script.py`。

**同步更新的调用站点清单**（grep 已验证全部命中）：
- `src/epubforge/editor/__main__.py:13-25`：列 13 个 commands，其中 9 个 kebab-case 条目需改为 snake-case（`init`/`doctor`/`compact`/`snapshot` 本就是 snake_case，不动）。
- `src/epubforge/editor/prompts.py:23-24`：prompt 文本中 `python -m epubforge.editor.run-script` 两处。
- `tests/test_editor_tool_surface.py`：`_run_module("epubforge.editor.<name>", ...)` 共 19 处。
- `docs/usage.md:38,88-99`：命令示例共 10 行。
- `docs/agentic-editing-howto.md`：全文约 29 处 kebab-case 命令引用（不仅命令列表行，还有正文散布的反引号引述）。
- `docs/footnote-audit-process.md` / `docs/finer-proofread.md` / `docs/fix-plan-v3.md`：若含引用，一并更新（grep 结果显示确有引用）。
- **执行时以 `grep -rn 'epubforge\.editor\.[a-z]*-[a-z]*' .` 扫尾验证**（详见 §5 检查清单），清单数字仅作参考。
- 9 个文件各自的 docstring 首行（`"""CLI entrypoint for \`python -m epubforge.editor.<name>\`."""`）。

**影响面**：`src/epubforge/editor/` 9 个文件重命名 + docstring；`editor/__main__.py`、`editor/prompts.py`；`tests/test_editor_tool_surface.py` ~19 处；`docs/` 3-4 文件。AGENTS.md 文档同步（在 R7 中一起）。

**人决策点**：
- **D8**: 是否同步调整 `epubforge editor` 顶层子命令？（详见 §6——推荐另开议题，本轮只改文件名）

**风险**：中。非代码 break 风险（用户若已用 shell alias 指向旧文件名）——本项目无外部用户，仅影响 `zxgb` 工具链，build 前需完整执行 `grep -rn 'epubforge\.editor\.[a-z-]*-[a-z-]*' .` 再扫一遍。

**依赖**：R7（文档同步）。

---

#### [R10] 补漏 `SplitMergedTable` 到 `editor/__init__.py` 的 `__all__`

**问题描述**：`editor/ops.py:869` 的 `__all__` 已含 `SplitMergedTable`（line 846-870），但 `editor/__init__.py:28-51` 的 re-export 和 `__all__` **遗漏**该名称。一致性瑕疵。

**建议方案**：
在 `editor/__init__.py:28-51` 的 import 语句加 `SplitMergedTable,`，`__all__` 列表加 `"SplitMergedTable",`。

**影响面**：`editor/__init__.py` 2 行。

**人决策点**：无。

**风险**：极低。

**依赖**：无。

---

#### [R11] `console.print` → `logging` 在 pipeline.py 中统一

**问题描述**：`pipeline.py` 对阶段边界使用 `console.print`（rich），其他所有模块用 `logging`（11 处，位于 line 23/57/60/71/74/91/94/104/107/119/127）。两套输出路径，stage banner 不会进 `work/<book>/logs/run-*.log`（因该 log 是 logging handler）。

**建议方案**：
删掉 `pipeline.py:8,13` 的 `Console` import 和实例；11 处 `console.print(...)` 改为 `log.info(...)`。去掉 rich markup（`[bold]`/`[dim]`），或改用 logging extra 字段。stderr 侧的 RichHandler（在 `observability.setup_logging`）会自动上色，不损失视觉。

**影响面**：`pipeline.py` ~15 行改动。

**人决策点**：无。

**风险**：低。用户看到的 CLI 输出会轻微变化（格式/颜色），但信息量一致，且进入 log 文件。

**依赖**：无。

---

#### [R12] 清理 `pyproject.toml` 的 `pillow` 未使用依赖

**问题描述**：`pyproject.toml:19` 声明 `pillow>=10`，但 `src/` grep 无 `PIL`/`pillow` import。Docling/PyMuPDF 可能间接依赖，但项目自身不直接用，声明为直接依赖是噪声。

**建议方案**：
删除 `pyproject.toml:19`。如果 `uv sync` 后 Docling 仍能正常工作（它的 transitive deps 会拉 pillow），则 OK。

**影响面**：`pyproject.toml` 1 行；`uv.lock` 由工具重算。

**人决策点**：无。

**风险**：低。若本地图片处理（`extract.py` 用 fitz/PyMuPDF）隐式依赖 PIL，会在 runtime 报 ImportError——但 fitz 不用 PIL，仅用自己的 pixmap。

**依赖**：无。

---

#### [R13] 统一 editor state 写入路径：`log.py` 裸 `write_text` → `atomic_write_text`

**问题描述（措辞修正，响应 m2）**：一致性瑕疵——同包 `editor/state.py:230-234` 已提供 `atomic_write_text/json/model`（临时文件 + `os.replace`）且同包大量使用，唯独 `editor/log.py:190-191,217` 3 处对 `archive_path / CURRENT_LOG`、`archive_path / BOOK_FILE`、`paths.current` 使用裸 `write_text`。不存在"崩溃半写文件"风险（Linux ext4 journal 下 `pathlib.write_text` 单次小写入原子性由 FS 保证），但代码一致性应统一。

**建议方案**：
`log.py` import `from epubforge.editor.state import atomic_write_text`；3 处 `.write_text(...)` 改为 `atomic_write_text(path, content)`。

**影响面**：`editor/log.py` 3 行改。

**人决策点**：无。

**风险**：低。

**依赖**：无。

---

#### [R14] 抽取 audit HTML regex 到 `audit/_html.py`（新增，响应 M1）

**问题描述**：`audit/tables.py:13-17` 与 `audit/table_merge.py:20-23` 存在 HTML 解析 regex 重复：
- **完全重复**：`TBODY_RE`、`COLSPAN_RE` 两个完全一致（含 `re.IGNORECASE | re.DOTALL` flags）。
- **等价重复**：`ROW_RE` 两处 pattern 稍有差异（tables.py 用 `(.*?)` 捕获 inner；table_merge.py 用非捕获 `.*?`），但语义等价——可统一为捕获式（table_merge.py 改用 `.group(0)` 或直接用第一组）。
- **语义不同保留差异**：`CELL_RE` 两处有实质差异——tables.py `<t[dh]\b([^>]*)>(.*?)</t[dh]>`（捕获 attrs+inner），table_merge.py `<t[dh]\b([^>]*)>`（仅 attrs，match 位置）。**不合并**。
- `ROWSPAN_RE` 只在 tables.py 有。保留。

**建议方案**：
新建 `src/epubforge/audit/_html.py`，导出：
- `TBODY_RE`
- `ROW_RE`（使用 tables.py 的捕获式版本，table_merge.py 改用 `.group(1)` 或 `.group(0)`）
- `COLSPAN_RE`

`audit/tables.py` 和 `audit/table_merge.py` 改为 `from epubforge.audit._html import TBODY_RE, ROW_RE, COLSPAN_RE`，删除本地定义。`CELL_RE` / `ROWSPAN_RE` 留在各自模块。

**影响面**：新增 `audit/_html.py`（~8 行）；`audit/tables.py` 删 3 行 + 改 import；`audit/table_merge.py` 删 3 行 + 改 import + 潜在 1 处 `.group(0)`/`.group(1)` 调整（若 ROW_RE 切回捕获式）。

**人决策点**：无（reviewer 已明确范围，不处理有差异的 `CELL_RE`）。

**风险**：低。需跑 `tests/test_audit_detectors.py` + `tests/test_audit_table_merge.py` 确认 regex match 行为不变。

**依赖**：无，适合与 R3/R12 一同作为 Commit 2 的死代码/一致性修整。

---

## 3. 计划外明确放弃的事项

以下问题**不在**本轮重构范围：

1. **Config 迁移到 pydantic-settings BaseSettings**：Report 1 的 Gap 建议。单人项目，当前 dataclass 虽手写但工作良好，迁移成本高于收益。保留现有 dataclass。
2. **prompt caching 多轮 `cache_control` 扩展**（Report 2 P6）：目前 VLM 调用只有一条 system message，`_apply_cache_control` 只处理它——功能性足够。未来若引入 few-shot 再处理。
3. **`_call_parsed` / `_call_json_object_fallback` usage 解包/budget 翻倍共享**（Report 2 P2）：虽是重复，但降级路径偶发，抽共享辅助可能反而增加间接层。判断收益不足，暂缓。
4. **`use_vlm: bool` → `ClientProfile` dataclass**（Report 2 P4）：当前项目实际只有一个 client 调用点（`extract.py:79`），profile 抽象收益极低。`use_vlm` 虽丑但非紧迫。
5. **`staging.jsonl` 的 apply-queue batch atomicity**（Report 3 P5）：这是设计选择（"逐条独立 apply"），非 bug。在 AGENTS.md 新节中文档化即可，不改行为。
6. **`TableMergeRecord.constituent_block_uids` 增补**（Report 3 P7）：是设计选择——uid 在 assembler 阶段不稳定。不改。
7. **`Book.version` 重命名为 `op_log_version`**（Report 3 P8）：牵扯序列化格式 break，且 `version` 名称虽宽泛但约定俗成。不改——但 R7 AGENTS.md 会文档化其确切语义（响应新事实 3）。
8. **`editor/prompts.py` 的 f-string 静态检查**（Report 2 P7）：prompt 模板变更不频繁，运行时 KeyError 容易排查。暂不引入静态检查框架。
9. **pipeline/extract 绕过 io.py**（Report 4 #3）：`extract.py` 处理多种中间 artifact（unit_*.json、book_memory.json、audit_notes.json），它们并非"Book"，`io.py` 目前专注于 Book。抽象共同的 `atomic_write_json` 到项目级 utils 是增值但非紧迫，暂缓。**为何 R13 修 log.py 而不修 extract.py**：log.py 写的是 `book.json` + `edit_log.jsonl` 核心 state（与同文件 `atomic_write_model` 混用，一致性瑕疵明显）；extract.py 的中间 artifact 是 pipeline 内部 cache（断电重跑即可），不在同一致性对比域。
10. **`audit` 的 `CELL_RE` 合并**（Report 4 #2 的子项）：两处 CELL_RE 语义不同，不合并（已在 R14 说明）。

---

## 4. 整体顺序与分组（commit 粒度）

建议 5 个 commit（按执行顺序）：

**Commit 1 — editor 内部去重**（~1 天）
- R1（validators 抽取）
- R2（StrictModel 统一）
- R10（`SplitMergedTable` 导出补漏）

**Commit 2 — 死代码清理 + audit 整合**（~半天）
- R3（CLEAN_SYSTEM 字符串删除，保留 4 个 `_*_RULES` 片段 + `Toc*`/`Clean*` model 删除）
- R12（pillow 依赖删除）
- R14（audit HTML regex 抽取）

**Commit 3 — apply 重构**（~1 天，单独 commit 便于 review）
- R4（`_apply_op` dispatch 表，handler 宽签名 + isinstance narrow）
- R5（`_join_text("cjk")` 修正 + text_utils 抽取）

**Commit 4 — Config/CLI 统一**（~1 天）
- R6（所有 Config 相关：死 section、CLI 硬编码默认、vlm_max_tokens、log_level。不改 leases.py 方法级默认值）
- R9（editor CLI 文件重命名 + 测试/文档 subprocess 字符串同步）
- R11（console.print → logging）
- R13（log.py atomic_write）

**Commit 5 — 文档 + 测试重整**（~1 天）
- R7（AGENTS.md 重写，含 invariant、Book.version 语义说明）
- R8（conftest.py，pytest fixture 风格）

**为什么分 5 个**：
- Commit 1 是纯机械重构，风险最低，先做让后续工作项基于新基类/共享 validator。
- Commit 2 是删除 + 新抽取 util，走得越早暴露问题越快。R14 与 R3 风格一致（死/重复清理），合并一 commit。
- Commit 3 的 dispatch 改动较大，单独 commit 便于 review 和回滚。
- Commit 4 混合 config/CLI/logging，但它们改同一批入口文件，合并可避免来回 merge。R9 重命名必须与 R6 一起以防中间状态不可用。
- Commit 5 最后做，以反映代码终态。

---

## 5. 验证策略

**每 commit 必跑**：
```bash
uv run pytest -x                       # 全量测试（快）
uv run pyrefly                         # 类型检查（pyproject.toml 已配）
uv run pre-commit run --all-files      # 若已配置
```

**端到端冒烟**（在 Commit 3 和 Commit 4 之后重点跑）：
```bash
uv run epubforge run fixtures/zxgb.pdf --force-rerun --from 1
```
确认 pipeline 正常走完、`work/zxgb/out/zxgb.epub` 生成。由于用户授权 break 历史文件，若 zxgb 的 `edit_state/` 不可复用（例如 `MergeBlocks(join="cjk")` 历史 apply 结果），允许整个目录重建。

**editor CLI 冒烟**（Commit 4 之后）：
```bash
python -m epubforge.editor.init work/zxgb
python -m epubforge.editor.doctor work/zxgb
python -m epubforge.editor.propose_op work/zxgb < sample_ops.json
python -m epubforge.editor.apply_queue work/zxgb
```

**关键检查清单**：
- [ ] `grep -r _require_non_empty src/` 只剩 `_validators.py` 一个定义。
- [ ] `grep -rn -E 'EditorModel|MemoryModel|DoctorModel|LeaseModel' src/ tests/` 只剩 R2 过程中的 import 行。
- [ ] `grep -r CLEAN_SYSTEM src/` 返回空。
- [ ] `grep -r _LINE_BREAK_RULES src/epubforge/llm/prompts.py` 返回定义 + `VLM_SYSTEM` 引用共 2 类（证明片段保留）。
- [ ] `grep -r 16384 src/` 返回空（或只剩 `config.py` 一处）。
- [ ] `grep -rn 'epubforge\.editor\.[a-z]*-[a-z]*' .` 返回空（所有 kebab-case subprocess 字符串都已改）。
- [ ] `uv run epubforge run fixtures/zxgb.pdf` 不报错。
- [ ] AGENTS.md 的 Pipeline 表格与 `cli.py:84` 的 `--from max=4` 一致（注：若采纳 D6 Stage 5 还需同步 `--from` 的 help 文案）。
- [ ] `grep -rn 'Stage 8' src/` 返回空（若 D6 采纳 Stage 5）。

---

## 6. 待用户决策的问题汇总

### D1: editor 基类命名

- **背景**：R2 合并 4 个基类。
- **选项**：
  - A. `StrictModel`（简短、描述配置）
  - B. `BaseStrictModel`（PEP 8 的 Base 前缀约定）
  - C. `EditorBaseModel`（点出 editor 包归属，但它其实是泛用基类）
- **推荐**：**A. `StrictModel`**。短、准确、不冗余。项目已经大量用 `StrictXxx` 风格（Pydantic `extra="forbid"` 即"strict"）。reviewer 同意。

### D2: `CLEAN_SYSTEM` 处理

- **背景**：R3 提议删除。
- **选项**：
  - A. 彻底删除（含 `CleanBlock`/`CleanOutput`，但保留共享 `_*_RULES` 片段）。
  - B. 保留 prompt 字符串 + 注释 "reserved for future clean-stage reintroduction"。
  - C. 迁移到 `docs/legacy/` 作参考资料。
- **推荐**：**A. 彻底删除**。死代码就是死代码，git history 就是参考。若未来重启 clean-stage，从头写 prompt 也只需半小时。reviewer 同意（明确 "彻底删除" 仅指 `CLEAN_SYSTEM` 字符串本身，4 个 `_*_RULES` 片段因 `VLM_SYSTEM` 仍引用必须保留——已在 R3 正文明示）。

### D3: R4 的 dispatch 表覆盖范围

- **背景**：`_apply_op` 有 16 个 `isinstance`，但 `_check_new_uid_collisions`（apply.py:~440）、`_resolve_intra_chapter_uid`（apply.py:695）、`_target_effect_preconditions`（apply.py:~780）各自也有类似大分支。
- **选项**：
  - A. 只改 `_apply_op`，另三处保留 isinstance（最小改动）。
  - B. 同时把另三个函数也改 dispatch——但每个 op 的这些 side-concerns 形式不一样（有的不返回 Book 而返回 set/str），设计上不是单分派能完美覆盖的。
  - C. 引入 `OpHandler` 协议类（一个类聚合某 op 的 apply/uid/preconditions），每个 op 一个 handler 类——重度抽象。
- **推荐**：**A**。仅改 `_apply_op`。其它三函数的分支各异，且未来新增 op 的频率未必高，C 方案过度设计。reviewer 同意。

### D4: text_utils 放哪

- **背景**：R5 需要一个放共享 CJK-join 的模块。
- **选项**：
  - A. 新建 `src/epubforge/text_utils.py`（独立、清晰）。
  - B. 放入现有 `src/epubforge/fields.py`（但 fields.py 职责是 IR 字段迭代，挂 text util 会模糊）。
  - C. 放入 `src/epubforge/markers.py`（当前 markers.py 处理脚注标记正则，与 CJK join 不同领域）。
- **推荐**：**A. 新建 `text_utils.py`**。reviewer 同意。

### D5: Config 是否迁到 pydantic-settings

- **背景**：R6 整理 Config。
- **选项**：
  - A. 保留 dataclass（改 CLI 硬编码默认但不换框架）。
  - B. 迁到 `pydantic-settings.BaseSettings`，三层自动合并，schema 声明式。
- **推荐**：**A**。单人项目、当前代码工作、迁移成本实质性，优雅度收益有限。reviewer 同意。

### D6: `build` stage 编号

- **背景**：`cli.py:151` 注释标 "Stage 8"，`pipeline.py:119` console 字符串 "Stage 8"，`pipeline.py:120` `stage_timer("8 build")`，`AGENTS.md:21` 旧表标 stage 7。
- **选项**：
  - A. 保留 "Stage 8" 命名（历史遗留，反映 "过去存在 5/6/7 的 refine-toc/proofread/old-build"）。
  - B. 改为 "Stage 5"（当前实际是第 5 个命令，顺序直观）。
  - C. 去掉数字编号，只叫 "Build stage"。
- **推荐**：**B. Stage 5**。已 break 了旧流程，编号就要反映当前事实。同步点三处（reviewer 指出）：`cli.py:151` docstring、`pipeline.py:119` console 字符串、`pipeline.py:120` `stage_timer("8 build")`——三处一起改为 "Stage 5" / `"5 build"`。

### D7: R8 的 conftest 实现方式

- **背景**：统一测试 `_prov`。
- **选项**：
  - A. pytest fixture（需写 `def test_foo(prov):` 参数）。
  - B. module-level helper `make_provenance()`，各测试 `from tests.conftest import make_provenance`。
  - C. 两者都提供。
- **推荐（v2 修订）**：**A. pytest fixture**。v1 推荐 B，reviewer 弱反对，理由是 pytest fixture 更符合 pytest 惯例，测试函数签名 `def test_x(prov)` 对新读者更直观，切换成本与 B 方案等价。v2 采纳。

### D8: R9 文件重命名同步调整 `epubforge editor` 顶层子命令？

- **背景**：当前 Typer 应用 `epubforge` 只有 parse/classify/extract/assemble/build/run。editor 系列靠 `python -m epubforge.editor.<cmd>`。
- **选项**：
  - A. 仅改文件名（本轮范围）。
  - B. 顺便把 `python -m epubforge.editor.apply_queue` 迁成 `epubforge editor apply-queue`——Typer 子子命令。
- **推荐**：**A**。B 是新功能而非重构。单独 issue。reviewer 同意。

---

## 7. 不确定 / 需要进一步调研的点

1. **R5 的 CJK join 语义差异影响 fixture**：`MergeBlocks(join="cjk")` 若曾在 zxgb 上被调用过、当前 concat 结果被写入 `edit_log.jsonl` 并 apply 到 `book.json`，R5 修正后**replay 会给出不同结果**。需确认 zxgb 是否用了 `join="cjk"`——grep `edit_log.jsonl` 里 "cjk" 字样；若用过，需要接受 book.json 差异或重建 edit_state。

2. **R6 中 `editor_lease_ttl_seconds` 的读 Config 方式**：CLI 子进程 `python -m epubforge.editor.acquire_lease` 启动开销。`load_config()` 读两个 TOML 文件 + env 约 5ms，可接受；但要确认 CLI 不在 ttl 分支前预早 import 其它重依赖（例如 docling 会拖慢 import）。当前 `tool_surface.py:28` import `render_prompt` 未拉 docling，应该快。**需抽查 import 时间**。

（v1 §7.1 的 `_*_RULES` 共享关系 → 已确认在 VLM_SYSTEM 中引用，见 R3；v1 §7.2 的 `python -m` 能否跑 kebab-case → 已确认能跑，见 R9；v1 §7.5 的 R4 类型标注 → 已在 R4 方案里用"宽签名 + isinstance narrow"定下，见 R4。以上 3 条从待调研降级为已解决。）

---

## 附录 A. v2 vs v1 差异摘要

- 工作项数：13 → **14**（新增 R14 audit HTML regex 抽取）。
- R3：明确保留 4 个 `_*_RULES` 片段；v1 §7.1 废除。
- R4：handler 签名方案从"精确类型"改为"宽类型 `EditOp` + `assert isinstance` narrow"；v1 §7.5 废除。
- R5：问题叙述从 "等同于 concat" 改为 "走隐式 catch-all 的 concat 分支"；建议方案改为 `match` exhaustive + `AssertionError` 兜底；保留 `cjk_join_pair` 的 strip 语义（note 化）。
- R6：`leases.py:106/146` 的默认值**保留不动**，只改 `tool_surface.py` 两个 `argparse default=`。
- R7：新增 3 条——`apply_envelope` deep copy invariant、`Book.version` op-log 语义、scratch env vars（`EPUBFORGE_EDITOR_NOW` 等）。
- R8：fixture 数量 "9" → "10"；推荐从 module-level helper 改为 pytest fixture。
- R9：动机重写（删除"kebab-case 不可执行"错误叙述），工作清单显式枚举 19+ 处 subprocess 字符串同步点。
- R13：措辞去掉"半写文件"，改为"一致性"。
- D6：明确同步点是 3 处（`cli.py:151` + `pipeline.py:119,120`）。
- D7：推荐翻转（module-level helper → pytest fixture）。
- §7：从 5 条待调研降至 2 条。
