# epubforge Refactor Plan — Human Reviewed v1

**Status**: finalized — 所有决策点已由用户确认，可进入执行。
**Base**: `refactor-plan-v2.md`（已 APPROVED） + `human_input.md`（用户决策）。
**Scope**: 优雅重构——消除重复、统一命名、清理死代码、修正文档。无用户可见的功能性变更（除 config 加载行为变更）。
**Authorization**: 用户已授权 break 向后兼容（zxgb fixture 已完成，无历史包袱）。

---

## 0. Human Decisions 汇总（来自 `human_input.md`）

| 编号 | 决策 | 与 v2 推荐比较 |
|---|---|---|
| D1 | **A — `StrictModel`** | 与 v2 推荐一致 |
| D2 | **A — 彻底删除 `CLEAN_SYSTEM`** | 与 v2 推荐一致 |
| D3 | **A — dispatch table 模式（只改 `_apply_op`）** | 与 v2 推荐一致 |
| D4 | **A — 新建 `text_utils.py`** | 与 v2 推荐一致 |
| D5 | **B — 迁移到 `pydantic-settings`** | ⚠️ **翻转 v2 推荐**（v2 推荐 A 保留 dataclass） |
| D6 | **B — 改名 Stage 5** | 与 v2 推荐一致 |
| D7 | **A — pytest fixture** | 与 v2 推荐一致 |
| D8 | **B — 把 `python -m epubforge.editor.<cmd>` 迁成 `epubforge editor <cmd>` Typer 子命令**（⚠️ 用户追加指令后翻转） | ⚠️ **翻转 v2 推荐**（v2 推荐 A 保留 `python -m`） |
| R5 额外 | 用户不关心 break fixture（"i've finished all job on it"）→ §7.1 原"zxgb fixture 重建确认"不再阻塞 | — |
| R6 修正 | **只读 CLI `--config <path>` 指定的文件**；不再隐式扫描 `config.toml` / `config.local.toml` | ⚠️ **扩大 v2 R6 的范围**（v2 保留双文件 layered 读） |
| 计划外新增 | **Book.version → op_log_version** 重命名（原在 v2 §3 "放弃项 #7"） | ⚠️ **新增 R15**（v2 标记"不做"） |

### 对执行的直接影响

- **D5 翻转**改写 R6 实现路径：原本小改 dataclass + 消除 CLI 硬编码 → 现在要迁到 `pydantic-settings.BaseSettings`。
- **R6 修正**进一步改写加载语义：`load_config()` 参数从"可选"变为"唯一来源"，无 TOML 指定时只用 defaults + env。命令行必须显式 `--config <path>`（或干脆不用 TOML，只 env）。
- **D8 翻转** + R6 收益合流：9 个 editor 脚本迁成 `epubforge editor <cmd>` Typer 子命令后，`--config` 只在 Typer 根 callback 接收一次，自然通过 `ctx.obj` 下发到所有子命令——**§7.1（子进程 config 分发策略）自动消失**。
- **新增 R15**把原本放弃的 `Book.version` 重命名列为正式工作项——涉及 IR schema 字段改名 + `edit_log.jsonl`/`book.json` 序列化字段随之 break，用户已授权。

---

## 1. Executive Summary

本轮重构聚焦 5 条主线：
1. **editor 子包内部整洁**（R1/R2/R10）——消除 4 份复制的 validator 与 4 个等价 `extra="forbid"` 基类，补漏 `SplitMergedTable` 导出。
2. **apply 分派重构**（R4/R5）——`_apply_op` 16 路 `isinstance` → dispatch 表；`_join_text("cjk")` 修正 + CJK helper 抽取到 `text_utils.py`。
3. **Config 现代化**（R6）——迁到 `pydantic-settings`；删除死 section `[proofread]` / `[footnote_verify]`；消除 CLI 硬编码默认；`load_config()` 只认 CLI `--config <path>`，不隐式扫描。
4. **清除死代码 + 一致性**（R3/R11/R12/R13/R14）——`CLEAN_SYSTEM`、`TocRefineOutput`、`CleanOutput` 删除（保留共享 `_*_RULES` 片段）；pipeline console.print → logging；pillow 未使用依赖删；log.py 裸 `write_text` → `atomic_write_text`；audit HTML regex 去重。
5. **命名/文档/测试**（R7/R8/R9/R15）——editor CLI 文件 kebab → snake_case；AGENTS.md 重写；测试 `_prov` → pytest fixture；`Book.version` → `op_log_version`。

预计 **15** 个工作项、~1000 行代码改动（多为删除 + 改名），覆盖约 12 个源文件 + 9 个 editor CLI 文件重命名。**新增 1 项生产依赖：`pydantic-settings`**。

---

## 2. 重构工作项

按"高价值低风险优先"排序。仅对比 v2 的改动在标题后标 **[MOD]** / **[NEW]**。

---

### [R1] 抽取 editor 共享 validator 到 `editor/_validators.py`

**问题描述**：`_require_non_empty` 在 4 个文件各有一份独立实现（行为一致）：`editor/ops.py:35-38`、`editor/memory.py:36-39`、`editor/doctor.py:20-23`、`editor/leases.py:11-14`。`_validate_uuid4` 和 `_validate_utc_iso_timestamp` 在 `ops.py:41-63` 与 `memory.py:42-64` 双份。`leases.py:17-27` 另有 `_parse_utc_iso`——返回 `datetime` 而非 `str`，共享同样的正则与错误路径。

**建议方案**：新建 `src/epubforge/editor/_validators.py`，导出：
- `require_non_empty(value: str, *, field_name: str) -> str`
- `validate_uuid4(value: str, *, field_name: str) -> str`
- `validate_utc_iso_timestamp(value: str, *, field_name: str) -> str`
- `parse_utc_iso(value: str, *, field_name: str) -> datetime`（保留 leases 专用语义）

4 个现有模块改为 `from epubforge.editor._validators import ...`，删除各自本地定义（~80 行删）。命名去掉前导下划线——它们现在是包内公用，而非文件私有。

**影响面**：`ops.py`、`memory.py`、`doctor.py`、`leases.py`；新增 `_validators.py`（~60 行）。无外部接口变化。

**风险**：低。Pydantic `field_validator` 闭包内调用保持不变。

**依赖**：无。

---

### [R2] 统一 editor 基类为 `StrictModel`（D1=A 确认）

**问题描述**：四个等价基类：`EditorModel`（`ops.py:75`）、`MemoryModel`（`memory.py:32`）、`DoctorModel`（`doctor.py:16`）、`LeaseModel`（`leases.py:41`），实现全部都是 `class X(BaseModel): model_config = ConfigDict(extra="forbid")`。

**建议方案**：在 `editor/_validators.py`（或独立 `editor/_models.py`）中定义：
```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
```
4 个模块 import 并继承 `StrictModel`，删除本地 4 个基类定义。

**Commit 前强制检查**：`grep -rn -E 'EditorModel|MemoryModel|DoctorModel|LeaseModel' src/ tests/` 必须仅在被删行与本次新增 import 中出现。

**影响面**：4 个 editor 文件，每个 3 行增减。

**风险**：低。

**依赖**：可与 R1 合并。

---

### [R3] 删除死代码 `CLEAN_SYSTEM` / `TocRefineOutput` / `CleanOutput`（D2=A 确认）

**问题描述**：grep 验证结果——
- `llm/prompts.py:160-223` 的 `CLEAN_SYSTEM` 在 `src/` 中零 import。
- `ir/semantic.py:186-194`（`TocRefineItem`/`TocRefineOutput`）、`ir/semantic.py:199-209`（`CleanBlock`/`CleanOutput`），均无 `src/` 引用。
- `io.py:13` 的 `LEGACY_BOOK_FILENAMES` 含 `"07_footnote_verified.json"`、`"06_proofread.json"` 对应的 stage 早已删除。

**已确认事实**：`_LINE_BREAK_RULES` / `_PARAGRAPH_BOUNDARY_RULES` / `_POETRY_RULES` / `_CROSS_PAGE_CONT_RULES` 在 `prompts.py:182-188`（CLEAN_SYSTEM）**和** `prompts.py:273-279`（VLM_SYSTEM）两处同时引用；还被 `_FOOTNOTE_CORE_RULES`/`_SPACING_RULES`/`_PENDING_*` 相邻片段共用。**必须保留这些片段不动**。

**建议方案**：
1. 删除 `llm/prompts.py:160-223`（仅 `CLEAN_SYSTEM` 三引号字符串本身）。**保留** `prompts.py:9-90` 的 7 个 `_*_RULES` 片段。
2. 删除 `ir/semantic.py:184-209` 的 4 个类与上方 `# --- stage 5.5 / stage 3` 注释区。
3. 清理 `io.py:11-16` 的 `LEGACY_BOOK_FILENAMES`：保留 `05_semantic_raw.json`/`05_semantic.json`，删除 `06_proofread.json` 与 `07_footnote_verified.json`。

**影响面**：`llm/prompts.py`（~64 行删）、`ir/semantic.py`（~25 行删）、`io.py`（2 行删）。

**风险**：低。

**依赖**：无。

---

### [R4] `_apply_op` 改 dispatch 表（D3=A 确认）

**问题描述**：`editor/apply.py:478-688`，`_apply_op` 是 16 个独立 `if isinstance(op, X): ... return book` 分支。

**建议方案**：逐 op apply 逻辑提为小函数，handler 签名统一为宽类型 `(book: Book, op: EditOp, op_id: str) -> Book`，函数体内用 `assert isinstance(op, SetRole)` 做 narrow：
```python
def _apply_set_role(book: Book, op: EditOp, op_id: str) -> Book:
    assert isinstance(op, SetRole)
    ...
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

**D3 范围**：仅改 `_apply_op`。`_check_new_uid_collisions` / `_resolve_intra_chapter_uid` / `_target_effect_preconditions` 三处 isinstance 暂不改——每个函数的 side-concern 形式不同（返回 set/str 等），单分派无法干净覆盖。

**影响面**：`editor/apply.py`（重构 ~210 行为若干短函数 + 一个 dispatch dict）。纯代码重排，不改语义。

**风险**：中。重构面较大，需完整跑 `tests/test_editor_apply.py` 覆盖所有 op 类型。

**依赖**：无，但建议在 R1/R2 后进行。

---

### [R5] 修正 `_join_text("cjk")` 隐式 catch-all → concat + 共享 CJK-join（D4=A 确认）

**问题描述**：
- `editor/apply.py:196-201`：`_join_text` 的 `"cjk"` 分支走的是默认 fallthrough `return "".join(parts)`——既不是 CJK 逻辑，也不是显式 concat。`MergeBlocks` 用 `join="cjk"` 时行为与 `"concat"` 相同，CJK 语义丢失。
- `assembler.py:601` 有完整 `_cjk_join(prev: str, cont: str) -> str`，二元签名与 n-ary 需求不匹配。

**建议方案**：
1. 新建 `src/epubforge/text_utils.py`，迁入：
   - `is_no_space_char(c: str) -> bool`（来自 `assembler._is_no_space_char`）
   - `cjk_join_pair(prev: str, cont: str) -> str`（来自 `assembler._cjk_join`，改公开）
   - `cjk_join(parts: list[str]) -> str` —— n-ary 版：`reduce(cjk_join_pair, parts, "")`
   - **Note**: 保留原 `_cjk_join` 的 `prev.rstrip() / cont.lstrip()` 语义。不增加 `strip=True/False` 参数。
2. `assembler.py` 保留 `_cjk_join` 作为 thin alias 或直接改 import。
3. `apply._join_text` 改写为 exhaustive `match`：
```python
def _join_text(parts: list[str], join: Literal["concat", "cjk", "newline"]) -> str:
    match join:
        case "newline": return "\n".join(parts)
        case "cjk":     return cjk_join(parts)
        case "concat":  return "".join(parts)
        case _:         raise AssertionError(f"unreachable join kind {join!r}")
```

**影响面**：新增 `text_utils.py`（~30 行）；`assembler.py:577-640` 局部改 import；`apply.py:196-201` 4-7 行改。

**R5 用户补充**："i don't care breaking that file, i've finished all job on it." → 若 zxgb 的 `book.json` 因 `MergeBlocks(join="cjk")` replay 结果变化，**直接接受 book.json 差异或重建 edit_state**。v2 §7.1 待调研点移除。

**风险**：低。行为变更范围已确认可接受。

**依赖**：无。

---

### [R6] Config 迁移到 pydantic-settings + 消除 CLI 硬编码 + CLI 显式指定 `--config`（D5=B 确认 ⚠️）[MOD]

**问题描述（v2 基础）**：
- `config.local.toml:32-44` 有 `[proofread]` 和 `[footnote_verify]` 节，`config.py:load_config` 完全不读取——死配置。
- `editor/tool_surface.py:337` 的 `--ttl default=1800`、`:378` 的 `--ttl default=300` 与 `config.py:37` 的 `editor_lease_ttl_seconds` 是独立硬编码。CLI 子进程从不读 `Config`，用户改 TOML 无效。
- `leases.py:106/146` 的 `ttl: int = 1800/300` 是方法级兜底，**保留**。
- `llm/client.py:122-123`：`if use_vlm and self.max_tokens is None: self.max_tokens = 16384` 对 Config 不可见。
- `cli.py:43`：`_log_level = log_level or os.environ.get("EPUBFORGE_LOG_LEVEL", "INFO")` 绕过 config.py。

**用户新增约束（human_input.md R6）**：
> don't infer config file to read, only read from the file specified in cli arg, don't make config.toml / config.local.toml as default config, force user to specify.

即：`config.py:65` 的 `toml_paths = (config_path,) if config_path else (Path("config.toml"), Path("config.local.toml"))` **隐式 fallback 必须消除**。没有 `--config` 时 → 仅用 defaults + env vars。

**建议方案（修订版，整合 D5 + R6 用户约束）**：

**Step 1: 迁到 `pydantic-settings`**
1. 新增依赖 `pydantic-settings>=2.7`（`pyproject.toml`）。
2. 改写 `src/epubforge/config.py`：
   ```python
   from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource
   from pydantic import Field

   class Config(BaseSettings):
       model_config = SettingsConfigDict(
           env_prefix="EPUBFORGE_",
           extra="ignore",  # 忽略未知 env var（兼容 scratch 子进程注入）
       )

       llm_base_url: str = "https://openrouter.ai/api/v1"
       llm_api_key: str = Field(default="", alias="llm_api_key")
       # ... 扁平化所有字段（env var 天然命名为 EPUBFORGE_LLM_BASE_URL 等）
       editor_lease_ttl_seconds: int = 1800
       book_exclusive_ttl_seconds: int = 300  # 新增
       vlm_max_tokens: int = 16384             # 从 None 改为具体默认
       log_level: str = "INFO"                  # 从 cli.py:43 迁入
       # ...
   ```
3. TOML 嵌套结构（`[llm]` / `[vlm]` / `[runtime]` / `[editor]` / `[extract]`）通过 `TomlConfigSettingsSource` + 自定义 mapping 或改用扁平 TOML 表达。**推荐**：让 pydantic-settings 读顶层扁平 key，TOML schema 也扁平化（`config.example.toml` 同步改写）。*若用户强烈要求保留嵌套 TOML 结构，作为 §7 待确认——执行前需选择。*
4. 自定义 `settings_customise_sources` 关闭自动 `.env` 扫描；保留顺序：init kwargs → env → toml（如指定）→ defaults。

**Step 2: `load_config()` 只认 CLI `--config`**
```python
def load_config(config_path: Path | None = None) -> Config:
    # config_path=None: defaults + env only (no TOML)
    # config_path=Path(...): defaults + env + that single TOML file
    sources: list = []
    if config_path is not None:
        if not config_path.exists():
            raise SystemExit(f"config file not found: {config_path}")
        sources.append(TomlConfigSettingsSource(Config, toml_file=config_path))
    return Config(_toml_sources=sources)  # 实际通过 settings_customise_sources 注入
```
- **删除** v2 `config.py:65` 的 `(Path("config.toml"), Path("config.local.toml"))` 隐式扫描分支。
- 删除 `.gitignore:233-234` 的 `config.toml` / `config.local.toml` 条目（因默认不再读，用户若放文件也不会被误读；`config.example.toml` 保留作为模板）。
- `config.local.toml` 若当前存在——可删除或由用户自行重命名为显式 `my-config.toml` 并通过 `--config` 传入。

**Step 3: CLI 入口统一 `--config`（因 D8=B 变简单）**
- `cli.py` 的 Typer `app` 根 callback 增加 `--config` 选项：
  ```python
  @app.callback()
  def main(ctx: typer.Context, config: Path | None = typer.Option(None, "--config", help="Path to TOML config file")):
      ctx.obj = load_config(config_path=config)
  ```
- 9 个 editor 子命令也挂在同一 Typer `app` 下（R9 迁 Typer 之后），共用根 callback 的 `--config`——无需在每个子命令再定义一次。
- 子命令通过 `ctx.obj` 取 Config：`def propose_op_cmd(ctx: typer.Context, work: Path): cfg = ctx.obj; ...`。
- **§7.1 已废除**：因为没有"9 个独立子进程"，无需选择 α/β/γ 分发策略。

**Step 4: 消除 CLI 硬编码默认**
- `editor/tool_surface.py` 中 `acquire-lease`/`acquire-book-lock` 的 `--ttl`：
  ```python
  cfg = load_config(args.config)
  parser.add_argument("--ttl", type=int, default=cfg.editor_lease_ttl_seconds)
  ```
- **不改** `leases.py:106/146` 方法签名默认值（最终兜底）。

**Step 5: 删除死 section**
- `config.example.toml` 和（若保留）`config.local.toml` 删除 `[proofread]` / `[footnote_verify]` 节。

**Step 6: vlm_max_tokens / log_level 统一**
- `Config.vlm_max_tokens: int = 16384`（不再 None），删 `client.py:122-123` 条件赋值。
- `Config.log_level: str = "INFO"`，`cli.py:43` 改为 `_log_level = log_level or cfg.log_level`。

**影响面**：
- `pyproject.toml`（+1 依赖）
- `src/epubforge/config.py`（整体重写 ~180 → ~100 行）
- `src/epubforge/cli.py`（+ `--config` 根 callback，log_level 改读 cfg）
- `src/epubforge/editor/tool_surface.py`（`--ttl` 默认改动态，通过 `ctx.obj` 取 Config）
- 9 个 editor 子命令（D8=B，R9 迁 Typer 后**自动**共用根 callback 的 `--config`，无需单独加参数）
- `config.example.toml` / `config.local.toml`（死 section 删 + 可能扁平化）
- `llm/client.py`（2 行删）
- `.gitignore`（删 2 条）

**风险**：中-高。
1. pydantic-settings 的 TomlConfigSettingsSource 对嵌套表的映射需要微调，若扁平化失败需改 schema 提供 alias。
2. 迁 Typer 后每次 `epubforge editor <cmd>` 启动仍需实例化 BaseSettings + parse TOML，但只一次（不再是 9 个独立 `python -m` 子进程），开销降低。
3. `.gitignore` 改动可能让用户本地 `config.toml` 意外进入 git —— **Commit 5 执行前必须口头提醒用户 `git rm --cached config.toml config.local.toml` 或确认本地无敏感内容**。

**依赖**：必须与 R9 绑定（R9 把 9 个 editor 脚本迁 Typer 后，根 callback 才能把 `--config` 推到所有子命令）。

---

### [R7] AGENTS.md 重写

**问题描述**：
- `AGENTS.md:6` 声称"Seven-stage pipeline: parse → classify → extract → assemble → refine-toc → proofread → build"，但 `refine-toc` / `proofread` **命令不存在**（`cli.py:80-155` 只注册 6 个子命令）。
- `AGENTS.md:13-22` Pipeline 表格 5/6/7 行完全虚构。
- `AGENTS.md` 对 `editor/` 子系统、`audit/` 子系统、`BookMemory` 机制零描述。
- `AGENTS.md:50` 未提 `Table.multi_page`、`Table.merge_record`、`TableMergeRecord`。
- `AGENTS.md:51` 未提 `VLMPageOutput.updated_book_memory`。
- `AGENTS.md:55-64` Config env vars 清单漏掉 `EPUBFORGE_LLM_TIMEOUT`、`EPUBFORGE_VLM_TIMEOUT`、`EPUBFORGE_LLM_MAX_TOKENS`、`EPUBFORGE_VLM_MAX_TOKENS`、`EPUBFORGE_ENABLE_BOOK_MEMORY`、`EPUBFORGE_EDITOR_*` 全套；以及 `EPUBFORGE_EDITOR_NOW`、`EPUBFORGE_PROJECT_ROOT`、`EPUBFORGE_WORK_DIR`、`EPUBFORGE_EDIT_STATE_DIR`（test-only / scratch subprocess injection）。
- `AGENTS.md:41` 提"Gemini 2.5/3"模糊；默认 VLM 是 `google/gemini-flash-3`（`config.py:24`）。

**建议方案**：重写 `AGENTS.md`，结构：
1. **Project Overview**：改为"5-stage ingestion pipeline + editor subsystem"。
2. **Pipeline Stages**：表格只列 parse/classify/extract/assemble/build；stage 编号按 D6=B **改为 Stage 1-5**。
3. **Editor Subsystem**（全新节）：简述 `edit_state/` 目录结构、`OpEnvelope` / `apply_envelope` / `memory_patches` 语义、所有 `python -m epubforge.editor.<cmd>` 命令清单及 JSON 契约要点。**补写 invariant**: "`apply_envelope` 的事务性依赖 `working = book.model_copy(deep=True)`（`apply.py:1065` 附近）——任何 op/memory_patches 失败都回滚到原 `book`。"
4. **Audit Subsystem**（全新节）：列举 `detect_structure_issues` / `detect_table_merge_issues` / `detect_footnote_issues` / `detect_dash_inventory` / `detect_table_issues` / `detect_invariant_issues`。
5. **Semantic IR**：补 `TableMergeRecord`、`Table.multi_page`、`Table.merge_record`；`VLMPageOutput.updated_book_memory`；`BookMemory` 用途。**补一句** "`Book.op_log_version: int` 是 op 日志版本号（每次 `apply_envelope` +1），不是 IR schema 版本"（R15 之后用新字段名；若 R15 先合入 AGENTS.md 也按新名写）。
6. **Config**：全量 env var 清单 + `EPUBFORGE_EDITOR_NOW` 等（标 "test-only / scratch subprocess injection"）。**新增一段**："自 vNext 起，TOML 配置文件路径必须通过 `--config <path>` CLI 参数或 `EPUBFORGE_CONFIG_PATH` env var 显式指定；不再隐式读取 `config.toml` / `config.local.toml`"（R6 行为说明）。
7. **保留** Beads / shell commands 段落不动。

**影响面**：仅 `AGENTS.md`（~160 行改动）。

**依赖**：建议放在所有代码重构之后，反映最终状态。

---

### [R8] 测试 `_prov` fixture 统一（D7=A 确认 pytest fixture）

**问题描述**：`_prov` helper 在 **10 个**测试文件各自独立定义（`tests/test_audit_table_merge.py:9` 等 10 处）。无 `conftest.py`。

**建议方案**：新建 `tests/conftest.py`：
```python
@pytest.fixture
def prov():
    def _make(page: int = 1, source: Literal["llm", "vlm", "passthrough"] = "passthrough") -> Provenance:
        return Provenance(page=page, bbox=None, source=source)
    return _make
```
各测试文件的测试函数签名加入 `prov` 参数，调用点从 `_prov(...)` 改为 `prov(...)`。删除 10 处本地定义。

**影响面**：新建 `tests/conftest.py`（~15 行）；10 个测试文件各删 3-5 行，改所有 `_prov()` 调用点与相关函数签名。

**风险**：低。

**依赖**：无。

---

### [R9] 迁移 9 个 editor `python -m` 脚本为 `epubforge editor <cmd>` Typer 子命令（D8=B 确认 ⚠️）[MOD]

**用户追加指令（human_input.md 最新，覆盖原 D8=A）**：
> python -m 子命令都改成 epubforge editor xxx 去掉 -m editor.xx

**问题描述**：
- `editor/` 下 9 个 kebab-case 文件：`acquire-book-lock.py`、`acquire-lease.py`、`apply-queue.py`、`import-legacy.py`、`propose-op.py`、`release-book-lock.py`、`release-lease.py`、`render-prompt.py`、`run-script.py`。同目录其他文件 snake_case。
- 用户调用方式当前是 `python -m epubforge.editor.<name>`，绕过顶层 `epubforge` Typer app，与 `epubforge run/parse/classify/...` 风格不一致。
- 测试 `tests/test_editor_tool_surface.py` 用 `subprocess` + `python -m` 调用 19 处；文档 `docs/agentic-editing-howto.md` 约 29 处反引号引用。

**建议方案（D8=B 的实现路径）**：

**Step 1：每个脚本的业务逻辑抽取为纯函数**

当前 9 个脚本各自是 `argparse` + `if __name__ == "__main__"` 薄包装。将每个脚本的 `main()`（或对等逻辑）重构为不依赖 argparse 的纯函数（带类型签名），`argparse` 块保留或删除取决于 Step 2 选择。

示例 `editor/propose_op.py`（改名后）：
```python
def propose_op(work: Path, input_payload: str, cfg: Config) -> int:
    """Core logic, returns exit code."""
    ...
```

**Step 2：建立 `editor` Typer 子 app**

新建 `src/epubforge/editor/app.py`：
```python
import typer
from pathlib import Path
from epubforge.config import Config

editor_app = typer.Typer(help="Editor subsystem commands", no_args_is_help=True)

@editor_app.command("propose-op")
def _propose_op_cmd(ctx: typer.Context, work: Path = typer.Argument(...)):
    from epubforge.editor.propose_op import propose_op
    raise typer.Exit(propose_op(work, sys.stdin.read(), ctx.obj))

# ... 9 commands, command name 可保持 kebab-case（Typer 的惯例）
```

子命令名用 **kebab-case**（`propose-op`、`apply-queue` 等）——Typer/Click 惯例，与用户原话 `epubforge editor xxx` 形式一致。文件名按 PEP 8 必须 snake_case（Python 模块名限制）。

**Step 3：`cli.py` 挂载 editor 子 app**
```python
from epubforge.editor.app import editor_app
app.add_typer(editor_app, name="editor")
```

**Step 4：文件重命名（kebab → snake，仅文件层）**

9 个 `.py` 文件重命名为 snake_case：`acquire_book_lock.py`、`acquire_lease.py`、`apply_queue.py`、`import_legacy.py`、`propose_op.py`、`release_book_lock.py`、`release_lease.py`、`render_prompt.py`、`run_script.py`。**目的**：能被 Typer command 从同包 import（`from epubforge.editor.propose_op import propose_op`）。

**Step 5：删除或瘦身 `editor/__main__.py`**

用户指令 "去掉 -m editor.xx" 意味着 `python -m epubforge.editor.<cmd>` 不再是支持的入口。`editor/__main__.py:13-25` 若只是一个 command 分发表，整个文件删除。若有其它职责（如当前 `python -m epubforge.editor` 整体分发），改为调用 `editor_app()`。

**Step 6：测试从 `subprocess` + `python -m` 迁 Typer `CliRunner`**

`tests/test_editor_tool_surface.py` 的 19 处 `_run_module("epubforge.editor.<name>", ...)` 改造：
- **首选**：`typer.testing.CliRunner().invoke(editor_app, ["<name>", ...])`——in-process，快 ~10x，保留 exit_code / stdout / stderr 检查。
- **例外**：若某测试明确验证 subprocess 隔离语义（scratch env var 注入、fork 行为等），改为 `subprocess.run(["uv", "run", "epubforge", "editor", "<name>", ...])`——调用真实 entrypoint。执行时 grep `EPUBFORGE_EDITOR_NOW` / `EPUBFORGE_PROJECT_ROOT` / `EPUBFORGE_WORK_DIR` / `EPUBFORGE_EDIT_STATE_DIR` 是否出现在 test 文件中——若是，保留 subprocess。

**Step 7：同步更新调用站点（文档 + prompt）**

- `src/epubforge/editor/prompts.py:23-24`：prompt 文本 `python -m epubforge.editor.run-script` → `epubforge editor run-script`（两处）。
- `docs/usage.md:38,88-99`：命令示例 10 行，全改。
- `docs/agentic-editing-howto.md`：全文约 29 处 `python -m epubforge.editor.<cmd>` 反引号引用，全改为 `epubforge editor <cmd>`。
- `docs/footnote-audit-process.md` / `docs/finer-proofread.md` / `docs/fix-plan-v3.md`：若含引用，一并更新。
- 9 个文件的 docstring 首行：`"""CLI entrypoint for \`python -m epubforge.editor.<name>\`."""` → `"""Implementation for \`epubforge editor <name>\`."""`。
- AGENTS.md 中所有 `python -m epubforge.editor.*` 引用（R7 中一起处理）。

**Step 8：执行扫尾检查**

- `grep -rn 'python -m epubforge\.editor\.' .` 返回空。
- `grep -rn '_run_module\|python.*-m.*epubforge\.editor' tests/` 返回空（subprocess 测试已迁 CliRunner 或改为 `epubforge editor`）。
- `grep -rn 'epubforge\.editor\.[a-z]*-[a-z]*' .` 返回空（kebab-case import path 都已消除）。

**影响面**：
- 新增 `src/epubforge/editor/app.py`（~100 行——9 个 command 注册 + import wiring）。
- 9 个文件重命名 + argparse 块删除或重构 + docstring 改（~200 行改动）。
- `src/epubforge/cli.py`（+1 行 `add_typer`）。
- `src/epubforge/editor/__main__.py`（删除或改为 `editor_app()`）。
- `tests/test_editor_tool_surface.py`（19 处迁 CliRunner，预计减少 ~40 行——in-process 不需要 subprocess cleanup）。
- `docs/`：3-4 文件，共 ~40 处字符串替换。
- `editor/prompts.py`：2 处。

**风险**：中-高（比 v2 的 R9 明显扩大）。
1. **subprocess → CliRunner 迁移**：若现有 subprocess 测试依赖 stdin/stdout pipe 精确行为，CliRunner 的 `input=` 参数可替代，但需逐一核对。
2. **Typer Context 下发 Config**：9 个 command 都要从 `ctx.obj` 取 Config，改错会导致 "NoneType has no attribute" 运行时错误。需 `pytest` 覆盖所有 9 个 command 的 happy path。
3. **scratch env var 注入语义**：若某测试通过 subprocess 显式设置 `EPUBFORGE_EDITOR_NOW` 等验证行为，迁 CliRunner 后需用 `monkeypatch.setenv(...)` 替代。
4. **R9 与 R6 绑定变强**：必须同一 commit 完成；中间态（R9 完成 R6 未完成，或反过来）会导致 `--config` 参数无处着落。

**依赖**：与 R6 绑定同 commit（根 callback 同时处理 `--config` 与挂载 `editor_app`）；R7 文档同步必须反映新命令形式。

---

### [R10] 补漏 `SplitMergedTable` 到 `editor/__init__.py` 的 `__all__`

**问题描述**：`editor/ops.py:869` 的 `__all__` 已含 `SplitMergedTable`，但 `editor/__init__.py:28-51` 的 re-export 与 `__all__` **遗漏**。

**建议方案**：`editor/__init__.py:28-51` 的 import 语句加 `SplitMergedTable,`，`__all__` 列表加 `"SplitMergedTable",`。

**影响面**：2 行。**风险**：极低。**依赖**：无。

---

### [R11] `console.print` → `logging` 在 pipeline.py 中统一

**问题描述**：`pipeline.py` 对阶段边界使用 `console.print`（rich），其他所有模块用 `logging`。两套输出路径，stage banner 不会进 `work/<book>/logs/run-*.log`。

**建议方案**：删掉 `pipeline.py:8,13` 的 `Console` import 与实例；11 处 `console.print(...)` 改 `log.info(...)`。stderr 侧 RichHandler 自动上色，不损失视觉。

**影响面**：`pipeline.py` ~15 行。**风险**：低（CLI 输出格式轻微变化）。**依赖**：无。

---

### [R12] 清理 `pyproject.toml` 的 `pillow` 未使用依赖

**问题描述**：`pyproject.toml:19` 声明 `pillow>=10`，但 `src/` grep 无 `PIL`/`pillow` import。

**建议方案**：删除 `pyproject.toml:19`。

**影响面**：1 行；`uv.lock` 工具重算。**风险**：低（fitz/PyMuPDF 不依赖 PIL）。**依赖**：无。

---

### [R13] 统一 editor state 写入路径：`log.py` 裸 `write_text` → `atomic_write_text`

**问题描述**：一致性瑕疵——同包 `editor/state.py:230-234` 已提供 `atomic_write_text/json/model`（临时文件 + `os.replace`）且同包大量使用，唯独 `editor/log.py:190-191,217` 3 处对 `archive_path / CURRENT_LOG`、`archive_path / BOOK_FILE`、`paths.current` 使用裸 `write_text`。

**建议方案**：`log.py` import `from epubforge.editor.state import atomic_write_text`；3 处 `.write_text(...)` 改为 `atomic_write_text(path, content)`。

**影响面**：`editor/log.py` 3 行。**风险**：低。**依赖**：无。

---

### [R14] 抽取 audit HTML regex 到 `audit/_html.py`

**问题描述**：`audit/tables.py:13-17` 与 `audit/table_merge.py:20-23` 存在 HTML 解析 regex 重复：
- **完全重复**：`TBODY_RE`、`COLSPAN_RE` 两个完全一致。
- **等价重复**：`ROW_RE` pattern 稍差异但语义等价。
- **语义不同保留**：`CELL_RE` 两处差异实质，**不合并**。
- `ROWSPAN_RE` 只在 tables.py 有，保留。

**建议方案**：新建 `src/epubforge/audit/_html.py`，导出 `TBODY_RE`、`ROW_RE`（使用 tables.py 捕获式版本）、`COLSPAN_RE`。两文件改为 import，删本地定义。`table_merge.py` 改用 `.group(1)` 或 `.group(0)` 对应新 ROW_RE。

**影响面**：新增 `audit/_html.py`（~8 行）；`audit/tables.py` 删 3 行 + import；`audit/table_merge.py` 删 3 行 + import + 1 处 group 调整。

**风险**：低。需跑 `tests/test_audit_detectors.py` + `tests/test_audit_table_merge.py`。

**依赖**：无，与 R3/R12 同 commit（死/重复清理）。

---

### [R15] `Book.version` → `Book.op_log_version` 重命名（用户新增 ⚠️）[NEW]

**问题描述**：`ir/semantic.py:174` 的 `Book.version: int = 0` 名称含糊——实际语义是"op 日志版本号"（每次 `apply_envelope` +1，与 `OpEnvelope.base_version` / `applied_version` 对齐；不是 IR schema 版本）。用户明确要求改名。

**v2 原放弃理由**："牵扯序列化格式 break，且 `version` 名称虽宽泛但约定俗成"——现用户授权 break fixture，该理由消失。

**Scope 界定（重要）**：仅重命名 `Book.version` 及其所有引用点；**不改** `OpEnvelope.base_version` / `applied_version`（这些名称已清晰，且含义不同——"the Book version that this op is based on / applied at"）。

**grep 范围**（需核对的文件，每处需识别是否指 `Book.version`）：
- **确认涉及 Book.version**：`src/epubforge/ir/semantic.py:174`（定义）；`src/epubforge/editor/apply.py`、`editor/tool_surface.py`、`editor/prompts.py`、`editor/log.py`、`editor/state.py`、`editor/ops.py`、`editor/memory.py`（通过 grep `\bversion\b` 确认上下文）。
- **确认不涉及**：`src/epubforge/epub_builder.py`、`src/epubforge/io.py` 的 `version` 可能指其他字段——逐一核对。
- **测试文件**：`tests/test_editor_apply.py`、`test_foundations_helpers.py`、`test_editor_tool_surface.py`、`test_editor_log.py`、`test_ir_semantic.py`——大量引用需改。

**建议方案**：
1. `ir/semantic.py:174`：`version: int = 0` → `op_log_version: int = 0`。
2. 全项目 grep + 批量替换 `book.version` → `book.op_log_version`；`self.version`（在 `Book` 类方法上下文）→ `self.op_log_version`。
3. **不改**：`OpEnvelope.base_version` / `applied_version`（字段名不同）、`epub_builder.py` 的 `version` 若指 EPUB metadata 版本（应不是 `Book.version`，grep 确认）、`io.py` 的 `LEGACY_BOOK_FILENAMES` 无 version 字段。
4. **JSON 序列化 break**：`book.json` 中 `"version": N` 字段 → `"op_log_version": N`；`edit_log.jsonl` 若直接序列化 Book（检查），随之 break。**用户已授权**，zxgb `edit_state/` 若需重建则重建。
5. AGENTS.md 相关一句在 R7 中使用新字段名即可（R7 依赖 R15 的字段名变更，执行顺序应 R15 先于 R7）。

**影响面**：IR schema + editor 8 个文件 + 测试 5 个文件 + 1 条 AGENTS.md 文案。总计 ~40 行改动（多为单 token 替换）。

**人决策点**：无（用户已明确"i prefer to use op_log_version"）。

**风险**：中-高。
1. 序列化字段名 break——zxgb `edit_state/book.json` replay 失败（用户授权）。
2. 若 `Book` 以外还有子对象（如 `Chapter`）有 `version` 字段，不能误伤——执行时 grep 细核。
3. pyrefly/mypy 会捕捉所有错漏，但 JSON 字符串 key 不会——必须全项目 grep `"version"` 在 book 上下文中的出现。

**依赖**：放在 R7 之前（文档需引用新名）；与 R6/R9 同 commit 或独立 commit 均可。

---

## 3. 计划外明确放弃的事项

以下问题**不在**本轮范围：

1. **Config 迁移到 pydantic-settings BaseSettings** — ~~不做~~ **已改为做**（R6 新方案，D5=B）。
2. **prompt caching 多轮 `cache_control` 扩展**（Report 2 P6）：目前 VLM 调用只有一条 system message，功能性足够。
3. **`_call_parsed` / `_call_json_object_fallback` usage 解包/budget 翻倍共享**（Report 2 P2）：降级路径偶发，抽共享反而增加间接层。
4. **`use_vlm: bool` → `ClientProfile` dataclass**（Report 2 P4）：当前项目只有一个 client 调用点，profile 抽象收益极低。
5. **`staging.jsonl` 的 apply-queue batch atomicity**（Report 3 P5）：设计选择，在 AGENTS.md 新节中文档化。
6. **`TableMergeRecord.constituent_block_uids` 增补**（Report 3 P7）：uid 在 assembler 阶段不稳定，是设计选择。
7. ~~**`Book.version` 重命名**~~ → **已改为做**（R15，用户明确要求）。
8. **`editor/prompts.py` 的 f-string 静态检查**（Report 2 P7）：prompt 模板变更不频繁。
9. **pipeline/extract 绕过 io.py**（Report 4 #3）：extract.py 的中间 artifact 是 pipeline 内部 cache，不在同一致性对比域；R13 只修 log.py（核心 state 写入）。
10. **`audit` 的 `CELL_RE` 合并**（Report 4 #2 的子项）：两处 CELL_RE 语义不同（见 R14）。

---

## 4. 整体顺序与分组（commit 粒度）

建议 **6 个 commit**（按执行顺序；比 v2 多一个因 R15 新增 + R6 scope 扩大）：

**Commit 1 — editor 内部去重**（~1 天）
- R1（validators 抽取）
- R2（StrictModel 统一）
- R10（`SplitMergedTable` 导出补漏）

**Commit 2 — 死代码清理 + audit 整合**（~半天）
- R3（CLEAN_SYSTEM 字符串删除 + `Toc*`/`Clean*` model 删除）
- R12（pillow 依赖删除）
- R14（audit HTML regex 抽取）

**Commit 3 — apply 重构 + CJK**（~1 天）
- R4（`_apply_op` dispatch 表）
- R5（`_join_text("cjk")` 修正 + text_utils 抽取）

**Commit 4 — IR schema rename**（~半天）
- R15（`Book.version` → `op_log_version`）

*单独 commit 方便 revert 与定位序列化 break 的冲击面。*

**Commit 5 — Config 现代化 + editor CLI 迁 Typer**（~2 天，最大 commit）
- R6（pydantic-settings 迁移 + 只读 `--config` + 死 section 删 + CLI 硬编码消除 + vlm_max_tokens/log_level 入 Config）
- R9（9 个 editor 脚本迁 `epubforge editor <cmd>` Typer 子命令 + 文件重命名 + 测试迁 `CliRunner` + 文档 ~40 处字符串替换）
- R11（console.print → logging）
- R13（log.py atomic_write）

*R6 与 R9 **必须**同一 commit：Typer 根 callback 处理 `--config`，所有子命令通过 `ctx.obj` 拿 Config——分开提交会产生无法运行的中间态。*

**Commit 6 — 文档 + 测试重整**（~1 天）
- R7（AGENTS.md 重写，含 invariant、`op_log_version` 语义说明、`--config` 新约定）
- R8（conftest.py，pytest fixture 风格）

### 为什么分 6 个
- **Commit 1** 纯机械重构，风险最低，先做让后续项基于新基类 / 共享 validator。
- **Commit 2** 删除 + 新抽取 util，走得越早暴露问题越快。
- **Commit 3** dispatch 改动较大，独立 commit 便于 review 与回滚。
- **Commit 4** 序列化 break 独立——任何 fixture 问题一眼可定位到此 commit。
- **Commit 5** pydantic-settings 迁移 + CLI 重命名深度耦合（`--config` 选项分发到 9 个 snake_case 子命令），合并可避免 `config.toml` 与 `propose-op.py` / `propose_op.py` 中间不一致态。
- **Commit 6** 最后做，反映终态。

---

## 5. 验证策略

**每 commit 必跑**：
```bash
uv run pytest -x
uv run pyrefly
uv run pre-commit run --all-files   # 若配置
```

**端到端冒烟**（Commit 3/4/5 后重点）：
```bash
uv run epubforge --config config.example.toml run fixtures/zxgb.pdf --force-rerun --from 1
```
若 zxgb 的 `edit_state/` 因 R5/R15 不可复用，允许重建。

**editor CLI 冒烟**（Commit 5 后，**新形式**）：
```bash
uv run epubforge --config config.example.toml editor init work/zxgb
uv run epubforge --config config.example.toml editor doctor work/zxgb
uv run epubforge --config config.example.toml editor propose-op work/zxgb < sample_ops.json
uv run epubforge --config config.example.toml editor apply-queue work/zxgb
```
（子命令名用 kebab-case 遵循 Typer/Click 惯例；`python -m epubforge.editor.<cmd>` 入口已废除。）

### 关键检查清单
- [ ] `grep -r _require_non_empty src/` 只剩 `_validators.py` 一处。
- [ ] `grep -rn -E 'EditorModel|MemoryModel|DoctorModel|LeaseModel' src/ tests/` 只剩 R2 过程的 import 行。
- [ ] `grep -r CLEAN_SYSTEM src/` 返回空。
- [ ] `grep -r _LINE_BREAK_RULES src/epubforge/llm/prompts.py` 返回定义 + `VLM_SYSTEM` 引用共 2 类。
- [ ] `grep -r 16384 src/` 返回空（或仅 `config.py` 一处）。
- [ ] `grep -rn 'python -m epubforge\.editor\.' .` 返回空（D8=B 废除该入口形式）。
- [ ] `grep -rn 'epubforge\.editor\.[a-z]*-[a-z]*' .` 返回空（kebab-case import path 消除）。
- [ ] `grep -rn '_run_module\|subprocess.*epubforge\.editor' tests/` 返回空或仅剩显式 subprocess 隔离测试。
- [ ] `uv run epubforge --config config.example.toml editor doctor work/zxgb` 正常退出。
- [ ] `grep -rn '\.version\b' src/epubforge/ | grep -Ev 'base_version|applied_version|op_log_version|__version__|epub_version|package_version'` 返回空（确认 `Book.version` 全部改名）。
- [ ] `grep -r '"version"' src/ tests/` 在 book 序列化上下文中的出现已改为 `"op_log_version"`。
- [ ] `grep -r 'config\.toml\|config\.local\.toml' src/ docs/` 只剩"历史兼容删除说明"的一处文档（R6 中 AGENTS.md 的迁移说明），不在运行时代码中。
- [ ] `grep -rn 'Stage 8' src/` 返回空。
- [ ] `uv run epubforge --config config.example.toml run fixtures/zxgb.pdf` 不报错。
- [ ] 根据 D6=B：AGENTS.md Pipeline 表格与 `cli.py` `--from max=4` 一致；`pipeline.py:120` stage_timer 改 `"5 build"`。

---

## 6. 决策记录（已封版）

| 决策 | 用户选择 | 执行后果 |
|---|---|---|
| **D1** 基类命名 | **A. `StrictModel`** | R2 使用该名 |
| **D2** CLEAN_SYSTEM 处理 | **A. 彻底删除** | R3 按 "仅删字符串、保留 `_*_RULES`" 执行 |
| **D3** dispatch 范围 | **A. 只改 `_apply_op`** | R4 不动其它三处 isinstance |
| **D4** text_utils 位置 | **A. 新建 `text_utils.py`** | R5 新增该模块 |
| **D5** Config 框架 | **B. 迁到 pydantic-settings** | R6 重写，新增依赖 |
| **D6** Build stage 编号 | **B. Stage 5** | 同步 `cli.py:151` / `pipeline.py:119,120` 三处 |
| **D7** conftest 形式 | **A. pytest fixture** | R8 使用 fixture 方案 |
| **D8** editor 顶层子命令 | **B. 迁 Typer `epubforge editor <cmd>`** ⚠️ 翻转 | R9 范围扩大：9 脚本迁 Typer + 测试迁 CliRunner + 文档 ~40 处字符串替换；`python -m` 入口废除 |
| **R5 额外** | 不在意 break zxgb fixture | §7.1 fixture 重建确认**移除** |
| **R6 扩展** | 只读 CLI `--config`，不隐式扫描 | R6 Step 2：删 `config.py:65` fallback 分支 + 删 `.gitignore:233-234` |
| **R15 新增** | `Book.version` → `op_log_version` | 新增独立 commit，带 schema break |

---

## 7. 执行前仍需用户最终确认的 1 点

### ~~7.1 R6：子进程接受 `--config` 的方式~~ — **已因 D8=B 自动消除**

D8 翻转为 Typer 子命令后，`epubforge editor <cmd>` 是单进程调用，`--config` 在 Typer 根 callback 一次解析后通过 `ctx.obj` 下发到所有子命令。不再有"9 个独立子进程如何拿 config"的问题。

### 7.1 R6：TOML schema 扁平化 vs 保留嵌套（**唯一剩余待定**）

pydantic-settings 的 `TomlConfigSettingsSource` 默认按顶层 key → 字段名映射。当前 TOML 嵌套如：
```toml
[llm]
base_url = "..."
api_key = "..."
```
对应字段 `llm_base_url` / `llm_api_key`，名称错位。两种解决：
- **方案 α（推荐）**：TOML schema 扁平化——`config.example.toml` 改为 `llm_base_url = "..."` 平铺写。用户只需改一个 example 文件，自己的 `*.toml` 按例子格式即可。代价：TOML 阅读性略降。
- **方案 β**：保留嵌套 TOML，为 Config 提供自定义 `settings_customise_sources` + 嵌套→扁平映射函数。代价：~40 行 glue code。

请用户在 `human_input.md` 追加选择（α/β）。

---

## 附录 A. v2 → human-reviewed v1 差异摘要

| 变化 | 内容 |
|---|---|
| 工作项数 | 14 → **15**（新增 R15 `Book.version` rename） |
| Commit 数 | 5 → **6**（R15 独立 commit） |
| 新依赖 | **+ `pydantic-settings`** |
| R6 | 从"保留 dataclass + 消除 CLI 硬编码" → "**迁 pydantic-settings + 只认 `--config`**"；删除 `config.toml`/`config.local.toml` 隐式扫描；删除 `.gitignore` 两条 |
| R9 | 从"仅重命名 9 个文件"扩大为 "**迁 Typer 子命令 `epubforge editor <cmd>`** + 重命名 + 测试迁 CliRunner + 文档全局替换"（因 D8 翻转 B） |
| R7 | 新增 `--config` 约定说明段落；`Book.version` 语义句改用 `op_log_version`；所有 `python -m epubforge.editor.*` 表述改为 `epubforge editor <cmd>` |
| §3 放弃项 | #1（config 迁移）与 #7（Book.version rename）从放弃转为正式项 |
| §7 待调研 | 从 2 点缩减到 **1 点**（仅 TOML schema 形态；子进程 config 分发因 D8=B 废除） |
| §6 决策汇总 | 从"待决定"转为"已封版"表格；D5/D8 翻转明确 |

---

## 附录 B. `human_input.md` 原文存档

```
D1: A, StrictModel
D2: delete
D3: use that dispatch table pattern
D4: A, ok
D5: B, migrate
D6: B, rename
D7: A
D8: A, currently keep agentic helpers in editor submodule

R5: i don't care breaking that file, i've finished all job on it.
R6: why there are two toml files? we should only have one config toml?
R6 decision: don't infer config file to read, only read from the file specified in cli arg,
don't make config.toml / config.local.toml as default config, force user to specify.

discarded items but i think it should be done:
item 7: i prefer to use op_log_version
```

**追加指令（用户对话内，2026-04-23）**：

> python -m 子命令都改成 epubforge editor xxx 去掉 -m editor.xx

此指令将 D8 从 A 翻转为 B，并扩大 R9 范围。
